import os
import re
from nonebot import on_message
from nonebot.adapters.onebot.v11 import MessageEvent, PrivateMessageEvent, GroupMessageEvent
from .acl import is_allowed

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

    context = {
        "user_id": str(event.get_user_id()),
        "group_id": str(event.group_id) if isinstance(event, GroupMessageEvent) else None,
        "platform": "qq",
    }

    try:
        reply = await engine.run(context, text)
    except Exception:
        reply = None

    if not reply:
        # M0 无 LLM Key 时的回声模式，验证 NapCat ↔ NoneBot 链路
        reply = f"[echo] {text}"

    try:
        for chunk in _split_qq_message(reply):
            await chat_matcher.send(chunk)
    except Exception as e:
        await chat_matcher.finish(f"出错啦：{e}")


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
                # 超长段落硬切，尽量在空格或标点处断
                start = 0
                while start < len(seg):
                    end = start + max_len
                    if end < len(seg):
                        cut = seg.rfind(" ", start, end)
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
