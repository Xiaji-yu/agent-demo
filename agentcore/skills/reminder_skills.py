"""提醒类 skill：让模型能帮用户登记/查看/取消提醒。

时间解析是确定性的（agentcore/scheduler/reminder.py），模型只负责把用户原话
（如「每天早上8点」）透传过来，不自己算时间。
"""
from __future__ import annotations

import logging
import time

from agentcore.scheduler.reminder import parse_when

logger = logging.getLogger(__name__)


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "-"
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def register_reminder_skills(registry, store, sink) -> None:
    """注册提醒技能；store 用于持久化，sink 用于投递。"""

    @registry.register(
        "reminder_add",
        "登记提醒，到点主动发消息给用户。when 支持：10分钟后 / 明天8点 / 每天9点 / "
        "每周一 9点 / 工作日 9点 / 9月12日 8点。text 是提醒内容（如「吃药」「开会」）。",
        {
            "type": "object",
            "properties": {
                "when": {"type": "string", "description": "什么时候提醒，用用户原话，如 明天早上8点"},
                "text": {"type": "string", "description": "提醒内容"},
            },
            "required": ["when", "text"],
        },
        permission="public",
    )
    async def reminder_add_skill(when: str, text: str, user_id: str = "", group_id: str | None = None) -> str:
        text = (text or "").strip()
        if not text:
            return "错误：请说明提醒内容"
        if len(text) > 200:
            return "错误：提醒内容过长（最多 200 字）"
        parsed = parse_when(when)
        if not parsed.get("ok"):
            return f"没能理解时间「{when}」：{parsed.get('error')}"
        target = f"group:{group_id}" if group_id else f"private:{user_id}"
        if not user_id and not group_id:
            return "错误：拿不到会话信息，无法登记提醒"
        sid = await store.schedule_add(
            kind=parsed["kind"],
            target=target,
            message=text,
            user_id=user_id or "",
            cron=parsed.get("cron") or "",
            next_run=parsed["run_at"],
        )
        return f"已登记提醒 #{sid}：{parsed['human']} → {text}"

    @registry.register(
        "reminder_list",
        "查看当前还没有触发的提醒（含一次性与周期提醒）。",
        {"type": "object", "properties": {}},
        permission="public",
    )
    async def reminder_list_skill(user_id: str = "") -> str:
        rows = await store.schedule_list(user_id or None)
        rows = [r for r in rows if not user_id or r["user_id"] == user_id]
        if not rows:
            return "当前没有待触发的提醒。"
        lines = [f"待触发提醒（{len(rows)} 条）："]
        for r in rows:
            kind = "周期" if r["kind"] == "cron" else "一次"
            lines.append(f"- #{r['id']} [{kind}] {_fmt_ts(r['next_run'])} → {r['message']}")
        return "\n".join(lines)

    @registry.register(
        "reminder_cancel",
        "取消一条提醒，需要提供 reminder_list 里看到的编号 id。",
        {
            "type": "object",
            "properties": {"schedule_id": {"type": "string", "description": "提醒编号，如 3"}},
            "required": ["schedule_id"],
        },
        permission="public",
    )
    async def reminder_cancel_skill(schedule_id: str, user_id: str = "") -> str:
        ok = await store.schedule_cancel(str(schedule_id), user_id or None)
        return f"已取消提醒 #{schedule_id}" if ok else f"没找到可取消的提醒 #{schedule_id}（或它不属于你）"
