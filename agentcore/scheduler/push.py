"""定时内容推送（M7）：到点**生成**内容并主动投递给群/私聊。

与「定时提醒」的分界（REVIEW-bbd8913..f6dffcc L17 点名的口径混乱）：
- 提醒（``action='remind'``）= 用户自助登记的**确定性文案**，``message`` 即最终文本；
- 推送（``action='push'``）= **部署级配置**的任务，到点由 LLM 按 ``prompt`` 生成正文，
  LLM 不可用 / 预算熔断 / 输出为空时回退 ``template`` 兜底文案，仍失败才停用并告警。

两者共用 ``schedules`` 表（DDL 的 ``action``/``params`` 列本就存在，**无 schema 变更**）；
提醒类工具只看 ``action='remind'``，因此 ``reminder_list`` 不会串到推送任务。

设计约束（与仓库既有约定对齐）：
- 内容生成走同一个 LLMClient（主备切换/usage 上报都在里面），并受 budget 硬闸门约束
- 投递走 ``Sink``（与回复、提醒共用同一个出站节流器）
- 群目标必须命中 ``ALLOWED_GROUPS``（与主聊天路径同一套 ACL），fail-closed
- 失败顺延退避重试；连续失败 ``max_failures`` 次后**停用**并私聊管理员，
  避免配置错误每 tick 刷屏、也避免无限重试打脸用户
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from agentcore import tz
from agentcore.budget import get_budget
from agentcore.diagnostics import record as _diag_record
from agentcore.scheduler.reminder import next_cron_time

logger = logging.getLogger(__name__)

# push 任务统一归属这个"系统用户"：真人 QQ 号是 ASCII 数字，不会撞名；
# 也让 /push list 与启动期幂等注册有一条干净的过滤口径。
OWNER = "__push__"
ACTION_PUSH = "push"

# 配置里允许的任务数上限（防一份配置把 schedules 表灌爆）
_MAX_JOBS = 50
# 单次 tick 最多处理多少条到点任务（>_MAX_JOBS 的部分顺延到下个 tick）
_DUE_LIMIT = 50
# prompt 长度上限：超长提示词既贵又难 review，按配置错误处理
_MAX_PROMPT_CHARS = 1000

_SYSTEM_PROMPT = (
    "你是 QQ 群/私聊里的机器人助手。下面是运维配置的一条**主动推送**任务，"
    "请按它的要求写出即将直接发给用户的正文。\n"
    "要求：只输出正文本身；不要解释你在做什么；不要加「以下是…」这类前后缀；"
    "不要用 markdown 标题或代码块；语气自然，像群友说话。"
)


# ---------------------------------------------------------------------------
# 配置


@dataclass(frozen=True)
class PushJob:
    """配置里的一条推送任务（**不是**落库行）。"""

    name: str
    cron: str
    target: str
    prompt: str
    template: str


@dataclass
class PushConfig:
    enabled: bool = True
    tick: int = 30
    retry_delay: int = 300
    max_failures: int = 5
    max_retry_delay: int = 86400
    daily_cap_per_target: int = 1
    max_chars: int = 500
    jobs: list[PushJob] = field(default_factory=list)


def _env_flag(env, name: str, default: bool) -> bool:
    """布尔 env：``0/false/no/off`` 为关，空值/脏值用默认（不崩启动）。"""
    raw = str(env.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def _env_int(env, name: str, default: int, *, minimum: int = 0) -> int:
    """整数 env：脏值告警后回退默认，并钳制下限（与 _reminder_tick_seconds 同款）。"""
    raw = str(env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r 不是整数，回退 %d", name, raw, default)
        return default
    return max(minimum, value)


def _scalar(
    env,
    env_name: str,
    cfg_key: str,
    cfg_value,
    default: int,
    *,
    minimum: int = 0,
) -> int:
    """标量配置统一解析：env > config.yaml > 内置默认（脏值告警回退）。

    旧实现只认 env + 内置默认：config.yaml 的 push 段标量（tick/retry_delay/
    max_failures/…）被静默丢弃——部署者照注释调了配置却毫无效果（审查 P1）。
    """
    if str(env.get(env_name) or "").strip():
        return _env_int(env, env_name, default, minimum=minimum)
    if cfg_value is None:
        return default
    try:
        value = int(cfg_value)
    except (TypeError, ValueError):
        logger.warning(
            "push.%s=%r 不是整数，回退默认 %s（env %s 可覆盖）",
            cfg_key,
            cfg_value,
            default,
            env_name,
        )
        return default
    if value < minimum:
        logger.warning(
            "push.%s=%s 低于下限 %s，回退默认 %s", cfg_key, value, minimum, default
        )
        return default
    return value


def _flag(env, env_name: str, cfg_value, default: bool) -> bool:
    """布尔配置统一解析：env > config.yaml > 内置默认（脏值告警回退）。"""
    if str(env.get(env_name) or "").strip():
        return _env_flag(env, env_name, default)
    if cfg_value is None:
        return default
    if isinstance(cfg_value, bool):
        return cfg_value
    s = str(cfg_value).strip().lower()
    if s in {"true", "yes", "on", "1"}:
        return True
    if s in {"false", "no", "off", "0"}:
        return False
    logger.warning("push.enabled=%r 不是布尔，回退默认 %s", cfg_value, default)
    return default


def parse_target(target: str) -> tuple[str, int] | None:
    """``group:123`` / ``private:456`` → ("group", 123)；非法返回 None。

    AGENTS.md §5：必须 ``isascii() and isdigit()``——全角 ``"１２３"`` 也能过
    ``isdigit()`` 并被 int() 成 123，会让推送发到**另一个**号上。
    """
    kind, _, ident = str(target or "").partition(":")
    if kind not in ("private", "group") or not ident:
        return None
    if not (ident.isascii() and ident.isdigit()):
        return None
    return kind, int(ident)


def _today_anchor() -> str:
    """时效性锚点：与日键/预算共用 agentcore.tz 的统一「今天」。

    旧实现写 `dt.ZoneInfo(...)`——datetime 模块没有 ZoneInfo 属性，
    AttributeError 被 except 吞掉后静默回落服务器本地时钟（重审 P1）。
    """
    return tz.today_anchor()


def _job_from_raw(raw, index: int) -> tuple[PushJob | None, str | None]:
    """把一段配置解析成 PushJob；返回 (job, 错误原因)，二者恰有一个非 None。"""
    if not isinstance(raw, dict):
        return None, "不是对象"
    name = str(raw.get("name") or "").strip() or f"job-{index + 1}"
    target = str(raw.get("target") or "").strip()
    if parse_target(target) is None:
        return None, f"target 非法（应为 group:<群号> / private:<QQ号>）：{target!r}"
    cron = str(raw.get("cron") or "").strip()
    if next_cron_time(cron) is None:
        return None, f"cron 无法解析：{cron!r}"
    prompt = str(raw.get("prompt") or "").strip()
    template = str(raw.get("template") or "").strip()
    if not prompt and not template:
        return None, "prompt 与 template 不能都为空（没有任何可推送内容）"
    if len(prompt) > _MAX_PROMPT_CHARS:
        return None, f"prompt 过长（>{_MAX_PROMPT_CHARS} 字）"
    job = PushJob(name=name, cron=cron, target=target, prompt=prompt, template=template)
    return job, None


def load_push_config(config: dict | None, env=None) -> PushConfig:
    """从 config.yaml 的 ``push:`` 段 + env 覆盖解析配置（纯函数，便于单测）。"""
    env = os.environ if env is None else env
    cfg = (config or {}).get("push") or {}
    if not isinstance(cfg, dict):
        logger.warning("push 配置段不是对象，已按默认值处理")
        cfg = {}

    raw_jobs = cfg.get("jobs") or []
    if not isinstance(raw_jobs, list):
        logger.warning("push.jobs 不是列表，已忽略")
        raw_jobs = []
    if len(raw_jobs) > _MAX_JOBS:
        logger.warning("push.jobs 超过 %d 条，多余部分已忽略", _MAX_JOBS)

    jobs: list[PushJob] = []
    for index, raw in enumerate(raw_jobs[:_MAX_JOBS]):
        job, error = _job_from_raw(raw, index)
        if job is None:
            logger.warning("push 任务 #%d 配置无效已跳过：%s", index + 1, error)
            continue
        jobs.append(job)

    return PushConfig(
        enabled=_flag(env, "AGENT_PUSH_ENABLED", cfg.get("enabled"), True),
        tick=_scalar(env, "AGENT_PUSH_TICK", "tick", cfg.get("tick"), 30, minimum=5),
        retry_delay=_scalar(
            env,
            "AGENT_PUSH_RETRY_DELAY",
            "retry_delay",
            cfg.get("retry_delay"),
            300,
            minimum=5,
        ),
        max_failures=_scalar(
            env,
            "AGENT_PUSH_MAX_FAILURES",
            "max_failures",
            cfg.get("max_failures"),
            5,
            minimum=1,
        ),
        max_retry_delay=_scalar(
            env,
            "AGENT_PUSH_MAX_RETRY_DELAY",
            "max_retry_delay",
            cfg.get("max_retry_delay"),
            86400,
            minimum=60,
        ),
        daily_cap_per_target=_scalar(
            env,
            "AGENT_PUSH_DAILY_CAP_PER_TARGET",
            "daily_cap_per_target",
            cfg.get("daily_cap_per_target"),
            1,
        ),
        max_chars=_scalar(
            env, "AGENT_PUSH_MAX_CHARS", "max_chars", cfg.get("max_chars"), 500
        ),
        jobs=jobs,
    )


# ---------------------------------------------------------------------------
# 投递服务


class PushService:
    """到点检查并投递定时推送。由调度器周期性调用 ``tick()``。

    ``on_alert`` 用回调注入（agentcore 不认识 bot，与 embedding.on_error /
    llm.on_fallback 同一模式）：任务被停用等需要人介入的情况私聊管理员。
    """

    def __init__(
        self,
        store,
        sink,
        llm,
        *,
        config: PushConfig | None = None,
        allowed_groups=None,
        on_alert=None,
        state_file: str | Path | None = None,
    ) -> None:
        self.store = store
        self.sink = sink
        self.llm = llm
        self.cfg = config or PushConfig()
        self.allowed_groups = {str(g) for g in (allowed_groups or set())}
        self.on_alert = on_alert
        # 进程内记账：schedule 行没有可写的失败计数字段（不动 DDL），重启即清零——
        # 重启本身就是一次重试机会，语义上说得通。
        self._failures: dict[str, int] = {}
        # 每日每目标上限跨进程持久化：纯内存记账在双进程/重启后同日可再发满
        # 一轮（审查 verified）。state_file=None 退回进程内行为（测试用）。
        self._delivered: dict[tuple[str, str], int] = {}
        env_state = (os.getenv("AGENT_PUSH_STATE_FILE") or "").strip()
        self._state_file: Path | None = (
            Path(env_state) if env_state else Path(state_file) if state_file else None
        )
        if self._state_file is not None:
            self._load_state()

    # ---------- 配置落库（幂等） ----------

    async def register_jobs(self) -> dict:
        """把配置里的任务登记进 schedules（重启/改配置后调用，幂等）。

        同 ``job_key`` 且 cron/target 都没变的**启用**行原样保留；改过时间或目标则
        停用旧行、重建新行；只剩**停用**行且配置没变时不复活（管理员用
        ``/push off`` 关掉的任务不该被重启悄悄打开——要恢复就改任务名或改时间）。
        """
        stats = {"added": 0, "kept": 0, "replaced": 0, "skipped": 0}
        if not self.cfg.jobs:
            # 空配置也要走孤儿清扫：删光任务后重启，残留启用行会照旧投递
            disabled = await self._disable_orphans(
                {
                    str((row.get("params") or {}).get("job_key")): [row]
                    for row in await self._push_rows()
                },
                set(),
            )
            if disabled:
                stats["disabled"] = disabled
                logger.info("push: 空配置，停用 %d 条残留启用行", disabled)
            return stats
        # 配置期按 name 去重（保留首条）：同名两条此前会注册出两条启用行，
        # 到点双投（审查 verified）
        seen: set[str] = set()
        jobs: list[PushJob] = []
        for job in self.cfg.jobs:
            if job.name in seen:
                logger.warning(
                    "push: 配置中存在重复任务名 %r，忽略后续条目（保留首条）", job.name
                )
                continue
            seen.add(job.name)
            jobs.append(job)
        by_key: dict[str, list[dict]] = {}
        for row in await self._push_rows():
            by_key.setdefault(str((row.get("params") or {}).get("job_key")), []).append(
                row
            )
        for job in jobs:
            existing = by_key.get(job.name) or []
            active = next((r for r in existing if r.get("enabled")), None)
            if active is not None:
                unchanged = (
                    active["cron"] == job.cron and active["target"] == job.target
                )
                if unchanged:
                    stats["kept"] += 1
                    continue
                await self.store.schedule_cancel(active["id"], user_id=None)
                stats["replaced"] += 1
            elif any(
                r["cron"] == job.cron and r["target"] == job.target for r in existing
            ):
                stats["skipped"] += 1
                continue
            else:
                stats["added"] += 1
            await self._add_job(job)
        # D1：配置里已删除的任务，其孤儿**启用**行要停用——否则照旧按原 cron
        # 投递，LLM 预算照花、群消息照发（verified：删配置重启后 tick 仍 sent=1）
        disabled_orphans = await self._disable_orphans(by_key, seen)
        if disabled_orphans:
            stats["disabled"] = disabled_orphans
        logger.info(
            "push: jobs registered added=%d kept=%d replaced=%d skipped=%d disabled=%d",
            stats["added"],
            stats["kept"],
            stats["replaced"],
            stats["skipped"],
            disabled_orphans,
            stats["added"],
            stats["kept"],
            stats["replaced"],
            stats["skipped"],
        )
        return stats

    async def _disable_orphans(self, by_key: dict, seen: set[str]) -> int:
        """停用配置中已不存在 job_key 的启用行，返回停用条数。"""
        disabled = 0
        for key, rows in by_key.items():
            if key in seen:
                continue
            for row in rows:
                if row.get("enabled"):
                    await self.store.schedule_cancel(row["id"], user_id=None)
                    disabled += 1
                    logger.warning("push: 任务 %r 已从配置移除，停用其残留调度行", key)
        return disabled

    async def _add_job(self, job: PushJob) -> str:
        return await self.store.schedule_add(
            kind="cron",
            target=job.target,
            message=job.template,
            user_id=OWNER,
            cron=job.cron,
            next_run=next_cron_time(job.cron),
            action=ACTION_PUSH,
            params={
                "job_key": job.name,
                "prompt": job.prompt,
                "template": job.template,
            },
        )

    async def _push_rows(self) -> list[dict]:
        rows = await self.store.schedule_list(OWNER, include_disabled=True)
        return [r for r in rows if r.get("action") == ACTION_PUSH]

    async def list_jobs(self) -> list[dict]:
        """全部推送任务（含已停用），按下次触发时间排序——供 ``/push`` 展示。"""
        rows = await self._push_rows()
        rows.sort(key=lambda r: (r["next_run"] is None, r["next_run"] or 0))
        return rows

    async def disable_job(self, schedule_id: str) -> bool:
        """停用一条推送任务（``/push off``）。只认属于本服务的**启用中** push 行。

        已停用的行返回 False：重复 off 不该回「已停用」让人以为又关了一次。
        """
        sid = str(schedule_id)
        row = next(
            (
                r
                for r in await self._push_rows()
                if str(r["id"]) == sid and r.get("enabled")
            ),
            None,
        )
        if row is None:
            return False
        await self.store.schedule_mark_fired(sid, None)
        self._failures.pop(sid, None)
        logger.info(
            "push: job %s (%s) disabled by admin",
            (row.get("params") or {}).get("job_key") or sid,
            row.get("target"),
        )
        return True

    # ---------- 到点投递 ----------

    async def tick(self) -> dict:
        now = time.time()
        # store 侧按 action 过滤：提醒积压时 push 行不再被先取后筛挤掉
        rows = await self.store.schedule_due(now, limit=_DUE_LIMIT, action=ACTION_PUSH)
        out = {"due": len(rows), "sent": 0, "failed": 0, "capped": 0, "disabled": 0}
        for row in rows:
            outcome = await self.process(row, now=now)
            if outcome == "sent":
                out["sent"] += 1
            elif outcome == "failed":
                out["failed"] += 1
            elif outcome == "capped":
                out["capped"] += 1
            else:  # blocked / disabled：见 process 的返回约定
                out["disabled"] += 1
        if rows:
            logger.info(
                "push: due=%d sent=%d failed=%d capped=%d disabled=%d",
                out["due"],
                out["sent"],
                out["failed"],
                out["capped"],
                out["disabled"],
            )
        return out

    async def process(self, row: dict, now: float | None = None) -> str:
        """处理一条到点任务，返回 ``sent``/``failed``/``capped``/``blocked``/``disabled``。

        ``capped`` = 本周期不投递但任务保留、已重新调度（每日上限，或预算闸门
        临时跳过）；``disabled`` = 永久停用并告警（配置错误 / 连续投递失败）。
        """
        now = time.time() if now is None else now
        sid = str(row["id"])
        params = row.get("params") or {}
        name = str(params.get("job_key") or sid)
        target = str(row.get("target") or "")

        if not self._target_allowed(target):
            # ALLOWED_GROUPS 在进程启动时读取，运行期改了也不生效——配置错误不会
            # 自愈，停用并告警，别每 tick 刷一次 WARNING。
            await self._disable(sid, name, target, "目标不在 ALLOWED_GROUPS 白名单")
            return "blocked"

        if self._cap_reached(target, now):
            # 预期限流而非故障：分钟级 cron 会一天刷 1440 条 WARNING
            logger.info("push: %s (%s) 已达当日每目标上限，跳过本次", name, target)
            await self._reschedule(row, now)
            return "capped"

        text, transient = await self._content(params)
        if not text:
            if transient:
                # prompt 型任务撞预算硬闸门且无模板兜底：**临时态**，下个周期再试。
                # 旧实现按「无内容配置」永久停用并告警——预算恢复后任务已经没了，
                # 管理员还收到一条假告警（审查 P1）。
                logger.warning(
                    "push: %s (%s) 预算硬闸门且无模板兜底，本周期跳过（已重新调度）",
                    name,
                    target,
                )
                await self._reschedule(row, now)
                return "capped"
            await self._disable(
                sid, name, target, "没有可推送内容（prompt 与 template 均为空）"
            )
            return "disabled"

        outcome = await self.sink.send_once(target, text)
        if outcome == "uncertain":
            # 结果不确定绝不重发（与 reminder 同口径）：照常推进调度，
            # 但不计入成功、不触发失败计数/告警
            logger.warning(
                "push: %s (%s) delivery uncertain, not resending", name, target
            )
            self._failures.pop(sid, None)
            await self._reschedule(row, now)
            _diag_record("push_uncertain", job=name)
            return "uncertain"
        if outcome == "failed":
            return await self._handle_failure(row, name, target, now)

        self._failures.pop(sid, None)
        self._bump_delivered(target, now)
        await self._reschedule(row, now)
        _diag_record("push_sent", job=name)
        return "sent"

    # ---------- 内部 ----------

    def _target_allowed(self, target: str) -> bool:
        parsed = parse_target(target)
        if parsed is None:
            return False
        kind, ident = parsed
        if kind == "private":
            # 私聊目标由部署者显式配置，与「私聊 ACL 只放 superuser」不同源：
            # 这里发不发是运维决定的事，故不再筛一遍。
            return True
        return str(ident) in self.allowed_groups

    def _day_key(self, now: float) -> str:
        # 与推送正文的「今天」同源（agentcore.tz）——日界漂移曾让 UTC 服务器
        # 上「每日上限」到本地早 8 点才重置（且旧实现因 dt.ZoneInfo 笔误是 no-op）
        return tz.day_key(now)

    def _cap_reached(self, target: str, now: float) -> bool:
        cap = self.cfg.daily_cap_per_target
        if cap <= 0:  # 0 = 不限
            return False
        return self._delivered.get((self._day_key(now), target), 0) >= cap

    def _bump_delivered(self, target: str, now: float) -> None:
        day = self._day_key(now)
        # 只留今天：跨天键自然失效，顺手防止字典无限增长
        self._delivered = {k: v for k, v in self._delivered.items() if k[0] == day}
        key = (day, target)
        self._delivered[key] = self._delivered.get(key, 0) + 1
        self._save_state()

    def _load_state(self) -> None:
        """读回持久化的每日计数；只保留今天（旧日键即过期）。"""
        try:
            raw = json.loads(self._state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception:
            logger.warning("push: 计数状态文件损坏，忽略（%s）", self._state_file)
            return
        day = self._day_key(time.time())
        today = raw.get(day) if isinstance(raw, dict) else None
        if isinstance(today, dict):
            for target, count in today.items():
                try:
                    self._delivered[(day, str(target))] = int(count)
                except (TypeError, ValueError):
                    continue

    def _save_state(self) -> None:
        """原子写（.part + os.replace，0600）；失败只告警——上限是弱约束，
        丢一笔计数最多少发一条，不该打断推送本身。"""
        if self._state_file is None:
            return
        day = self._day_key(time.time())
        payload = {day: {t: c for (d, t), c in self._delivered.items() if d == day}}
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            part = Path(f"{self._state_file}.part")
            part.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.chmod(part, 0o600)
            os.replace(part, self._state_file)
        except OSError:
            logger.warning(
                "push: 计数状态写入失败（%s）", self._state_file, exc_info=True
            )

    async def _content(self, params: dict) -> tuple[str, bool]:
        """返回 ``(内容, 是否临时不可用)``。

        临时不可用 = 有 prompt 但预算硬闸门挡住了生成且无模板兜底——这是
        「等下个周期」的信号，与「配置里根本没有内容」（永久配置错误）必须区分。
        """
        prompt = str(params.get("prompt") or "").strip()
        template = str(params.get("template") or "").strip()
        if not prompt:
            return self._clamp(template), False
        blocked, _reason = get_budget().chat_blocked()
        if blocked:
            # 预算硬闸门：继续生成会把"超额"本身刷给用户。回退模板照发，
            # 与预算分支"直接返回未发送结果"不对立——那是**对话轮次**的语义。
            logger.info("push: budget hard gate reached, fall back to template")
            return self._clamp(template), not template
        try:
            data = await self.llm.chat(
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (f"{_today_anchor()}\n\n推送任务要求：\n{prompt}"),
                    },
                ],
                # max_chars 兼任输出上限（CJK 下 1 字≈1 token）；0 = 不限制
                max_tokens=self.cfg.max_chars or None,
            )
        except Exception:
            logger.exception("push: content generation failed, fall back to template")
            return self._clamp(template), False
        text = self._extract(data)
        if not text:
            logger.warning("push: LLM returned empty content, fall back to template")
            return self._clamp(template), False
        return self._clamp(text), False

    @staticmethod
    def _extract(data) -> str:
        choices = data.get("choices") if isinstance(data, dict) else None
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        return str(message.get("content") or "").strip()

    def _clamp(self, text: str) -> str:
        text = (text or "").strip()
        cap = self.cfg.max_chars
        if cap > 0 and len(text) > cap:
            return text[:cap]
        return text

    async def _reschedule(self, row: dict, now: float) -> None:
        sid = str(row["id"])
        cron = str(row.get("cron") or "")
        if str(row.get("kind") or "") == "cron" and cron:
            nxt = next_cron_time(cron, now)
            if nxt is None:
                # apscheduler 缺失/表达式失效：顺延不了就只能停用，别让任务每 tick
                # 重复投递同一条（比"再也不推"更难排查）
                logger.error(
                    "push: cron %r 无法计算下次触发时间，任务 #%s 已停用", cron, sid
                )
            await self.store.schedule_mark_fired(sid, nxt)
        else:
            # 一次性任务：送达即停用（与提醒同语义）
            await self.store.schedule_mark_fired(sid, None)

    async def _handle_failure(
        self, row: dict, name: str, target: str, now: float
    ) -> str:
        sid = str(row["id"])
        fails = self._failures.get(sid, 0) + 1
        self._failures[sid] = fails
        if fails >= max(1, self.cfg.max_failures):
            await self._disable(sid, name, target, f"连续 {fails} 次投递失败")
            return "disabled"
        delay = min(self.cfg.retry_delay * (2 ** (fails - 1)), self.cfg.max_retry_delay)
        await self.store.schedule_mark_fired(sid, now + delay)
        logger.warning(
            "push: %s (%s) delivery failed (%d/%d), retry in %ss",
            name,
            target,
            fails,
            self.cfg.max_failures,
            delay,
        )
        _diag_record("push_failed", job=name, fails=fails)
        return "failed"

    async def _disable(self, sid: str, name: str, target: str, reason: str) -> None:
        logger.error("push: job %s (%s) disabled: %s", name, target, reason)
        _diag_record("push_disabled", job=name, reason=reason[:40])
        await self.store.schedule_mark_fired(sid, None)
        self._failures.pop(sid, None)
        await self._alert(f"⚠️ 定时推送任务已停用：{name}（{target}）\n原因：{reason}")

    async def _alert(self, text: str) -> None:
        if self.on_alert is None:
            return
        try:
            await self.on_alert(text)
        except Exception:
            logger.warning("push: alert notify failed", exc_info=True)
