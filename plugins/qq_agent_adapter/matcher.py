"""QQ 消息接入层：触发规则、防抖接线、引擎调用与回复发送。

重逻辑（payload 组装、图片管线、引用/转发解析、最近图片缓冲）在 pipeline.py；
本文件只保留 NoneBot 接线与回复链路。
"""
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
from .pipeline import build_payload, chat_key, get_bot, merge_parts

logger = logging.getLogger(__name__)

PREFIX = os.getenv("AGENT_PREFIX", r"^[!！/]?ai\s*")  # 兼容旧引用；真正生效处见 pipeline
engine = None  # set by __init__.py


def trigger_rule(event: MessageEvent):
    text = str(event.get_message()).strip()
    if isinstance(event, PrivateMessageEvent):
        return True
    if isinstance(event, GroupMessageEvent):
        # 群聊需命中前缀或 @机器人；「先发图后追问」等无前缀消息不会进入本 handler
        # （私聊无此限制，见 README「群聊限制」一节）
        return bool(re.match(PREFIX, text, re.IGNORECASE)) or event.is_tome()
    return False


chat_matcher = on_message(rule=trigger_rule, priority=10, block=True)


def _debounce_seconds() -> float:
    v = os.getenv("AGENT_DEBOUNCE", "3").strip()
    try:
        return max(0.0, float(v))
    except Exception:
        return 3.0


_debouncer = None  # 模块级单例；delay 变化时重建（M14：不再用 globals() hack）


def _get_debouncer():
    global _debouncer
    from .debounce import Debouncer

    delay = _debounce_seconds()
    if _debouncer is None or _debouncer.delay != delay:
        _debouncer = Debouncer(delay)
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
    logger.info("[msg] %s | user=%s | text=%s", chat_target, user_id, _truncate(text, 200))

    delay = _debounce_seconds()
    if delay <= 0:
        await _answer([payload])
        return

    await _get_debouncer().push(chat_key(user_id, group_id), payload, _answer)


async def _answer(parts: list) -> None:
    """防抖窗口结束：合并多条消息内容，跑引擎并直接经 Bot API 回复。"""
    payload = parts[0]
    combined, images = merge_parts(parts)

    reply = await _run_and_format(payload, combined, images)
    try:
        for chunk in _split_qq_message(reply):
            await _send_reply(payload, chunk)
    except Exception as e:
        logger.exception("send reply failed")
        try:
            await _send_reply(payload, f"出错啦：{e}")
        except Exception:
            logger.exception("final send failed")


async def _run_and_format(payload, text: str, extra_images: list[str]) -> str:
    """引擎调用 + 文件兜底 + QQ 纯文本化。"""
    user_id = payload["user_id"]
    context = {"user_id": user_id, "group_id": payload.get("group_id"), "platform": "qq"}
    try:
        reply = await engine.run(context, text, extra_images=extra_images or None)
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
    """
    bot = get_bot(payload.get("self_id") or None)
    if bot is None:
        raise RuntimeError("no bot connected")
    if payload.get("group_id"):
        await bot.send_group_msg(group_id=int(payload["group_id"]), message=chunk)
    else:
        await bot.send_private_msg(user_id=int(payload["user_id"]), message=chunk)
    logger.info("[reply] %s | text=%s", payload.get("chat_target", "?"), _truncate(chunk, 200))


def _user_asked_for_file(text: str) -> bool:
    return any(k in text for k in ["文件", "文档", "md文档", "markdown", "发我文件", "发我文档"])


def _qq_plain(text: str) -> str:
    """QQ 聊天框不渲染 Markdown：把回复做轻量纯文本化，剥掉渲染符号但保留换行/列表。

    - 围栏代码块 ```...``` 整体保留、不做任何改写（占位符保护）
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
    # 还原代码块
    for i, b in enumerate(code_blocks):
        t = t.replace(f"\x01CODE{i}\x02", b)
    # 删除行首/行尾多余空白（保留行间换行）
    t = re.sub(r"[ \t]+\n", "\n", t)
    return t.strip()


def _split_qq_message(text: str, max_len: int = 1500) -> list[str]:
    """按句边界切分，避免在词/代码/URL 中间断开。"""
    if not text:
        return []
    if len(text) <= max_len:
        return [text]

    sentence_breaks = re.compile(r'(?<=[。！？；\n])\s*')
    segments = sentence_breaks.split(text)

    chunks: list[str] = []
    buf = ""

    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        if len(buf) + len(seg) + 1 <= max_len:
            buf = f"{buf}\n{seg}" if buf else seg
        else:
            if buf:
                chunks.append(buf.strip())
            if len(seg) <= max_len:
                buf = seg
            else:
                # 超长段落硬切：优先换行、空格、中文标点边界，避免切断词/代码
                start = 0
                while start < len(seg):
                    end = min(start + max_len, len(seg))
                    if end < len(seg):
                        cut = seg.rfind("\n", start, end)
                        if cut == -1 or cut <= start:
                            cut = seg.rfind(" ", start, end)
                        if cut == -1 or cut <= start:
                            for p in "。，；、！？：":
                                cut = seg.rfind(p, start, end)
                                if cut > start:
                                    cut += 1
                                    break
                        if cut == -1 or cut <= start:
                            cut = end
                    else:
                        cut = end
                    chunks.append(seg[start:cut].strip())
                    start = cut
                buf = ""

    if buf:
        chunks.append(buf.strip())
    # 过滤空白块（纯空格/仅符号被剥掉后可能为空）
    return [c for c in chunks if c]


def _truncate(text: str, max_len: int = 200) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."
