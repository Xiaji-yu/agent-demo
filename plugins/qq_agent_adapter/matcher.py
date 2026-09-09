import logging
import os
import re
from nonebot import on_message
from nonebot.adapters.onebot.v11 import MessageEvent, PrivateMessageEvent, GroupMessageEvent
from .acl import is_allowed

logger = logging.getLogger(__name__)

PREFIX = os.getenv("AGENT_PREFIX", r"^[!！/]?ai\s*")
engine = None  # set by __init__.py


def trigger_rule(event: MessageEvent):
    text = str(event.get_message()).strip()
    if isinstance(event, PrivateMessageEvent):
        return True
    if isinstance(event, GroupMessageEvent):
        return bool(re.match(PREFIX, text, re.IGNORECASE)) or event.is_tome()
    return False


chat_matcher = on_message(rule=trigger_rule, priority=10, block=True)


@chat_matcher.handle()
async def handle_chat(event: MessageEvent):
    if not is_allowed(event):
        await chat_matcher.finish("你没有权限使用这个功能。")

    text = str(event.get_message()).strip()
    text = re.sub(PREFIX, "", text, flags=re.IGNORECASE).strip()

    user_id = str(event.get_user_id())
    group_id = str(event.group_id) if isinstance(event, GroupMessageEvent) else None
    chat_target = f"group:{group_id}" if group_id else f"private:{user_id}"

    logger.info("[msg] %s | user=%s | text=%s", chat_target, user_id, _truncate(text, 200))

    context = {
        "user_id": user_id,
        "group_id": group_id,
        "platform": "qq",
    }

    try:
        reply = await engine.run(context, text)
    except Exception:
        logger.exception("Agent engine failed")
        reply = None

    if not reply:
        # M0 无 LLM Key 时的回声模式，验证 NapCat ↔ NoneBot 链路
        reply = f"[echo] {text}"

    # 兜底：如果用户明确要文件，但 agent 只返回了文本，自动把这段文本作为文件发送
    # （文件内容保留原始 markdown，仅聊天文本做 QQ 纯文本化）
    if (
        _user_asked_for_file(text)
        and reply
        and not reply.startswith("[skill error]")
        and not reply.startswith("（")
        and not reply.startswith("[echo]")
    ):
        try:
            file_result = await engine.skills.execute(
                "send_markdown_file",
                user_id=user_id,
                group_id=group_id,
                content=reply,
            )
            logger.info("[auto_file] %s", file_result)
            if file_result and "已发送" in str(file_result):
                reply = "文件已发送"
        except Exception:
            logger.exception("auto file send failed")

    display_text = _qq_plain(reply)

    try:
        for chunk in _split_qq_message(display_text):
            await chat_matcher.send(chunk)
            logger.info("[reply] %s | text=%s", chat_target, _truncate(chunk, 200))
    except Exception as e:
        await chat_matcher.finish(f"出错啦：{e}")


def _user_asked_for_file(text: str) -> bool:
    return any(k in text for k in ["文件", "文档", "md文档", "markdown", "发我文件", "发我文档"])


def _qq_plain(text: str) -> str:
    """QQ 聊天框不渲染 Markdown：把回复做轻量纯文本化，剥掉渲染符号但保留换行/列表。

    说明：仅影响 QQ 里显示的文本；以文件形式发送的内容仍是原始 markdown。
    """
    if not text:
        return text
    t = text
    # 粗体 **x** / __x__（非贪婪，跨行）
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t, flags=re.S)
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
    # 删除行首/行尾多余空白（保留行间换行）
    t = re.sub(r"[ \t]+\n", "\n", t)
    return t.strip()


def _split_qq_message(text: str, max_len: int = 1500) -> list[str]:
    """按句边界切分，避免在词/代码/URL 中间断开。"""
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
    return chunks


def _truncate(text: str, max_len: int = 200) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."
