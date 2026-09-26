"""进程内统一的「今天」口径（审查 P2 正确性收敛）。

历史现状：push 日键/正文锚点写 Asia/Shanghai、budget 用 date.today()（服务器
本地时区）、reminder cron 取宿主机 TZ——TZ=UTC 的容器里同一个进程存在三种
「一天」。这里提供唯一实现：默认 Asia/Shanghai（产品用户群体决定），
``AGENT_SCHEDULER_TZ`` 可整体覆盖（值为 IANA 名称或 ``local``）。
ZoneInfo 缺失（无 tzdata）时回落服务器本地时区——功能可用性优先于口径，
但要**告警**而不是像旧实现那样静默（`dt.ZoneInfo` 属性不存在的 AttributeError
曾被 except Exception 吞成"永远走本地时钟"，修好的日键其实是 no-op）。
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import time

logger = logging.getLogger(__name__)

_DEFAULT_TZ_NAME = "Asia/Shanghai"


def _tz_name() -> str:
    raw = (os.getenv("AGENT_SCHEDULER_TZ") or "").strip()
    return raw or _DEFAULT_TZ_NAME


def zoneinfo() -> dt.tzinfo:
    """统一时区：env 覆盖 > Asia/Shanghai > 服务器本地（带一次性告警）。"""
    name = _tz_name()
    if name.lower() == "local":
        return dt.datetime.now().astimezone().tzinfo
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception as e:
        logger.warning(
            "AGENT_SCHEDULER_TZ=%r 不可用（%s），回退服务器本地时区；"
            "日界/定时语义将随宿主机 TZ 漂移",
            name,
            type(e).__name__,
        )
        return dt.datetime.now().astimezone().tzinfo


def now() -> dt.datetime:
    """统一口径的「现在」（tz-aware）。"""
    return dt.datetime.now(zoneinfo())


def today_date() -> dt.date:
    """统一口径的「今天」（date 类型，budget 月/日键用）。"""
    return now().date()


def day_key(ts: float | None = None) -> str:
    """统一口径的 YYYY-MM-DD 日键（budget 日界 / push 每日上限共用）。"""
    if ts is None:
        moment = now()
    else:
        moment = dt.datetime.fromtimestamp(ts, zoneinfo())
    return moment.strftime("%Y-%m-%d")


def today_anchor() -> str:
    """给 LLM 的时效锚点（与日键同源，杜绝「正文说今天、闸门算昨天」）。"""
    moment = now()
    return f"今天是 {moment.year} 年 {moment.month} 月 {moment.day} 日。"


def monotonic_like_ts() -> float:
    """time.time() 的语义别名：day_key(ts) 的调用方在无 ts 时可直接不传。"""
    return time.time()
