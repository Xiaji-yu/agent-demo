"""启动后认账：把上次停机前「结果不确定」的主动投递通知主人。

由来（对照 dsh-superagent 的 receipts/outbox）：那边出站有持久回执，重启后逐条
认账。本仓出站铁律是「uncertain 绝不重发」，所以这里**只通知、不补发**——
通知让运维知道「这条悬空了」，补发则会让用户收到两遍（AGENTS.md 出站铁律）。

接线同 ``reboot.py:458``：模块导入时注册 ``on_bot_connect``，由
``_load_plugin_modules()`` 隔离装载（装载失败只降级该通知，不影响 bot 本体）。
认账是一次性的——``outbox.take_uncertain`` 读走即删，重连不会重复打扰。
"""

from __future__ import annotations

import logging

from agentcore import outbox, tz
from agentcore.workspace.utils import load_superusers

logger = logging.getLogger(__name__)

# 通知里最多列几条明细，其余折叠成「…等共 N 条」（提醒主人的同时不刷屏）。
_MAX_LISTED = 5


def _fmt_ts(value: object) -> str:
    try:
        return tz.fmt_ts(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "?"


def _fmt_entry(entry: dict) -> str:
    ref = str(entry.get("ref") or "").strip()
    target = str(entry.get("target") or "?")
    detail = str(entry.get("detail") or "").strip()
    head = f"{ref} → {target}" if ref else target
    return f"• {_fmt_ts(entry.get('ts'))} {head}" + (f"（{detail}）" if detail else "")


def pending_notice_text(entries: list[dict]) -> str:
    """悬空投递清单 → 通知正文（纯函数，便于用例锁定文案）。"""
    lines = [_fmt_entry(e) for e in entries[-_MAX_LISTED:]]
    if len(entries) > _MAX_LISTED:
        lines.append(f"…等共 {len(entries)} 条")
    return (
        f"☁️ 启动自检：上次停机前有 {len(entries)} 条主动投递结果不确定"
        "（已按铁律不重发，请知悉）：\n" + "\n".join(lines)
    )


async def _send_pending_notice(bot) -> None:
    """bot 连上后一次性认账：读走 uncertain 行并私聊通知每个管理员。

    任何失败只记日志——通知是增益，不能反噬刚起来的 bot。
    """
    entries = outbox.take_uncertain()
    if not entries:
        return
    admins = sorted(load_superusers())
    if not admins:
        logger.warning(
            "outbox: 有 %d 条悬空投递但未配置 SUPERUSERS，无法通知", len(entries)
        )
        return
    text = pending_notice_text(entries)
    for uid in admins:
        if not uid.isascii() or not uid.isdigit():
            logger.warning("outbox: 管理员 id 形态不合法，跳过：%r", uid)
            continue
        try:
            await bot.send_private_msg(user_id=int(uid), message=text)
        except Exception:
            logger.exception("outbox: 悬空投递通知发送失败：%s", uid)
    logger.warning("outbox: 已通知管理员 %d 条悬空投递", len(entries))


from nonebot import get_driver  # noqa: E402

get_driver().on_bot_connect(_send_pending_notice)
