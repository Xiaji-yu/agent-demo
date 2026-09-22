"""戳一戳（notice: notify/poke）→ 回一张当前机器的概览图。

为什么单独一个模块：这是**通知事件**，与 ``matcher.py`` 的消息链路（唤醒词、
防抖、LLM turn）毫无关系——不跑引擎、不占防抖窗口、不写历史。混进 matcher
只会让那条链路的 rule 与状态机更难读。

行为
----
- 只响应「戳的对象是 bot 自己」的 poke（``PokeNotifyEvent.is_tome``）；bot 自己
  戳别人产生的回传事件（``user_id == self_id``）直接忽略，避免自激。
- **只给 superuser**，且群聊还必须在 ``ALLOWED_GROUPS`` 白名单里
  （``acl.is_strict_allowed``）。未授权时**静默**——在别人的群里回一句
  「你没权限」等于把机器信息的存在性也暴露了，而且刷屏。
- 取数走看板只读 API（``agentcore.dashboard``），**本机用 Pillow 重绘**概览页
  （``agentcore.render.overview``），不依赖浏览器。
- 默认 30 秒冷却/会话（``AGENT_POKE_COOLDOWN``），免得被连点刷屏；渲染是同步
  CPU，放 ``asyncio.to_thread`` 里，不阻塞事件循环。
- 渲染失败（无中文字体等）降级为一行文本摘要，绝不发豆腐块图；取数失败给一句
  人话原因。

env
---
- ``AGENT_POKE_ENABLED``：``0`` 关闭（默认开启）。
- ``AGENT_POKE_COOLDOWN``：同会话冷却秒数，``0`` = 不限（默认 30）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from nonebot import on_notice
from nonebot.adapters.onebot.v11 import PokeNotifyEvent

from agentcore.dashboard import DashboardError, load_from_env
from agentcore.render.overview import render_overview_png

from .acl import is_strict_allowed

logger = logging.getLogger(__name__)

_TRUE = {"1", "true", "yes", "on"}

# 同会话冷却：{chat_key: 上次成功响应时刻（monotonic）}
_last_reply: dict[str, float] = {}
_clock = time.monotonic  # 测试可注入


def enabled() -> bool:
    """``AGENT_POKE_ENABLED`` 显式设了值（含空串）就按值生效——与 help_render 同款：
    ``AGENT_POKE_ENABLED=`` 是"设了但无效"，按关闭处理，不会被 `or "1"` 变成开启。"""
    raw = os.getenv("AGENT_POKE_ENABLED")
    if raw is None:
        return True
    return raw.strip().lower() in _TRUE


def cooldown_seconds() -> float:
    """冷却秒数；脏值告警后回退 30，负值按 0（不限）。"""
    raw = (os.getenv("AGENT_POKE_COOLDOWN") or "").strip()
    if not raw:
        return 30.0
    try:
        value = float(raw)
    except ValueError:
        logger.warning("AGENT_POKE_COOLDOWN=%r 不是数字，回退 30s", raw)
        return 30.0
    return max(0.0, value)


def _is_self_poke(event: PokeNotifyEvent) -> bool:
    """bot 自己戳别人也会作为 notice 回传（协议端开启上报），必须忽略。"""
    self_id = str(getattr(event, "self_id", "") or "")
    return bool(self_id) and str(event.get_user_id()) == self_id


def _poke_rule(event) -> bool:
    return isinstance(event, PokeNotifyEvent) and not _is_self_poke(event)


poke_matcher = on_notice(rule=_poke_rule, priority=5, block=False)


def _chat_key(event: PokeNotifyEvent) -> str:
    group_id = getattr(event, "group_id", None)
    if group_id is not None:
        return f"group:{group_id}"
    return f"private:{event.get_user_id()}"


def _cooling(key: str) -> bool:
    """冷却期内返回 True（并记日志）；否则刷新时间戳。"""
    cd = cooldown_seconds()
    if cd <= 0:
        return False
    now = _clock()
    last = _last_reply.get(key)
    if last is not None and now - last < cd:
        logger.info("戳一戳忽略：%s 冷却中（剩余 %.1fs）", key, cd - (now - last))
        return True
    _last_reply[key] = now
    return False


async def _reply(event: PokeNotifyEvent, message) -> None:
    """按事件所属会话发送（过与文本回复同一进程级节流）。

    用 ``get_bot(self_id)`` 而不是 matcher 自带的 ``send``：戳一戳是后台性质的
    通知事件，多账号部署下必须回到**触发它的那个 bot**，否则会串号。
    """
    from nonebot import get_bot

    from .outbound import default_throttle

    group_id = getattr(event, "group_id", None)
    kind = "group" if group_id is not None else "private"
    ident = int(group_id) if group_id is not None else int(event.get_user_id())
    bot = get_bot(str(getattr(event, "self_id", "") or "") or None)
    await default_throttle().acquire(f"{kind}:{ident}")
    if kind == "group":
        await bot.send_group_msg(group_id=ident, message=message)
    else:
        await bot.send_private_msg(user_id=ident, message=message)


def _safe_number(value: Any, default: float = 0.0) -> float:
    """``float()`` 的容错版：字段可能是 None/字符串/脏值，降级路径绝不能因此再炸。"""
    if isinstance(value, bool) or value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _fallback_text(snapshot) -> str:
    """渲染不可用时的文本降级：一行关键指标（信息在，只是不美观）。"""
    ov = snapshot.get("overview") or {}
    cpu = ov.get("cpu") or {}
    mem = ov.get("memory") or {}
    temp = ov.get("temp") or {}
    disk = ov.get("disk") or {}
    parts = [f"本机概览｜{ov.get('host') or '未知主机'}"]
    if cpu.get("available"):
        parts.append(f"CPU {_safe_number(cpu.get('percent')):.0f}%")
    if mem.get("available"):
        parts.append(
            f"内存 {_safe_number(mem.get('used_gb')):.1f}/"
            f"{_safe_number(mem.get('total_gb')):.1f} GB"
        )
    if temp.get("available"):
        parts.append(f"温度 {_safe_number(temp.get('celsius')):.0f}°C")
    if disk.get("available"):
        parts.append(f"磁盘剩余 {_safe_number(disk.get('free_gb')):.0f} GB")
    return "｜".join(parts) + "\n（概览图渲染不可用，已退回文本）"


async def _safe_reply(event: PokeNotifyEvent, message) -> None:
    """发送失败只记日志。

    戳一戳是后台性质的通知处理，**任何发送异常都不该再往外冒**——否则会在日志里
    留下与业务无关的 NoneBot 报错栈；而且发送失败（超时/断连）结果不确定，
    **绝不重发**（与 outbound 的出站口径一致：宁可少发一次，不要重复刷屏）。
    """
    try:
        await _reply(event, message)
    except Exception:
        logger.warning("戳一戳回复发送失败（不重发）", exc_info=True)


@poke_matcher.handle()
async def handle_poke(event: PokeNotifyEvent):
    if not enabled():
        return
    if not event.is_tome():
        # 群里 A 戳 B 也会上报，只有戳 bot 才响应
        return
    if not is_strict_allowed(event):
        # 静默：不回话（连"有权限要求"都不必说给无关的人听）
        logger.debug("戳一戳忽略：%s 未授权", event.get_user_id())
        return
    key = _chat_key(event)
    if _cooling(key):
        return

    client = load_from_env()
    try:
        if not client.token:
            await _safe_reply(
                event, "未配置看板只读令牌（AGENT_DASHBOARD_TOKEN），无法取本机概览。"
            )
            return
        snapshot = await client.snapshot()
        if not snapshot.get("overview", {}).get("ready"):
            await _safe_reply(event, "看板刚启动，采样还没就绪，稍后再戳我一下。")
            return
        png = await asyncio.to_thread(render_overview_png, snapshot)
        if png is None:
            await _safe_reply(event, _fallback_text(snapshot))
            return
        from nonebot.adapters.onebot.v11 import MessageSegment

        await _safe_reply(event, MessageSegment.image(png))
        logger.info("[poke] %s | %d bytes", key, len(png))
    except DashboardError as exc:
        logger.warning("戳一戳取数失败：%s", exc)
        await _safe_reply(event, f"取本机概览失败：{exc}")
    except Exception:
        logger.exception("戳一戳处理异常")
        await _safe_reply(event, "本机概览生成失败，请稍后再试（详见 bot 日志）。")
    finally:
        # aclose 放在 finally 里是为了"未配置令牌提前 return"也不泄漏连接池；
        # 它自己再抛异常会把上面真正的失败原因顶掉，所以同样要吞。
        try:
            await client.aclose()
        except Exception:
            logger.warning("看板客户端关闭失败", exc_info=True)
