"""QQ 消息接入层：触发规则、防抖接线、引擎调用与回复发送。

重逻辑（payload 组装、图片管线、引用/转发解析、最近图片缓冲）在 pipeline.py；
本文件只保留 NoneBot 接线与回复链路。
"""

import asyncio
import logging
import os
import re

from nonebot import on_message
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    MessageEvent,
    PrivateMessageEvent,
)

from .acl import is_allowed
from .outbound import default_throttle, deliver_reply
from .pipeline import build_payload, chat_key, get_bot, merge_parts
from .wakewords import match_wake_word

logger = logging.getLogger(__name__)

engine = None  # set by __init__.py


def _match_wake_words(text: str) -> bool:
    """群聊文本触发：**只认自定义唤醒词**。

    旧前缀（`ai ` / `!ai ` / `/ai `，由 AGENT_PREFIX 正则定义）已按需求移除；
    群里除了唤醒词，只有 @机器人 能触发。
    """
    return match_wake_word(text) is not None


def _plain_text(event: MessageEvent) -> str:
    """只取 text 段拼接（忽略 reply/at/image 等），用于唤醒词匹配。

    评审 REVIEW-bbd8913..f6dffcc.md 的 M11：此前用 ``str(event.get_message())``，
    前导 ``[CQ:reply…][CQ:at,…]`` 会让 ``startswith`` 失配——「引用别人消息后打唤醒词」
    不触发，而 README 声称「消息以任一唤醒词开头即触发」。
    """
    try:
        return "".join(
            str(seg.data.get("text") or "")
            for seg in event.get_message()
            if seg.type == "text"
        ).strip()
    except Exception:  # 段结构异常时退回原字符串，保持旧行为
        return str(event.get_message()).strip()


def _is_self_message(event: MessageEvent) -> bool:
    """事件是否是 bot 自己发出的消息。

    协议端开启「上报自身消息」后，bot 经 WS 发的每条消息都会作为 message 事件
    回传（user_id == self_id）。此前全仓无此过滤：私聊分支无条件触发，bot 于是
    对自己的消息跑完整对话——线上复现：群里要文件 → 私发文件 → 回传 → 空文本
    + 复用历史图片 → 模型解析旧图，且 bot 的回复再次回传，存在自循环。
    """
    self_id = str(getattr(event, "self_id", "") or "")
    return bool(self_id) and str(event.get_user_id()) == self_id


def trigger_rule(event: MessageEvent):
    if _is_self_message(event):
        return False
    if isinstance(event, PrivateMessageEvent):
        return True
    if isinstance(event, GroupMessageEvent):
        # 群聊需命中唤醒词或 @机器人；「先发图后追问」等无前缀消息不会进本 handler
        # （私聊无此限制，见 README「群聊限制」一节）
        # 适配器（onebot v11 bot.py::_check_at_me）只认「消息开头/结尾」的 @bot——
        # 「reply + @别人 + @bot」这种 @bot 在中间的消息 to_me 会是 False，这里自行扫描补上
        at_bot = any(
            str(seg.data.get("qq", "")) == str(event.self_id)
            for seg in event.get_message()
            if seg.type == "at"
        )
        return _match_wake_words(_plain_text(event)) or at_bot or event.is_tome()
    return False


chat_matcher = on_message(rule=trigger_rule, priority=10, block=True)


# ---------- 群聊上下文记录 ----------
# 优先级高于 chat_matcher（数字小=更先）且 block=False：先记录再放行，
# 这样「没被 @ 的群消息」也留痕，被唤醒时才有语境可用。
def _record_group_rule(event: MessageEvent) -> bool:
    # bot 自己的消息不进群上下文：那是 bot 的发言，混进「其他群成员最近说了什么」
    # 会让模型把 bot 说过的话当成群友发言（与 _is_self_message 同一判据）
    return isinstance(event, GroupMessageEvent) and not _is_self_message(event)


group_recorder = on_message(rule=_record_group_rule, priority=5, block=False)


@group_recorder.handle()
async def handle_group_record(event: MessageEvent):
    from .group_context import context_enabled, group_context

    if not context_enabled():
        return
    try:
        segs = list(event.get_message())
        has_image = any(getattr(s, "type", "") == "image" for s in segs)
        sender = getattr(event, "sender", None)
        who = ""
        if sender is not None:
            who = str(
                getattr(sender, "card", "") or getattr(sender, "nickname", "") or ""
            )
        if not who:
            who = str(event.get_user_id())
        group_context.record(
            str(event.group_id),
            who,
            _plain_text(event),
            message_id=str(getattr(event, "message_id", "") or ""),
            has_image=has_image,
        )
    except Exception:
        logger.warning("record group context failed", exc_info=True)


def _debounce_seconds() -> float:
    v = os.getenv("AGENT_DEBOUNCE", "3").strip()
    try:
        return max(0.0, float(v))
    except Exception:
        return 3.0


_debouncer = None  # 模块级单例；delay 变化时重建（M14：不再用 globals() hack）


def _debounce_max_parts() -> int:
    try:
        return max(1, int(os.getenv("AGENT_DEBOUNCE_MAX_PARTS", "20")))
    except ValueError:
        return 20


def _get_debouncer():
    global _debouncer
    from .debounce import Debouncer

    delay = _debounce_seconds()
    max_parts = _debounce_max_parts()
    if (
        _debouncer is None
        or _debouncer.delay != delay
        or _debouncer.max_parts != max_parts
    ):
        _debouncer = Debouncer(delay, max_parts=max_parts)
    return _debouncer


def get_debouncer():
    """供停机 flush 使用（bot.py on_shutdown）。"""
    return _debouncer


@chat_matcher.handle()
async def handle_chat(event: MessageEvent):
    if not is_allowed(event):
        await chat_matcher.finish("你没有权限使用这个功能。")

    user_id = str(event.get_user_id())
    group_id = str(event.group_id) if isinstance(event, GroupMessageEvent) else None
    chat_target = f"group:{group_id}" if group_id else f"private:{user_id}"

    payload = await build_payload(event, user_id, group_id)
    text = payload.get("text", "")
    logger.info(
        "[msg] %s | user=%s | text=%s", chat_target, user_id, _truncate(text, 200)
    )

    delay = _debounce_seconds()
    if delay <= 0:
        await _answer([payload])
        return

    await _get_debouncer().push(chat_key(user_id, group_id), payload, _answer)


_turn_semaphore: asyncio.Semaphore | None = None


def _max_concurrent_turns() -> int:
    try:
        return max(1, int(os.getenv("AGENT_MAX_CONCURRENT_TURNS", "4")))
    except ValueError:
        return 4


def _get_turn_semaphore() -> asyncio.Semaphore:
    """全进程并发闸门：不同 key 的窗口同时到期时不至于把 LLM 打满
    （M：实测 200 个 key 并发 → 200 路引擎；per-key 锁只能保证同会话串行）。"""
    global _turn_semaphore
    if _turn_semaphore is None:
        _turn_semaphore = asyncio.Semaphore(_max_concurrent_turns())
    return _turn_semaphore


async def _answer(parts: list) -> None:
    """防抖窗口结束：合并多条消息内容，跑引擎并按阈值分层投递回复。"""
    payload = parts[0]
    combined, images = merge_parts(parts)

    async with _get_turn_semaphore():
        reply = await _run_and_format(payload, combined, images)
    try:
        bot = get_bot(payload.get("self_id") or None)
        if bot is None:
            raise RuntimeError("no bot connected")
        group_id = payload.get("group_id")
        kind = "group" if group_id else "private"
        ident = int(group_id) if group_id else int(payload["user_id"])
        mode = await deliver_reply(
            bot,
            kind=kind,
            ident=ident,
            text=reply,
            self_id=str(payload.get("self_id") or getattr(bot, "self_id", "") or ""),
            throttle=default_throttle(),
        )
        logger.info(
            "[reply] %s | mode=%s | text=%s",
            payload.get("chat_target", "?"),
            mode,
            _truncate(reply, 200),
        )
    except Exception:
        logger.exception("send reply failed")
        try:
            # 异常串可能内嵌内网主机名/URL（httpx 错误信息），不进聊天
            await _send_reply(payload, "出错啦，请稍后再试")
        except Exception:
            logger.exception("final send failed")


def _turn_timeout_seconds() -> float:
    """单回合引擎超时（秒），``AGENT_TURN_TIMEOUT`` 可覆盖，0 = 不限制。

    挂死的工具/LLM 调用此前会永久占用并发闸门（信号量计数 -1，叠几次后
    整个 bot 假死）。默认 180s——多工具回合（搜索+识图+文件）正常上界远低于
    此；真挂死时用户宁可要一句超时提示，也不该无限等待。脏值告警回退默认。
    """
    raw = (os.getenv("AGENT_TURN_TIMEOUT") or "").strip()
    if not raw:
        return 180.0
    try:
        value = float(raw)
    except ValueError:
        logger.warning("AGENT_TURN_TIMEOUT=%r 不是数字，回退 180", raw)
        return 180.0
    if value < 0:
        logger.warning("AGENT_TURN_TIMEOUT=%r 为负，回退 180", raw)
        return 180.0
    return value


async def _run_and_format(payload, text: str, extra_images: list[str]) -> str:
    """引擎调用 + 文件兜底 + QQ 纯文本化。"""
    user_id = payload["user_id"]
    context = {
        "user_id": user_id,
        "group_id": payload.get("group_id"),
        "platform": "qq",
    }
    try:
        run = engine.run(context, text, extra_images=extra_images or None)
        timeout = _turn_timeout_seconds()
        if timeout > 0:
            # 超时只取消这次引擎执行：出站投递在其后，不存在"取消后重发"的
            # 双发风险（与 is_uncertain_send_error 防的不是一个方向）
            reply = await asyncio.wait_for(run, timeout=timeout)
        else:
            reply = await run
    except TimeoutError:
        logger.error(
            "turn timeout after %ss (chat=%s)",
            _turn_timeout_seconds(),
            payload.get("chat_target", "?"),
        )
        reply = None
    except Exception:
        logger.exception("Agent engine failed")
        reply = None

    if not reply:
        reply = f"[echo] {text}" if text else "（没有收到有效内容）"

    # 文件兜底只看用户本人文本（user_text）——引用/转发内容属不可信数据，
    # 其中出现「文件/文档」字样不得触发自动发文件
    user_text = payload.get("user_text") or ""
    if (
        _user_asked_for_file(user_text)
        and reply
        and not reply.startswith("[skill error]")
        and not reply.startswith("（")
        and not reply.startswith("[echo]")
    ):
        try:
            file_result = await engine.skills.execute(
                "send_markdown_file",
                user_id=user_id,
                group_id=payload.get("group_id"),
                content=reply,
            )
            logger.info("[auto_file] %s", file_result)
            from agentcore.skills.file_sender import FILE_SEND_OK_PREFIX

            if str(file_result).startswith(FILE_SEND_OK_PREFIX):
                reply = "文件已发送"
        except Exception:
            logger.exception("auto file send failed")

    display_text = _qq_plain(reply)
    return display_text or "（回复内容为空）"


async def _send_reply(payload, chunk: str) -> None:
    """后台任务直接经 Bot API 发送（matcher 已结束，不能再用 chat_matcher.send）。

    优先用触发消息所属的 bot（payload.self_id），多账号部署不串号。
    这里只发单条，用于降级路径与并行会话；长回复分层见 outbound.deliver_reply。
    """
    bot = get_bot(payload.get("self_id") or None)
    if bot is None:
        raise RuntimeError("no bot connected")
    group_id = payload.get("group_id")
    if group_id:
        await default_throttle().acquire(f"group:{group_id}")
        await bot.send_group_msg(group_id=int(group_id), message=chunk)
    else:
        await default_throttle().acquire(f"private:{payload['user_id']}")
        await bot.send_private_msg(user_id=int(payload["user_id"]), message=chunk)
    logger.info(
        "[reply] %s | text=%s", payload.get("chat_target", "?"), _truncate(chunk, 200)
    )


def _user_asked_for_file(text: str) -> bool:
    return any(
        k in text
        for k in ["文件", "文档", "md文档", "markdown", "发我文件", "发我文档"]
    )


def _qq_plain(text: str) -> str:
    """QQ 聊天框不渲染 Markdown：把回复做轻量纯文本化，剥掉渲染符号但保留换行/列表。

    - 围栏代码块 ```...``` 整体保留、不做任何改写（占位符保护）
    - 连续空行压成单个换行（模型爱用空行分段，QQ 里只是"透气"）
    - 仅影响 QQ 里显示的文本；以文件形式发送的内容仍是原始 markdown。
    """
    if not text:
        return text
    t = text
    code_blocks: list[str] = []

    def _protect(m):
        code_blocks.append(m.group(0))
        return f"\x01CODE{len(code_blocks) - 1}\x02"

    t = re.sub(r"```.*?```", _protect, t, flags=re.DOTALL)
    # 粗体 **x** / __x__
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t, flags=re.DOTALL)
    t = re.sub(r"__(.+?)__", r"\1", t, flags=re.DOTALL)
    # 行首标题：### 标题 / # 标题 -> 标题
    t = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", t)
    # 行首引用 > -> 空
    t = re.sub(r"(?m)^\s{0,3}>\s?", "", t)
    # 行首无序列表 * 统一成 -
    t = re.sub(r"(?m)^(\s*)\*\s+", r"\1- ", t)
    # 行内代码 `x`
    t = re.sub(r"`([^`\n]+)`", r"\1", t)
    # 链接 [text](url) -> text（url）
    t = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r"\1（\2）", t)
    # 还原代码块：单次 re.sub 回调（原实现逐块 str.replace 全串扫描，随代码块数量
    # 呈 O(n²)——test_perf 实测 300k 字符 1.28s / 600k 5.00s，现为线性）
    # 连续空行压成单个换行：模型默认按 markdown 习惯用空行分段（实测一条
    # 中等长度回答 6-8 个空行），而 QQ 聊天框里空行只是纯粹的"透气"——密集
    # 分段/列表被拉得更长、更散。标题与列表行本身仍在，结构不丢。
    # **必须在还原代码块之前**做：此刻代码块还是单行占位符，块内空行不会被
    # 误收（放在还原之后就会把代码格式一起压掉，test_code_block_* 会抓住）。
    # 正则要能吃掉"只含空格的空行"：若只匹配 \n{2,}，行尾空白清理
    # （在还原之后执行）会把 " \n" 变成 "\n"，**重新制造出空行**。
    t = re.sub(r"[ \t]*\n(?:[ \t]*\n)+", "\n", t)
    if code_blocks:

        def _restore(m):
            idx = int(m.group(1))
            return code_blocks[idx] if 0 <= idx < len(code_blocks) else m.group(0)

        t = re.sub(r"\x01CODE(\d+)\x02", _restore, t)
    # 删除行首/行尾多余空白（保留行间换行）
    t = re.sub(r"[ \t]+\n", "\n", t)
    return t.strip()


def _truncate(text: str, max_len: int = 200) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."
