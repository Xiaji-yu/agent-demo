"""榜单计算：周窗口径 + Top N / 新上榜 / 名次变化 / 最热时段。

周窗以**周一为一周起点**（与产品截图 10/5~10/11 的周一口径一致），全部按
进程统一时区（agentcore.tz，AGENT_SCHEDULER_TZ 可覆盖）取日界——与预算日键、
reminder cron 同源，杜绝 TZ=UTC 部署下"榜算上周、推送算上上周"。

本模块是纯函数（数据进、结构出），不碰存储与渲染。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from agentcore import tz


def week_bounds(day: dt.date) -> tuple[dt.date, dt.date]:
    """day 所在周的 (周一, 周日)。"""
    monday = day - dt.timedelta(days=day.weekday())
    return monday, monday + dt.timedelta(days=6)


def previous_week(now: dt.date | None = None) -> tuple[dt.date, dt.date]:
    """上一个**完整**周（周一~周日）。今天无论周几都取已完成的那周。"""
    today = now or tz.today_date()
    monday, _ = week_bounds(today)
    start = monday - dt.timedelta(days=7)
    return start, start + dt.timedelta(days=6)


def current_week(now: dt.date | None = None) -> tuple[dt.date, dt.date]:
    """本周（周一 ~ 今天）——进行中的周，用于「发言榜 本周」。"""
    today = now or tz.today_date()
    monday, _ = week_bounds(today)
    return monday, today


def fmt_range(start: dt.date, end: dt.date) -> str:
    """截图口径的区间标题：10/5 ~ 10/11。"""
    return f"{start.month}/{start.day} ~ {end.month}/{end.day}"


@dataclass(frozen=True)
class Entry:
    """榜单一行。delta 为名次变化（正=上升）；None 表示上周未上榜（NEW）。"""

    rank: int
    user_id: str
    nickname: str
    msgs: int
    chars: int
    pct: float
    is_new: bool
    delta: int | None


def build_leaderboard(
    current: list[dict],
    previous: list[dict],
    top_n: int,
    total: int | None = None,
) -> tuple[list[Entry], int]:
    """由两个窗口的 user_ranking 结果生成榜单。

    current/previous: [{"user_id","msgs","chars"}]（已按 msgs 降序）。
    ``total`` = 窗口内**全部群友**条数（百分比分母）。调用方只拿到 Top N 行，
    分母必须另查（store.window_total）；不传则退回 current 之和（测试/降级用）。
    返回 (entries, total)。top_n<=0 → 空列表（铁律：非正 limit=空结果）。
    """
    if top_n <= 0:
        return [], 0
    if total is None:
        total = sum(int(r.get("msgs") or 0) for r in current)
    prev_rank = {str(r["user_id"]): i + 1 for i, r in enumerate(previous)}
    prev_top = set(list(prev_rank)[:top_n])
    entries: list[Entry] = []
    for i, row in enumerate(current[:top_n]):
        uid = str(row["user_id"])
        msgs = int(row.get("msgs") or 0)
        rank = i + 1
        last = prev_rank.get(uid)
        entries.append(
            Entry(
                rank=rank,
                user_id=uid,
                nickname="",
                msgs=msgs,
                chars=int(row.get("chars") or 0),
                pct=(msgs / total * 100) if total else 0.0,
                is_new=uid not in prev_top,
                delta=(last - rank) if last is not None else None,
            )
        )
    return entries, total


def peak_hour(hist: dict[int, int]) -> tuple[int, int] | None:
    """直方图 → (小时, 条数)；全零/空返回 None。"""
    if not hist:
        return None
    hour = max(hist.items(), key=lambda kv: (kv[1], -kv[0]))[0]
    return hour, int(hist[hour])


def fmt_peak(peak: tuple[int, int] | None) -> str:
    """截图口径：19:00 前后（430 条）。"""
    if not peak:
        return "数据不足"
    hour, count = peak
    return f"{hour:02d}:00 前后（{count} 条）"
