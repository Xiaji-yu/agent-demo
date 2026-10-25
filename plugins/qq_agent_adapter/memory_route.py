"""/memory 长期记忆自助管理：查看 / 删除 / 清空**自己的**事实。

隐私闭环（M4-1）：Agent 会把对话蒸馏成长期事实并注入后续 prompt，用户需要
一个能看见、能删除的出口。本模块只暴露"自己的数据"——任何指令都带
``user_id`` 约束，没有管理员全局查看/删除入口（跨用户越权由存储层再兜一层）。

- ``/memory`` 列出**当前会话**最近 20 条（id + 来源 + 摘要）
- ``/memory forget <id>`` 删除当前会话的一条
- ``/memory clear`` 两段确认后清空自己的全部事实（跨会话）

列表/删除按当前会话（用户 + 群/私聊）作用域：群里的回显不得带出私聊或其他群的
事实正文，删除也只能落在当前会话内。`clear` 是本人主动触发的清空（不回显内容），
保留跨会话语义——这是用户行使「删除我的数据」的出口，若也收窄到当前会话，
换一个群就得重来一遍。

``clear`` 的确认码走内存 + 2 分钟 TTL：清空是破坏性操作，但与 web 写面/工作区
删除不同，不落盘审计也不占用 workspace confirm gate（那是文件路径语义）。
"""

from __future__ import annotations

import logging
import re
import secrets
import time

from nonebot import on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent
from nonebot.exception import FinishedException

from . import _get_driver
from .acl import deny, is_allowed

logger = logging.getLogger(__name__)

_MAX_LIST = 20
_CONTENT_PREVIEW = 60
_CLEAR_TTL = 120.0

# user_id → (确认码, 过期 monotonic)
_pending_clear: dict[str, tuple[str, float]] = {}

_USAGE = (
    "记忆指令：\n"
    "/memory              查看本会话的长期记忆（最近 20 条）\n"
    "/memory forget <id>  删除本会话的一条\n"
    "/memory clear        清空自己的全部长期记忆（跨全部会话，需二次确认）"
)


def _not_self_message(event: MessageEvent) -> bool:
    self_id = str(getattr(event, "self_id", "") or "")
    return not (self_id and str(event.get_user_id()) == self_id)


memory_cmd = on_command(
    "memory", aliases={"记忆"}, priority=5, block=True, rule=_not_self_message
)


def _get_store():
    return getattr(_get_driver(), "_agent_memory", None)


def _event_group_id(event: MessageEvent) -> str | None:
    # 与 matcher/admin 同口径：OneBot 私聊事件也带 group_id 属性（值 None），
    # 用 isinstance 判定而不是 hasattr，避免把 "None" 当群号。
    if isinstance(event, GroupMessageEvent):
        return str(event.group_id)
    return None


async def _current_session(store, event: MessageEvent) -> str:
    return await store.resolve_session(str(event.get_user_id()), _event_group_id(event))


def parse_memory_cmd(raw: str) -> tuple[str, str]:
    """解析 /memory 子命令，返回 (action, argument)。"""
    text = re.sub(r"^[/!！]?(memory|记忆)[\s/]*", "", (raw or "").strip(), flags=re.I)
    text = text.strip()
    if not text:
        return "list", ""
    parts = text.split(maxsplit=1)
    action = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    aliases = {"ls": "list", "rm": "forget", "del": "forget", "delete": "forget"}
    return aliases.get(action, action), arg


def _preview(text: str) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= _CONTENT_PREVIEW else text[:_CONTENT_PREVIEW] + "…"


async def _list_facts(matcher, event: MessageEvent, store, session_id: str) -> None:
    items = await store.list_facts_detailed(
        str(event.get_user_id()), limit=_MAX_LIST, session_id=session_id
    )
    if not items:
        await matcher.finish(
            "本会话还没有长期记忆。\n"
            "提示：对话里明确说过的偏好/事实会被自动记住；用 /memory forget <id> 删除。"
        )
    lines = [f"本会话的长期记忆（最近 {len(items)} 条）："]
    for it in items:
        src = f"[{it['source']}] " if it.get("source") else ""
        lines.append(f"#{it['id']} {src}{_preview(it['content'])}")
    lines.append("删除：/memory forget <id>；清空全部会话：/memory clear")
    await matcher.finish("\n".join(lines))


async def _forget_fact(
    matcher, event: MessageEvent, store, arg: str, session_id: str
) -> None:
    raw = arg.strip()
    if not (raw.isascii() and raw.isdigit()):
        await matcher.finish(
            f"用法：/memory forget <id>（id 来自 /memory 列表）\n\n{_USAGE}"
        )
    deleted = await store.delete_fact(
        str(event.get_user_id()), int(raw), session_id=session_id
    )
    if deleted:
        await matcher.finish(f"已删除记忆 #{int(raw)}。")
    await matcher.finish(f"没有找到记忆 #{int(raw)}（可能已删除，或不在本会话）。")


async def _clear_facts(matcher, event: MessageEvent, store, arg: str) -> None:
    user_id = str(event.get_user_id())
    code = arg.strip().upper()
    now = time.monotonic()

    if not code:
        # 第一段：发确认码，不执行任何删除；顺手清掉过期的待确认项
        for uid in [k for k, v in _pending_clear.items() if v[1] < now]:
            _pending_clear.pop(uid, None)
        pending = secrets.token_hex(3).upper()
        _pending_clear[user_id] = (pending, now + _CLEAR_TTL)
        await matcher.finish(
            f"这会清空你的**全部**长期记忆（{_CLEAR_TTL:.0f} 秒内有效）。\n"
            f"确认请发送：/memory clear {pending}\n"
            "（不想清空就不回复，直接忽略本条消息）"
        )

    entry = _pending_clear.get(user_id)
    if entry is None or entry[0] != code:
        await matcher.finish("确认码无效，未执行清空。")
    if entry[1] < now:
        _pending_clear.pop(user_id, None)
        await matcher.finish("确认码已过期，未执行清空。")
    # L11：确认码在存储失败时保留（TTL 内可重试），成功后再消费
    removed = await store.clear_facts(user_id)
    _pending_clear.pop(user_id, None)
    logger.info("memory clear: uid=%s removed=%d", user_id, removed)
    await matcher.finish(
        f"已清空 {removed} 条长期记忆。" if removed else "没有可清空的长期记忆。"
    )


@memory_cmd.handle()
async def handle_memory(event: MessageEvent):
    if not is_allowed(event):
        await deny(memory_cmd, event, "无权限")
    store = _get_store()
    if store is None:
        await memory_cmd.finish("记忆功能未初始化。")

    action, arg = parse_memory_cmd(str(event.get_message()))
    try:
        if action == "list":
            await _list_facts(
                memory_cmd, event, store, await _current_session(store, event)
            )
        if action == "forget":
            await _forget_fact(
                memory_cmd,
                event,
                store,
                arg,
                await _current_session(store, event),
            )
        if action == "clear":
            await _clear_facts(memory_cmd, event, store, arg)
        await memory_cmd.finish(_USAGE)
    except FinishedException:
        raise
    except Exception:
        # finish 抛 FinishedException 会被 NoneBot 正常处理；这里只兜真正异常，
        # 不能让记忆管理故障污染普通聊天链路
        logger.exception("memory command failed")
        await memory_cmd.finish("操作失败，请稍后再试。")
