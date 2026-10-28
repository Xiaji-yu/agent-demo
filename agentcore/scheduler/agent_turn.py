"""agent_turn 执行体：到点让 agent 跑一轮，把结果投递到 jobs.json 指定的会话。

与 push/nudge 的分工：那两者是「配置里写死 prompt/模板，到点生成正文投递」；
agent_turn 是「把 message 当一轮用户输入丢给 AgentEngine」——agent 可以自己调
skill（执行脚本、查数据），产出什么就投什么。用户给的样例正是这个形态
（``执行 sh .../energy_report.sh，把 stdout 原样发到群聊``）。

**为什么复用 ``engine.run`` 而不是 ``generate_proactive``**：后者不带工具、
不写历史，是「一句话主动搭讪」用的；而 agent_turn 的 message 明确要求调用工具
（exec / message），必须走完整的 tool-loop。

**人格与历史**：人格走 default 分支（需求①改造后 = 根级 SOUL.md）；**不带历史**
——jobs.json 是无人值守任务，隔离单轮更可预期，也不会把群里几天的聊天灌进去。

**投递三态**：与 push/nudge 共用 ``Sink.send_once`` 的 sent/failed/uncertain。
结果不确定时**绝不重发**（出站铁律），状态记 ``uncertain`` 而非 ``ok``。
"""

from __future__ import annotations

import logging

from agentcore import tz
from agentcore.scheduler.jobs import CronJob, parse_target, update_state

logger = logging.getLogger(__name__)


def _zone_for(tz_name: str | None):
    """取 job 的时区对象：job 自带 tz 优先，缺省/非法回退进程统一时区。

    ``agentcore.tz.zoneinfo()`` 只接受 0 个参数（返回进程统一时区），所以命名时区
    必须自己构造 ZoneInfo——但**不能用裸 ``datetime.now()``**：仓库不变量要求时钟
    统一走 tz，否则 UTC 容器上「每天9点」会拖到北京时间 17:00 才响。
    """
    if not tz_name:
        return tz.zoneinfo()
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(tz_name)
    except Exception:
        logger.warning(
            "cron job 时区 %r 不可用，回退进程统一时区（%s）",
            tz_name,
            tz.zoneinfo(),
        )
        return tz.zoneinfo()


def _next_run_ms(cron: str, tz_name: str | None) -> int:
    """算下次执行时刻（epoch ms）；解析失败返回 0。

    时区口径：**job 自带 tz 优先，缺省用进程统一时区**（``agentcore.tz``，
    ``AGENT_SCHEDULER_TZ`` 可覆盖）——与 ``reminder.next_cron_time`` 同一套判据。
    """
    try:
        from apscheduler.triggers.cron import CronTrigger
    except Exception:  # pragma: no cover
        return 0
    zone = _zone_for(tz_name)
    try:
        trigger = CronTrigger.from_crontab(cron, timezone=zone)
    except Exception:
        logger.warning("cron job 表达式无法解析：%r", cron)
        return 0
    # 基准时刻取自统一时钟再转到目标时区：两边同源，「下次执行」才准
    base = tz.now().astimezone(zone)
    nxt = trigger.get_next_fire_time(None, base)
    return int(nxt.timestamp() * 1000) if nxt else 0


class AgentTurnRunner:
    """持有一组 agent_turn job，到点逐个执行。"""

    def __init__(self, engine, sink, *, scheduler=None):
        self.engine = engine
        self.sink = sink
        self.scheduler = scheduler
        # 坏文件/坏条目的告警去重：tick 是 60s 一轮，不去重会把同一条错误刷
        # 1440 次/天，把真正的告警埋掉（与 personas._warn_once 同思路）。
        self._last_error: str | None = None

    def _warn_once(self, msg: str) -> None:
        if msg == self._last_error:
            return
        self._last_error = msg
        logger.warning("%s", msg)

    async def run_job(self, job: CronJob) -> str:
        """执行一条 job，返回三态：``ok`` / ``failed`` / ``uncertain``。"""
        target = parse_target(job.to)
        if target is None:
            # 构造期已校验，正常到不了；防御性兜底避免把 None 解包炸掉整个 tick
            logger.warning("cron job %s (%s) 目标非法：%r", job.id, job.name, job.to)
            return "failed"
        kind, ident = target
        context = {
            "user_id": str(ident),
            "group_id": str(ident) if kind == "group" else "",
        }
        status = "failed"
        try:
            text = await self.engine.run(context, job.message)
        except Exception:
            logger.exception("cron job %s (%s) agent_turn 执行失败", job.id, job.name)
            text = ""
        if not text or not str(text).strip():
            logger.warning("cron job %s (%s) 产出为空，不投递", job.id, job.name)
        else:
            outcome = await self.sink.send_once(job.to, str(text))
            if outcome == "sent":
                status = "ok"
            elif outcome == "uncertain":
                # 已发出、响应丢失：不当失败（不触发重试计数），但也不算成功
                status = "uncertain"
            else:
                logger.warning("cron job %s (%s) 投递失败", job.id, job.name)
        update_state(job.id, status=status, next_run_ms=_next_run_ms(job.cron, job.tz))
        return status

    async def tick(self) -> None:
        """扫描 jobs.json 并执行所有 enabled 的 agent_turn job。

        由 apscheduler 的 interval 触发器驱动（默认 60s）。真正的「到点」判断在
        这里做而不是依赖 apscheduler 的 cron 触发器——因为 cron 表达式带 **tz**，
        而 AgentScheduler 只有一个全局时区；让 apscheduler 按 UTC/本地触发会让
        带 tz 的 job 在错误时刻跑。interval + 自己算下次时刻更直白，也顺带
        支持「手改 jobs.json 后无需重启即生效」（下次 tick 重读文件）。
        """
        from agentcore.scheduler import jobs as jobs_mod

        try:
            jobs, errors = jobs_mod.load()
        except jobs_mod.JobsError as e:
            self._warn_once(f"cron jobs.json 读取失败，本轮跳过：{e}")
            return
        if errors:
            # 坏条目逐条报告（一个错 job 不该把其余的也废掉），按内容去重
            self._warn_once("cron job 配置错误，已跳过：" + "；".join(errors))
        else:
            self._last_error = None  # 恢复后重新报下一次故障
        # 到点判断用统一时钟（tz.now），与 next_run_at_ms 的写入同源：
        # 一边用进程时区、一边用裸 datetime.now() 会让「下次执行」永远算不准。
        now_ms = int(tz.now().timestamp() * 1000)
        for job in jobs:
            if not job.enabled:
                continue
            # 到点判断：nextRunAtMs 缺省/已过期（<=now）即执行。首次运行
            # （next_run_at_ms == 0）立即跑一次——新加的 job 不该等到下个周期。
            if job.next_run_at_ms and job.next_run_at_ms > now_ms:
                continue
            await self.run_job(job)

    def register(self, seconds: int = 60) -> bool:
        """把 tick 注册进 scheduler（interval）。"""
        if self.scheduler is None:
            return False
        return self.scheduler.add_interval(
            "cron_jobs", seconds, self.tick, name="jobs.json 定时任务"
        )
