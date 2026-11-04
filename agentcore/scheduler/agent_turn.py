"""agent_turn 执行体：到点让 agent 跑一轮，把结果投递到 jobs.json 指定的会话。

来源：UGREEN a7e221b 骨架，按 AC-group-guest-mode.md D4 补齐三修复：

- **M1 投递白名单**：目标群不在 ``ALLOWED_GROUPS`` → ``failed`` + WARNING +
  **零投递**——jobs.json 是无人值守通道，配置错误/被改时不能成为绕过群白名单
  的投递后门（判定与 acl 的群准入同一清单）。
- **M2 会话隔离 + 身份收窄**（REVIEW-63f9444..858afcc 修订）：engine 会话键用
  ``cron:<job_id>``（``session_override``），不读不写任何真实用户/群的历史与
  摘要；预算路由记 ``cron:<job_id>``（主对话的成本账不被 cron 摊薄）。轮次身份
  用伪用户 ``cron:<job_id>``（非超管）+ ``unattended`` 标记——无人值守轮次
  **一概看不到 superuser 技能**（私聊目标也不例外；旧实现私聊目标带真实 QQ，
  where 轴视作私聊全放行）；growth 显式跳过，不写任何用户的成长层（旧实现以
  目标 id 记 growth，私聊目标会把无人值守产物污染进真实用户的 persona_growth）。
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

import asyncio
import logging
from pathlib import Path

from agentcore import tz
from agentcore.scheduler.jobs import CronJob, parse_target, update_state
from agentcore.scheduler.notify_budget import get_notify_budget

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


# 「算不出下次执行时刻」与「解析失败」必须分开：前者是永触发表达式
# （`0 0 30 2 *` = 2 月 30 日，from_crontab 能解析但 get_next_fire_time 永远
# None）。tick 的到点判断 `if next_run_at_ms and ...` 把 0 当「首次运行」——
# 于是这类 job 每 60s 跑一次完整 LLM 回合 + 投递，无限烧 token 且永不标记
# failed。负哨兵让调用方能区分两者；0 仍表示「从没跑过 / 依赖缺失」。
_NEVER_FIRE = -1


def _next_run_ms(cron: str, tz_name: str | None) -> int:
    """算下次执行时刻（epoch ms）；解析失败返回 0，永触发返回 ``_NEVER_FIRE``。

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
    return int(nxt.timestamp() * 1000) if nxt else _NEVER_FIRE


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
        # 永触发表达式告警按 job 去重：tick 是 60s 一轮，不去重会把同一条错误
        # 刷 1440 次/天，把真正的告警埋掉。
        self._dead_warned: set[str] = set()
        # M9：手动试跑（/cron run）与 tick 可能同时进入 run_job——不串行化会
        # 双投递（nudge 同款教训：与 tick 共用同一把锁）。懒加载：构造可能
        # 发生在无事件循环处（与 nudge._lock_obj 同思路）。
        self._lock: asyncio.Lock | None = None
        # 打扰预算告警按 job+目标去重：同一个 job 每分钟触发时，同一条预算用尽
        # 也只该出现一次（与 _warn_once 同思路）。
        self._budget_warned: set[tuple[str, str]] = set()
        # P3：全局打扰预算。agent_turn 原本是四类主动投递里唯一没有上限的——
        # 一条 * * * * * 的 job 就能把一个群每分钟刷一遍。
        self._budget = get_notify_budget()

    def _warn_once(self, msg: str) -> None:
        if msg == self._last_error:
            return
        self._last_error = msg
        logger.warning("%s", msg)

    def _warn_dead_once(self, job: CronJob) -> None:
        """永触发表达式按 job 去重告警（与 _warn_once 同思路）。"""
        if job.id in self._dead_warned:
            return
        self._dead_warned.add(job.id)
        logger.warning(
            "cron job %s (%s) 的表达式 %r 永触发（不存在满足条件的日期），"
            "已跳过；请改 schedule.expr 或把 enabled 置为 false",
            job.id,
            job.name,
            job.cron,
        )

    def _warn_budget_once(self, job: CronJob, target: str) -> None:
        """打扰预算用尽按 job+目标去重告警。

        与 _warn_dead_once 的区别：预算每天会重置，所以恢复后要能重新报，
        不能像永触发那样只报一次。job+目标作 key 已经够细——同一 job 换个
        目标仍是另一条。
        """
        key = (job.id, target)
        if key in self._budget_warned:
            return
        self._budget_warned.add(key)
        logger.warning(
            "cron job %s (%s) 的目标 %s 当日主动投递已达全局打扰预算上限，"
            "本轮不执行也不投递（AGENT_NOTIFY_DAILY_LIMIT，按 job 去重告警）",
            job.id,
            job.name,
            target,
        )

    @property
    def _run_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def run_job(self, job: CronJob) -> str:
        """执行一条 job（M9：与 tick / 手动试跑互斥），返回三态：
        ``ok`` / ``failed`` / ``uncertain``。"""
        async with self._run_lock:
            return await self._run_job_locked(job)

    async def _run_job_locked(self, job: CronJob) -> str:
        # 永触发表达式（`0 0 30 2 *`）：from_crontab 能解析，get_next_fire_time
        # 却永远 None，_next_run_ms 因此返 _NEVER_FIRE。装载期已在
        # jobs._validate_job 拒掉这类 job；这里是第二道闸——run_job 是执行一条
        # job 的唯一入口，挡住绕过装载期的调用方。不跑引擎也不推进 next_run：
        # 一条永远不会有下次执行的 job，写任何 next_run_at_ms 都是假的。
        if _next_run_ms(job.cron, job.tz) == _NEVER_FIRE:
            self._warn_dead_once(job)
            return "failed"
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

        # 全局打扰预算（P3）：agent_turn 的产出是 LLM 生成正文，跑一轮要花
        # token，所以预算闸必须放在引擎**之前**——省 LLM 调用，也省一次无意
        # 义的 tool-loop。仍推进下次执行时刻：否则 tick 会每 60s 重试一次，
        # 把日志刷满。
        # M13：判定 + 预占一步走（reserve）。旧流程 check→await 引擎+投递→record
        # 之间隔着整轮 LLM 调用，并发 tick 会双双看到空位而超发。
        budget_ok, budget_reserve = self._budget.reserve(job.to, "agent_turn")
        if not budget_ok:
            # P4：被全局预算拦下要留痕（面板「待决定区」据此显示"被挡下几次"）
            self._budget.record_blocked(job.to, "agent_turn")
            self._warn_budget_once(job, job.to)
            update_state(
                job.id,
                status="failed",
                next_run_ms=_next_run_ms(job.cron, job.tz),
                path=self._jobs_path,
            )
            return "failed"

        kind, ident = target
        # M2：会话隔离键 + 预算路由都以 cron 命名空间记——真实用户/群的
        # 历史、摘要、成本账一概不碰。轮次身份用伪用户 cron:<job_id>（非超管），
        # 无人值守轮次一概看不到 superuser 技能（私聊目标也不例外）；
        # unattended 供 engine 跳过 growth（不写任何用户的成长层）。
        context = {
            "user_id": f"cron:{job.id}",
            "group_id": str(ident) if kind == "group" else "",
            "session_override": f"cron:{job.id}",
            "route_override": f"cron:{job.id}",
            "unattended": True,
            # 主动投递类技能（发文件/图片）的目标会话：无人值守轮次的轮次
            # 身份是伪用户，直接拿 user_id 当投递目标必然失败——技能级产物
            # 的去处只能是 job.to。只解耦「投给谁」，不改权限轴（伪用户
            # 依旧看不到 superuser 技能）。
            "delivery_user_id": str(ident) if kind == "private" else "",
            "delivery_group_id": str(ident) if kind == "group" else "",
        }
        # AC-cron-trusted：显式信任的 job 以 owner 的权限身份执行（技能可见性
        # 与放行用 perm_user_id；会话键/预算/growth 隔离不变）。fail-closed：
        # trusted 与 owner 任一缺失 → 不带 perm_user_id，按伪用户收窄。
        if job.trusted and job.owner:
            context["perm_user_id"] = job.owner
            context["trusted_channel"] = True
            from agentcore.diagnostics import record

            record(
                "cron_trusted_run",
                job_id=job.id,
                owner=job.owner,
                to=job.to,
            )
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
            # M13：没投递就不占额度（预占在闸门处，归还放在所有不投递的出口）
            if budget_reserve is not None:
                budget_reserve.rollback()
        else:
            # ref 供出站 outbox 记账（只存字段不存正文，见 agentcore/outbox.py）
            outcome = await self.sink.send_once(job.to, str(text), ref=str(job.id))
            if outcome == "sent":
                status = "ok"
                # 预占已在闸门处完成，确认送达即 commit（M13）
                if budget_reserve is not None:
                    budget_reserve.commit()
            elif outcome == "uncertain":
                status = "uncertain"
                if budget_reserve is not None:
                    budget_reserve.rollback()  # 没送达不占额度
            else:
                logger.warning("cron job %s (%s) 投递失败", job.id, job.name)
                if budget_reserve is not None:
                    budget_reserve.rollback()
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
