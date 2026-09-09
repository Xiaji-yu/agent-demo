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
        if len(reply) > 500:
            for i in range(0, len(reply), 500):
                await chat_matcher.send(reply[i : i + 500])
        else:
            await chat_matcher.send(reply)
    except Exception as e:
        await chat_matcher.finish(f"出错啦：{e}")
