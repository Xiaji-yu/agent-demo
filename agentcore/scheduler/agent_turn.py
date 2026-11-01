"""agent_turn 执行体：到点让 agent 跑一轮，把结果投递到 jobs.json 指定的会话。

来源：UGREEN a7e221b 骨架，按 AC-group-guest-mode.md D4 补齐三修复：

- **M1 投递白名单**：目标群不在 ``ALLOWED_GROUPS`` → ``failed`` + WARNING +
  **零投递**——jobs.json 是无人值守通道，配置错误/被改时不能成为绕过群白名单
  的投递后门（判定与 acl 的群准入同一清单）。
- **M2 会话隔离**：engine 会话键用 ``cron:<job_id>``（``session_override``），
  不读不写任何真实用户/群的历史与摘要；预算路由记 ``cron:<job_id>``（主对话
  的成本账不被 cron 摊薄）。人格走 default 分支；growth 以伪 user（目标群号/
  QQ 号）记录——真实用户会话永不以该 id 对话，条目成为不可召回的孤儿，无污染
  面（显式跳过留待 growth 侧有需要时再加）。
- **L3 崩溃窗口不重投**：执行前先把 ``next_run`` 推进到下个周期再执行——执行
  中崩溃（掉电/重启）不会让下个 tick 立即重跑（出站铁律：宁可少一次，不可
  多一次）。

与 push/nudge 的分工：那两者是「配置里写死 prompt/模板，到点生成正文投递」；
agent_turn 是「把 message 当一轮用户输入丢给 AgentEngine」——agent 可以自己调
skill（执行脚本、查数据），产出什么就投什么。

**投递三态**：与 push/nudge 共用 ``Sink.send_once`` 的 sent/failed/uncertain。
结果不确定时**绝不重发**（出站铁律），状态记 ``uncertain`` 而非 ``ok``。
"""

from __future__ import annotations

import logging
from pathlib import Path

from agentcore import tz
from agentcore.scheduler.jobs import CronJob, parse_target, update_state

logger = logging.getLogger(__name__)


def _zone_for(tz_name: str | None):
    """取 job 的时区对象：job 自带 tz 优先，缺省/非法回退进程统一时区。

    不能用裸 ``datetime.now()``：仓库不变量要求时钟统一走 tz，否则 UTC 容器上
    「每天9点」会拖到北京时间 17:00 才响。
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

    时区口径：job 自带 tz 优先，缺省用进程统一时区（与 reminder.next_cron_time
    同一套判据）。
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
    base = tz.now().astimezone(zone)
    nxt = trigger.get_next_fire_time(None, base)
    return int(nxt.timestamp() * 1000) if nxt else 0


def _target_allowed(to: str, group_allowed) -> bool:
    """M1：投递目标过群白名单。私聊目标不设限（superuser 本人配置的通道），
    群目标必须命中白名单——判定函数由 plugins 接线时注入（acl 的群准入同一
    清单；agentcore 不得反向依赖适配层）。``group_allowed`` 缺省 None 时
    **fail-closed**：所有群目标一律拒（宁可不投，不可绕过白名单）。"""
    target = parse_target(to)
    if target is None:
        return False
    kind, ident = target
    if kind != "group":
        return True
    if group_allowed is None:
        return False
    return bool(group_allowed(str(ident)))


class AgentTurnRunner:
    """持有一组 agent_turn job，到点逐个执行。

    ``group_allowed``：群目标白名单判定（``Callable[[str], bool]``），由
    plugins 接线注入 acl 的群准入；不注入则群目标 fail-closed。
    """

    def __init__(
        self,
        engine,
        sink,
        *,
        scheduler=None,
        group_allowed=None,
        jobs_path: Path | None = None,
    ):
        self.engine = engine
        self.sink = sink
        self.scheduler = scheduler
        self._group_allowed = group_allowed
        # jobs_path：测试注入用；生产 None = 默认位置（data/cron/jobs.json）。
        # load/update_state 都以它为准，测试不传就会写真实文件（jobs.py 的
        # docstring 有同款教训）
        self._jobs_path = jobs_path
        # 坏文件/坏条目的告警去重：tick 是 60s 一轮，不去重会把同一条错误刷
        # 1440 次/天（与 personas._warn_once 同思路）。
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
            logger.warning("cron job %s (%s) 目标非法：%r", job.id, job.name, job.to)
            return "failed"
        # M1：白名单外目标零投递（写在 next_run 推进之后——失败的 job 也要
        # 推进到下个周期，否则下个 tick 立即重试同样的失败）
        if not _target_allowed(job.to, self._group_allowed):
            logger.warning(
                "cron job %s (%s) 目标 %s 不在 ALLOWED_GROUPS，已跳过投递",
                job.id,
                job.name,
                job.to,
            )
            update_state(
                job.id,
                status="failed",
                next_run_ms=_next_run_ms(job.cron, job.tz),
                path=self._jobs_path,
            )
            return "failed"

        kind, ident = target
        # M2：会话隔离键 + 预算路由都以 cron 命名空间记——真实用户/群的
        # 历史、摘要、成本账一概不碰
        context = {
            "user_id": str(ident),
            "group_id": str(ident) if kind == "group" else "",
            "session_override": f"cron:{job.id}",
            "route_override": f"cron:{job.id}",
        }
        # L3：先把 next_run 推进到下个周期再执行——执行中崩溃不会重跑（宁可
        # 少一次，不可多一次）；执行完再以最终状态覆盖一次
        update_state(
            job.id,
            status="running",
            next_run_ms=_next_run_ms(job.cron, job.tz),
            path=self._jobs_path,
        )
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
                status = "uncertain"
            else:
                logger.warning("cron job %s (%s) 投递失败", job.id, job.name)
        update_state(
            job.id,
            status=status,
            next_run_ms=_next_run_ms(job.cron, job.tz),
            path=self._jobs_path,
        )
        return status

    async def tick(self) -> None:
        """扫描 jobs.json 并执行所有 enabled 的 agent_turn job。

        由 interval 触发器驱动（默认 60s），「到点」判断在这里做——cron 表达式
        带 **tz** 而 scheduler 只有一个全局时区；interval + 自算下次时刻更直白，
        也顺带支持「手改 jobs.json 无需重启即生效」（下次 tick 重读文件）。
        """
        from agentcore.scheduler import jobs as jobs_mod

        try:
            jobs, errors = jobs_mod.load(self._jobs_path)
        except jobs_mod.JobsError as e:
            self._warn_once(f"cron jobs.json 读取失败，本轮跳过：{e}")
            return
        if errors:
            self._warn_once("cron job 配置错误，已跳过：" + "；".join(errors))
        else:
            self._last_error = None
        now_ms = int(tz.now().timestamp() * 1000)
        for job in jobs:
            if not job.enabled:
                continue
            # 到点判断：nextRunAtMs 缺省/已过期（<=now）即执行。首次运行
            # （next_run_at_ms == 0）立即跑一次——新 job 不该等到下个周期。
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
