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

# 周一开头（与 utility_skills.now_text 的星期表同口径，中文用户认知）
_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _tz_name() -> str:
    raw = (os.getenv("AGENT_SCHEDULER_TZ") or "").strip()
    return raw or _DEFAULT_TZ_NAME


def _local_tz() -> dt.tzinfo:
    # astimezone().tzinfo 理论上恒非 None；typeshed 标 Optional，兜底到 UTC
    local = dt.datetime.now().astimezone().tzinfo
    return local if local is not None else dt.UTC


# 已告警过的坏时区名。M11（REVIEW-4d5c1b8..63f9444）：_history_time_prefix 对每条
# 历史消息要调 3 次 zoneinfo()，无去重时 AGENT_SCHEDULER_TZ 写错（或镜像缺
# tzdata）能在一轮对话里刷出数百条同文 WARNING，把真正的告警埋掉——docstring
# 承诺的「一次性告警」此前并没有实现。
_warned_bad_tz: set[str] = set()


def zoneinfo() -> dt.tzinfo:
    """统一时区：env 覆盖 > Asia/Shanghai > 服务器本地（带一次性告警）。"""
    name = _tz_name()
    if name.lower() == "local":
        return _local_tz()
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception as e:
        if name not in _warned_bad_tz:
            _warned_bad_tz.add(name)
            logger.warning(
                "AGENT_SCHEDULER_TZ=%r 不可用（%s），回退服务器本地时区；"
                "日界/定时语义将随宿主机 TZ 漂移",
                name,
                type(e).__name__,
            )
        return _local_tz()


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
    """给 LLM 的时效锚点（与日键同源，杜绝「正文说今天、闸门算昨天」）。

    **日期与时刻都要给**：只给日期时，模型对「现在几点 / 离 14:30 还有多久 /
    今天剩多少时间」没有任何依据可算（2026-10-26 线上：凌晨 3:24 对「每天
    14:30 推送」回复「还有十几分钟」，实际差 11 小时）。时刻与日期同源
    （agentcore.tz），每轮 prompt 组装时现取；tool-loop 内需要更精确的时刻时
    模型应调用 now skill。
    """
    moment = now()
    return (
        f"今天是 {moment.year} 年 {moment.month} 月 {moment.day} 日"
        f"（{_WEEKDAYS[moment.weekday()]}），"
        f"现在 {moment.strftime('%H:%M')}。"
    )


def monotonic_like_ts() -> float:
    """time.time() 的语义别名：day_key(ts) 的调用方在无 ts 时可直接不传。"""
    return time.time()


def fmt_ts(ts: float, pattern: str = "%m-%d %H:%M") -> str:
    """统一口径的时间**显示**（与 day_key 同时区，替代各处 time.localtime）。

    端口性评审 P3：显示层曾直接用 ``time.localtime``——宿主机 TZ 与
    ``AGENT_SCHEDULER_TZ`` 不一致时（如 UTC 服务器 + 上海日键），列表里看到的
    时间与「统一的今天」差几个小时，用户对不上账。所有给用户看的时间戳都走这里。
    """
    return dt.datetime.fromtimestamp(ts, zoneinfo()).strftime(pattern)


def format_duration(seconds: float) -> str:
    """耗时的**日志**显示：按量级分档，避免满屏 ``0.0001`` / ``123.4567``。

    ``0.42s`` / ``12.35s`` / ``1m05s`` / ``1h02m05s``。耗时差值一律来自
    ``time.monotonic()``（墙上时钟跳变/回拨时可能出负数），负值显示
    ``0.00s``——日志里的负数只会误导排查。
    """
    try:
        total = max(0.0, float(seconds))
    except (TypeError, ValueError):
        return "0.00s"
    if total == float("inf"):
        # L5（REVIEW-6745556..d730827）：inf 过了 max 但 int(inf) 抛
        # OverflowError——调用点全是 monotonic 差值不可达，纯健壮性兜底
        return "0.00s"
    if total < 60:
        return f"{total:.2f}s"
    whole = int(total)
    if whole < 3600:
        return f"{whole // 60}m{whole % 60:02d}s"
    return f"{whole // 3600}h{whole % 3600 // 60:02d}m{whole % 60:02d}s"
