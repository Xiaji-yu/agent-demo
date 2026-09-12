"""定时提醒：自然语言时间解析 + 到点投递。

支持的说法（中文口语为主）：
- 一次性：`10分钟后`、`2小时后`、`明天 8点`、`今天 21:30`、`9月12日 9点`、`2026-09-12 08:00`、`9点`（已过则顺延到明天）
- 周期性：`每天9点`、`每天早上8点`、`每周一 9:00`、`工作日 9点`

设计要点：
- 解析是**确定性**的（不依赖模型算时间），解析失败会把可用说法回给模型/用户
- 周期性提醒的「下次时间」用 apscheduler 的 CronTrigger 计算，避免自己实现 cron
- 投递失败不丢弃：顺延重试（机器人当时没连接也不至于把提醒弄丢）
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import time

logger = logging.getLogger(__name__)

# 注意：apscheduler 的 day_of_week 以 0=周一 … 6=周日（不是传统 cron 的 0=周日）
_WEEK_MAP = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
    "1": 0,
    "2": 1,
    "3": 2,
    "4": 3,
    "5": 4,
    "6": 5,
    "7": 6,
}
_PERIOD_HINT = {
    "凌晨": 0,
    "早上": 0,
    "上午": 0,
    "中午": 12,
    "下午": 12,
    "傍晚": 12,
    "晚上": 12,
    "夜里": 12,
}

_TIME_RE = re.compile(
    r"(凌晨|早上|上午|中午|下午|傍晚|晚上|夜里)?\s*(\d{1,2})\s*(?:[点:：时])\s*(\d{1,2})?\s*分?"
)
_OFFSET_RE = re.compile(r"(\d+)\s*(秒|分钟|分|小时|钟头|天|日)\s*(?:后|之后|以后)")
_COMPOUND_OFFSET_RE = re.compile(
    r"(?:(\d+)\s*(?:小时|钟头))?\s*(?:(\d+)\s*(?:分钟|分))?\s*(?:后|之后|以后)"
)
_DATE_RE = re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})")
_MD_RE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_DAILY_RE = re.compile(r"(每天|每日|天天)")
_WEEKLY_RE = re.compile(r"每?周\s*([一二三四五六日天1-7])")
_WEEKDAY_RE = re.compile(r"(工作日|周一至周五|每个工作日)")


def _hour_minute(
    text: str, default_hour: int = 9, default_minute: int = 0
) -> tuple[int, int]:
    """从文本里取「几点几分」；带 上午/下午 等修饰时换算成 24 小时制。"""
    m = _TIME_RE.search(text or "")
    if not m:
        return default_hour, default_minute
    hint, hh, mm = m.group(1), int(m.group(2)), int(m.group(3) or 0)
    if not m.group(3) and "半" in (text or "")[m.end() : m.end() + 2]:
        mm = 30
    if hint and hh < 12:
        if hint == "中午":
            hh = 12
        elif hint in _PERIOD_HINT and _PERIOD_HINT[hint] == 12:
            hh += 12
    if hh > 23 or mm > 59:
        raise ValueError(f"时间不合法：{m.group(0).strip()}")
    return hh, mm


def next_cron_time(cron: str, after_ts: float | None = None) -> float | None:
    """用 apscheduler 计算 cron 的下次触发时间戳；表达式非法返回 None。"""
    try:
        from apscheduler.triggers.cron import CronTrigger
    except Exception:  # pragma: no cover
        return None
    tz = dt.datetime.now().astimezone().tzinfo
    try:
        trigger = CronTrigger.from_crontab(cron, timezone=tz)
    except Exception:
        logger.warning("invalid cron: %r", cron)
        return None
    base = dt.datetime.fromtimestamp(after_ts or time.time(), tz=tz)
    nxt = trigger.get_next_fire_time(None, base)
    return nxt.timestamp() if nxt else None


def parse_when(text: str, now: dt.datetime | None = None) -> dict:
    """解析提醒时间。返回 {ok, kind, run_at, cron, human, error}。"""
    now = now or dt.datetime.now()
    raw = (text or "").strip()
    if not raw:
        return {"ok": False, "error": "没有说明时间，例如「10分钟后」「每天9点」"}

    # 1a) 复合时长：2小时30分钟后 / 1小时15分后
    cm = _COMPOUND_OFFSET_RE.search(raw)
    if cm and (cm.group(1) or cm.group(2)):
        hours = int(cm.group(1) or 0)
        minutes = int(cm.group(2) or 0)
        run_at = now + dt.timedelta(hours=hours, minutes=minutes)
        label = "".join(
            filter(
                None,
                [f"{hours}小时" if hours else "", f"{minutes}分钟" if minutes else ""],
            )
        )
        return _once(run_at, f"{label}后（{run_at.strftime('%m-%d %H:%M')}）", now)

    # 1b) 相对时间：N秒/分/小时/天后
    m = _OFFSET_RE.search(raw)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit in ("天", "日") and _TIME_RE.search(raw):
            # 「3天后 8点」：日期按天数推，时刻用指定的
            hh, mm = _hour_minute(raw)
            run_at = dt.datetime.combine(
                (now + dt.timedelta(days=n)).date(), dt.time(hh, mm)
            )
        else:
            seconds = {
                "秒": 1,
                "分钟": 60,
                "分": 60,
                "小时": 3600,
                "钟头": 3600,
                "天": 86400,
                "日": 86400,
            }[unit]
            run_at = now + dt.timedelta(seconds=n * seconds)
        return _once(run_at, f"{n}{unit}后（{run_at.strftime('%m-%d %H:%M')}）", now)

    recurring = (
        bool(_DAILY_RE.search(raw))
        or bool(_WEEKLY_RE.search(raw))
        or bool(_WEEKDAY_RE.search(raw))
    )
    if recurring:
        try:
            hh, mm = _hour_minute(raw)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if _WEEKDAY_RE.search(raw):
            cron, human = f"{mm} {hh} * * 1-5", f"每个工作日 {hh:02d}:{mm:02d}"
        elif wm := _WEEKLY_RE.search(raw):
            dow = _WEEK_MAP.get(wm.group(1))
            if dow is None:  # 注意 0 = 周一，不能用真值判断
                return {"ok": False, "error": "星期写法无法识别"}
            names = {0: "一", 1: "二", 2: "三", 3: "四", 4: "五", 5: "六", 6: "日"}
            cron, human = f"{mm} {hh} * * {dow}", f"每周{names[dow]} {hh:02d}:{mm:02d}"
        else:
            cron, human = f"{mm} {hh} * * *", f"每天 {hh:02d}:{mm:02d}"
        nxt = next_cron_time(cron, now.timestamp())
        if nxt is None:
            return {"ok": False, "error": "周期表达式无法解析"}
        return {
            "ok": True,
            "kind": "cron",
            "cron": cron,
            "run_at": nxt,
            "human": f"{human}（下次 {dt.datetime.fromtimestamp(nxt).strftime('%m-%d %H:%M')}）",
        }

    # 2) 具体日期 + 可选时间
    day: dt.date | None = None
    if "今天" in raw or "今日" in raw:
        day = now.date()
    elif "后天" in raw:
        day = now.date() + dt.timedelta(days=2)
    elif "明天" in raw or "明日" in raw:
        day = now.date() + dt.timedelta(days=1)
    elif dm := _DATE_RE.search(raw):
        try:
            day = dt.date(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)))
        except ValueError as e:
            return {"ok": False, "error": f"日期不合法：{e}"}
    elif md := _MD_RE.search(raw):
        try:
            day = dt.date(now.year, int(md.group(1)), int(md.group(2)))
        except ValueError as e:
            return {"ok": False, "error": f"日期不合法：{e}"}
        if day < now.date():  # 只写月日且已过 → 理解为明年
            day = day.replace(year=now.year + 1)

    has_time = bool(_TIME_RE.search(raw))
    if day is not None:
        try:
            hh, mm = _hour_minute(raw)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        run_at = dt.datetime.combine(day, dt.time(hh, mm))
        if run_at <= now:
            # 用户明确指定了日期（含「今天」）却已过去：直接说清楚，不要偷偷改成明天
            return {
                "ok": False,
                "error": f"指定时间 {run_at.strftime('%m-%d %H:%M')} 已经过去了，请换个时间",
            }
        return _once(run_at, run_at.strftime("%m-%d %H:%M"), now)

    # 3) 只有时间点：今天该点，已过则明天
    if has_time:
        try:
            hh, mm = _hour_minute(raw)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        run_at = dt.datetime.combine(now.date(), dt.time(hh, mm))
        if run_at <= now:
            run_at += dt.timedelta(days=1)  # 没写日期时：今天已过 → 明天
        return _once(run_at, run_at.strftime("%m-%d %H:%M"), now)

    return {
        "ok": False,
        "error": "无法识别时间，可用：10分钟后 / 明天8点 / 每天9点 / 9月12日 9点",
    }


def _once(run_at: dt.datetime, human: str, now: dt.datetime | None = None) -> dict:
    base = (now or dt.datetime.now()).timestamp()
    return {
        "ok": True,
        "kind": "once",
        "cron": "",
        "run_at": run_at.timestamp(),
        "human": f"{human}（约 {_humanize_delta(run_at.timestamp() - base)}后）",
    }


def _humanize_delta(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分钟"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} 小时"
    return f"{seconds / 86400:.1f} 天"


class ReminderService:
    """到点检查并投递提醒。由调度器周期性调用 tick()。"""

    def __init__(self, store, sink, retry_delay: int = 120):
        self.store = store
        self.sink = sink
        self.retry_delay = retry_delay

    async def tick(self) -> dict:
        now = time.time()
        due = await self.store.schedule_due(now)
        sent = failed = 0
        for row in due:
            ok = await self.sink.send(row["target"], f"⏰ 提醒：{row['message']}")
            if not ok:
                # 机器人没连接/发送失败：顺延重试，别把提醒弄丢
                failed += 1
                logger.warning(
                    "reminder %s delivery failed, retry in %ss: %s",
                    row["id"],
                    self.retry_delay,
                    row["message"][:60],
                )
                await self.store.schedule_mark_fired(row["id"], now + self.retry_delay)
                continue
            sent += 1
            next_run = None
            if row["kind"] == "cron" and row["cron"]:
                next_run = next_cron_time(row["cron"], now)
            await self.store.schedule_mark_fired(row["id"], next_run)
        if due:
            logger.info("reminders: due=%d sent=%d failed=%d", len(due), sent, failed)
        return {"due": len(due), "sent": sent, "failed": failed}
