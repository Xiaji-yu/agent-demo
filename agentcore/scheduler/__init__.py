"""定时任务（M7 最小实现）：目前只用于「每天从记忆蒸馏知识入库」。

用 apscheduler 的 AsyncIOScheduler + cron 触发器；任务失败只记日志，
绝不影响机器人本身（知识沉淀是增益功能，不是关键路径）。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

# apscheduler 的执行器每次跑任务都会打两条 INFO（Running job / executed successfully）。
# 提醒轮询是 30 秒一次的空转，一天会刷几千行、把有用日志淹掉，所以默认压到 WARNING：
# 任务失败（异常/错过执行时间）仍会以 WARNING/ERROR 出现，启动期的 job 注册日志也保留。
_NOISY_APSCHEDULER_LOGGERS = (
    "apscheduler.executors.default",
    "apscheduler.executors.asyncio",
)


def quiet_apscheduler_executor_logs() -> None:
    """把 apscheduler 的「每轮执行」日志压到 WARNING（幂等）。"""
    for name in _NOISY_APSCHEDULER_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


class AgentScheduler:
    def __init__(self, timezone: str | None = None):
        quiet_apscheduler_executor_logs()
        self._scheduler = (
            AsyncIOScheduler(timezone=timezone) if timezone else AsyncIOScheduler()
        )
        self._started = False

    def add_cron(
        self, job_id: str, cron: str, func: Callable[[], Awaitable], *, name: str = ""
    ):
        """注册一个 cron 任务；cron 为 5 段表达式（分 时 日 月 周）。"""
        try:
            trigger = CronTrigger.from_crontab(cron, timezone=self._scheduler.timezone)
        except Exception:
            logger.exception("invalid cron %r for job %s, job skipped", cron, job_id)
            return False
        self._scheduler.add_job(
            _guard(func, job_id),
            trigger=trigger,
            id=job_id,
            name=name or job_id,
            replace_existing=True,
            misfire_grace_time=3600,  # 停机错过仍可补跑 1 小时内的窗口
            coalesce=True,
        )
        logger.info("scheduled job %s: cron=%s", job_id, cron)
        return True

    def add_interval(self, job_id: str, seconds: int, func, *, name: str = ""):
        """注册一个固定间隔任务（如每 30 秒检查一次到点提醒）。"""
        seconds = max(5, int(seconds))
        self._scheduler.add_job(
            _guard(func, job_id),
            trigger="interval",
            seconds=seconds,
            id=job_id,
            name=name or job_id,
            replace_existing=True,
            misfire_grace_time=max(30, seconds),
            coalesce=True,
            max_instances=1,
        )
        logger.info("scheduled job %s: every %ss", job_id, seconds)
        return True

    def start(self) -> None:
        if self._started:
            return
        self._scheduler.start()
        self._started = True
        logger.info(
            "scheduler started, jobs=%s", [j.id for j in self._scheduler.get_jobs()]
        )

    def shutdown(self, wait: bool = False) -> None:
        if self._started:
            self._scheduler.shutdown(wait=wait)
            self._started = False

    def jobs(self) -> list[dict]:
        out = []
        for j in self._scheduler.get_jobs():
            # apscheduler 3.x 只在调度器启动后才会给 job 挂上 next_run_time
            nxt = getattr(j, "next_run_time", None)
            out.append({"id": j.id, "next_run": str(nxt) if nxt else None})
        return out


def _guard(func: Callable[[], Awaitable], job_id: str):
    async def _wrapper():
        try:
            await func()
        except Exception:
            logger.exception("scheduled job %s failed", job_id)

    return _wrapper
