"""Web「定时」页的后端：cron job（jobs.json）与提醒（schedules 表）的表单化 CRUD。

分工：
- 纯逻辑（频率 → cron 表达式、校验、人话描述）不依赖 NoneBot，可直接单测；
- 落盘复用 ``agentcore.scheduler.jobs`` 的锁/原子写/改前备份；
- 鉴权与两段确认在 ``web.py``，这里只负责「给定已确认的 payload 写进去」。

cron 的星期字段按 **APScheduler** 口径：0=周一 … 6=周日（不是传统 cron 的
0=周日）。``agentcore/scheduler/reminder.py`` 的 ``_WEEK_MAP`` 同口径。
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any

from agentcore import tz

logger = logging.getLogger(__name__)

FREQS = {
    "once",
    "daily",
    "weekly",
    "weekdays",
    "weekends",
    "odd",
    "even",
    "monthly",
    # 编辑已存任务时回填用：jobs.json 只存 cron 表达式，反推不出原始频率，
    # 直接让管理员编辑表达式（新建表单不暴露这一项，避免绕开校验）。
    "custom",
}
KINDS = {"agent_turn", "reminder"}

_TARGET_RE = re.compile(r"^(private|group):([0-9]{5,12})$")
_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

_WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

_FREQ_LABELS = {
    "once": "一次",
    "daily": "每天",
    "weekly": "每周",
    "weekdays": "工作日",
    "weekends": "周末",
    "odd": "单日（日期为奇数）",
    "even": "双日（日期为偶数）",
    "monthly": "每月",
    "custom": "自定义",
}


class CronWebError(ValueError):
    """表单/落盘校验错误；消息可直接展示给管理员。"""


def _parse_hhmm(text: Any) -> tuple[int, int]:
    m = _HHMM_RE.match(str(text or "").strip())
    if not m:
        raise CronWebError("时间格式应为 HH:MM（24 小时制）")
    return int(m.group(1)), int(m.group(2))


def validate_target(text: Any) -> str:
    t = str(text or "").strip()
    if not _TARGET_RE.match(t):
        raise CronWebError("推送路由应为 private:<QQ号> 或 group:<群号>（纯数字）")
    return t


def _next_fire(cron: str, now: dt.datetime) -> dt.datetime:
    from apscheduler.triggers.cron import CronTrigger

    try:
        trigger = CronTrigger.from_crontab(cron, timezone=now.tzinfo)
        nxt = trigger.get_next_fire_time(None, now)
    except Exception as e:
        raise CronWebError(f"cron 表达式无法解析：{e}") from e
    if nxt is None:
        raise CronWebError("该 cron 表达式没有下一次触发时间")
    return nxt


def build_schedule(payload: dict, now: dt.datetime | None = None) -> dict:
    """把表单频率翻译成 (cron 表达式, 是否一次性, 下次触发时间)。"""
    freq = str(payload.get("freq") or "").strip().lower()
    if freq not in FREQS:
        raise CronWebError("频率不合法")
    now = now or tz.now()
    if freq == "custom":
        cron = str(payload.get("cron") or "").strip()
        if not cron:
            raise CronWebError("自定义 cron 表达式不能为空")
        return {
            "cron": cron,
            "delete_after_run": bool(payload.get("delete_after_run")),
            "next_run": _next_fire(cron, now),
        }
    hour, minute = _parse_hhmm(payload.get("time"))
    delete_after_run = False

    if freq == "once":
        raw_date = str(payload.get("date") or "").strip()
        try:
            day = dt.date.fromisoformat(raw_date)
        except ValueError as e:
            raise CronWebError("一次性任务需要 YYYY-MM-DD 格式的日期") from e
        fire_at = dt.datetime.combine(day, dt.time(hour, minute), tzinfo=now.tzinfo)
        if fire_at <= now:
            raise CronWebError("一次性任务的时间必须晚于当前时间")
        cron = f"{minute} {hour} {day.day} {day.month} *"
        delete_after_run = True
    elif freq == "daily":
        cron = f"{minute} {hour} * * *"
    elif freq == "weekly":
        days: list[int] = []
        for raw in payload.get("weekdays") or []:
            try:
                d = int(raw)
            except (TypeError, ValueError) as e:
                raise CronWebError("每周的星期取值应为 0–6") from e
            if not 0 <= d <= 6:
                raise CronWebError("每周的星期取值应为 0–6（0=周一，6=周日）")
            if d not in days:
                days.append(d)
        if not days:
            raise CronWebError("每周任务至少要选一天")
        cron = f"{minute} {hour} * * {','.join(str(d) for d in sorted(days))}"
    elif freq == "weekdays":
        cron = f"{minute} {hour} * * 0-4"
    elif freq == "weekends":
        cron = f"{minute} {hour} * * 5,6"
    elif freq == "odd":
        cron = f"{minute} {hour} 1-31/2 * *"
    elif freq == "even":
        cron = f"{minute} {hour} 2-30/2 * *"
    else:  # monthly
        try:
            day = int(payload.get("day") or 0)
        except (TypeError, ValueError) as e:
            raise CronWebError("每月日期应为 1–31") from e
        if not 1 <= day <= 31:
            raise CronWebError("每月日期应为 1–31")
        cron = f"{minute} {hour} {day} * *"

    return {
        "cron": cron,
        "delete_after_run": delete_after_run,
        "next_run": _next_fire(cron, now),
    }


def describe(item: dict) -> str:
    """给确认框/列表用的一句人话。"""
    freq = str(item.get("freq") or "")
    label = _FREQ_LABELS.get(freq, freq or "—")
    time_text = str(item.get("time") or "")
    if freq == "weekly":
        names = [
            _WEEKDAY_NAMES[int(d)]
            for d in (item.get("weekdays") or [])
            if 0 <= int(d) <= 6
        ]
        label += "（" + "、".join(names) + "）"
    elif freq == "monthly":
        label += f" {item.get('day')} 号"
    elif freq == "once":
        label += f" {item.get('date')}"
    return f"{label} {time_text}".strip()


def normalize_payload(payload: dict, superusers: set[str]) -> dict:
    """校验并归一化表单；返回可直接落盘的完整字段（也用于 challenge 绑定）。"""
    if not isinstance(payload, dict):
        raise CronWebError("payload 不是对象")
    kind = str(payload.get("kind") or "agent_turn").strip()
    if kind not in KINDS:
        raise CronWebError("任务类型不合法")

    name = str(payload.get("name") or "").strip()
    if not 1 <= len(name) <= 40:
        raise CronWebError("任务名称长度应为 1–40 字")

    message = str(payload.get("message") or "").strip()
    limit = 2000 if kind == "agent_turn" else 200
    if not message:
        raise CronWebError("任务内容不能为空")
    if len(message) > limit:
        raise CronWebError(f"任务内容最多 {limit} 字")

    target = validate_target(payload.get("target"))
    trusted = bool(payload.get("trusted"))
    owner = str(payload.get("owner") or "").strip()
    if trusted:
        if not owner or owner not in superusers:
            raise CronWebError("trusted 任务必须指定 SUPERUSERS 中的执行身份")
    elif not owner:
        owner = next(iter(sorted(superusers)), "")
    if kind == "reminder" and not owner:
        raise CronWebError("提醒需要一个归属管理员（SUPERUSERS 为空）")

    schedule = build_schedule(payload)
    # trusted 任务默认停用（与 /cron add 的 M7 两步确认一致）；普通任务
    # 默认启用。前端也会按这个规则初始化开关。
    enabled = payload.get("enabled")
    if enabled is None:
        enabled = not trusted
    enabled = bool(enabled)

    return {
        "id": str(payload.get("id") or "").strip(),
        "kind": kind,
        "name": name,
        "message": message,
        "target": target,
        "freq": str(payload.get("freq") or "").strip().lower(),
        "time": str(payload.get("time") or "").strip(),
        "date": str(payload.get("date") or "").strip(),
        "weekdays": list(payload.get("weekdays") or []),
        "day": payload.get("day"),
        "trusted": trusted,
        "owner": owner,
        "enabled": enabled,
        "cron": schedule["cron"],
        "delete_after_run": schedule["delete_after_run"],
        # JSON 里不塞 datetime（前端/confirm 绑定都要可序列化）
        "next_run_ts": schedule["next_run"].timestamp(),
        "next_run_text": tz.fmt_ts(schedule["next_run"].timestamp(), "%m-%d %H:%M"),
    }


# ---------------------------------------------------------------- 读取


async def list_all(store=None) -> dict:
    """jobs.json + 提醒列表；jobs 带 message 全文（编辑回填，鉴权面已登录）。"""
    from agentcore.scheduler import jobs as jobs_mod

    try:
        jobs, errors = jobs_mod.load()
    except jobs_mod.JobsError as e:
        raise CronWebError(f"cron 配置读取失败：{e}") from e

    out_jobs = []
    for j in jobs:
        d = j.public()
        d["message"] = j.message
        out_jobs.append(d)

    reminders: list[dict] = []
    if store is not None:
        try:
            rows = await store.schedule_list(None, include_disabled=True)
            reminders = [r for r in rows if (r.get("action") or "remind") == "remind"]
        except Exception:
            logger.warning("读取提醒列表失败", exc_info=True)
    return {"jobs": out_jobs, "reminders": reminders, "errors": errors}


# ---------------------------------------------------------------- 写入


def _unique_id(jobs: list, base: str) -> str:
    existing = {j.id for j in jobs}
    if base not in existing:
        return base
    n = 1
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def save_agent_turn(item: dict) -> str:
    from agentcore.scheduler import jobs as jobs_mod

    path = jobs_mod.jobs_path()
    with jobs_mod.write_lock(path):
        jobs, _ = jobs_mod.load(path)
        jid = item.get("id") or ""
        hit = next((j for j in jobs if j.id == jid), None) if jid else None
        if hit is None:
            if jid:
                raise CronWebError("任务不存在（可能已被删除），请刷新后重试")
            jid = _unique_id(jobs, jobs_mod._slug(item["name"]))
            hit = jobs_mod.CronJob(
                id=jid,
                name=item["name"],
                enabled=item["enabled"],
                cron=item["cron"],
                message=item["message"],
                to=item["target"],
                channel="onebot",
                tz=str(tz.zoneinfo()),
                delete_after_run=item["delete_after_run"],
                created_at_ms=0,
                owner=item["owner"],
                trusted=item["trusted"],
            )
            jobs.append(hit)
        else:
            hit.name = item["name"]
            hit.enabled = item["enabled"]
            hit.cron = item["cron"]
            hit.message = item["message"]
            hit.to = item["target"]
            hit.tz = str(tz.zoneinfo())
            hit.delete_after_run = item["delete_after_run"]
            hit.owner = item["owner"]
            hit.trusted = item["trusted"]
        jobs_mod.save(jobs, path)
        # 显式写下次时间：否则 sidecar 里没有该 id 时 nextRunAtMs=0，tick 会把
        # "从未跑过"当"立即跑一次"（现有 /cron add 的已知行为；Web 表单不该
        # 在保存瞬间触发一次投递）。
        jobs_mod.set_next_run(jid, int(item["next_run_ts"] * 1000), path)
    return jid


async def save_reminder(item: dict, store) -> str:
    if item.get("id"):
        # 提醒没有 update 接口：编辑 = 取消旧的再建一条（id 会变）。
        await store.schedule_cancel(item["id"], user_id=None)
    return await store.schedule_add(
        kind="once" if item["freq"] == "once" else "cron",
        target=item["target"],
        message=item["message"],
        user_id=item["owner"],
        cron="" if item["freq"] == "once" else item["cron"],
        next_run=item["next_run_ts"],
        action="remind",
    )


def toggle_agent_turn(job_id: str, enabled: bool) -> None:
    from agentcore.scheduler import jobs as jobs_mod

    path = jobs_mod.jobs_path()
    with jobs_mod.write_lock(path):
        jobs, _ = jobs_mod.load(path)
        hit = next((j for j in jobs if j.id == job_id), None)
        if hit is None:
            raise CronWebError("任务不存在（可能已被删除）")
        hit.enabled = enabled
        jobs_mod.save(jobs, path)


def delete_agent_turn(job_id: str) -> None:
    from agentcore.scheduler import jobs as jobs_mod

    path = jobs_mod.jobs_path()
    with jobs_mod.write_lock(path):
        jobs, _ = jobs_mod.load(path)
        kept = [j for j in jobs if j.id != job_id]
        if len(kept) == len(jobs):
            raise CronWebError("任务不存在（可能已被删除）")
        jobs_mod.save(kept, path)


def run_agent_turn(job_id: str) -> str:
    """立即试跑一次（结果仍投递到任务自己的路由）。"""
    import asyncio

    from agentcore.scheduler import cron_admin
    from agentcore.scheduler import jobs as jobs_mod

    runner = cron_admin._RUNNER_REF[0]
    if runner is None:
        raise CronWebError("试跑器未接线（bot 尚未完成启动）")
    jobs, _ = jobs_mod.load()
    hit = next((j for j in jobs if j.id == job_id), None)
    if hit is None:
        raise CronWebError("任务不存在（可能已被删除）")
    asyncio.get_running_loop().create_task(runner.run_job(hit))
    return f"试跑已发起：{job_id}"
