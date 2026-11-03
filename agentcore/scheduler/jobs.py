"""OpenClaw 兼容的 cron 任务（``data/cron/jobs.json``，2026-11-01 P2 批移植重构）。

来源：UGREEN 分支 a7e221b 的骨架（格式与 OpenClaw 互拷兼容），按
AC-group-guest-mode.md D2 改造：

1. **出沙箱**：默认目录从 LLM 沙箱（data/workspace 下）→ ``data/cron``——LLM
   沙箱（fs_write 可达区）里不放任何持久化配置（H1）；修改通道 = SSH / web 面 /
   QQ 私聊命令（superuser），**LLM 全通道不可达**（workspace_purity 守卫钉住，
   本文件不得再出现沙箱内 cron 路径的字面串）。
2. **state 拆 sidecar**（M4）：``jobs.json`` 回归纯配置（手改即生效、USB 冷备、
   与 OpenClaw 互拷干净——那边文件里的 ``state`` 字段导入时作初值、之后忽略）；
   运行态写 ``state.json``，执行体回写不再触碰配置文件。
3. jobs.json 不入库（含真实投递目标，H2 教训）；模板在
   ``docs/examples/cron-jobs.example.json``；整目录 gitignore。

**文件形态**（未出现的字段忽略不报错，向前兼容 OpenClaw 的增量字段）::

    {
      "version": 1,
      "jobs": [{
        "id": "c2bfdcf1fba388b7",          # 缺省由 name 派生（unicode slug）
        "name": "每3小时用电用水报告",
        "enabled": true,                    # 新任务建议先 false 试跑再开
        "schedule": {"kind": "cron", "expr": "0 */3 * * *", "tz": "Asia/Shanghai"},
        "payload": {
          "kind": "agent_turn",             # 只此一种：到点让 agent 跑一轮并投递
          "message": "执行 ... 把 stdout 原样发到群聊",
          "channel": "onebot",
          "to": "group:123456789"          # 复用 push.parse_target
        },
        "deleteAfterRun": false
      }]
    }

**与 env 驱动的系统任务的分工**：jobs.json 只接管 ``payload.kind == "agent_turn"``；
kb_digest / db_backup / 群统计 / 提醒轮询仍是系统运维任务，继续走 env——用
agent_turn 间接表达它们只会更脆（多一层 LLM 依赖，失败时连备份都不做）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentcore import tz as agent_tz
from agentcore.boolenv import env_flag
from agentcore.scheduler.push import parse_target

logger = logging.getLogger(__name__)

CRON_DIR_ENV = "AGENT_CRON_DIR"
JOBS_FILENAME = "jobs.json"
STATE_FILENAME = "state.json"
_JOBS_KEY = "jobs"
# 出沙箱（H1）：绝不能指回 data/workspace 下——tests 的 workspace 纯净性守卫
# 钉住这一默认值
_DEFAULT_DIR = "data/cron"
_BACKUP_KEEP = 10
# id 允许 Unicode 词字符（中文等）：_slug 对中文 name 保留原词（docstring 自例
# 「每3小时用电用水报告」），旧 ASCII 正则会把无显式 id 的纯中文 job 全部拒掉
# （REVIEW-63f9444..858afcc M5，与 _slug 的承诺互相矛盾）。仍拒绝空格/标点/
# 控制字符，且必须以词字符开头（防纯连字符）。
_ID_RE = re.compile(r"^\w[\w-]{0,63}$")
_SUPPORTED_KINDS = {"agent_turn"}
_SUPPORTED_CHANNELS = {"onebot"}
_MSG_MAX = 2000


class JobsError(Exception):
    """配置文件层面的错误（坏 JSON / 顶层形态不对 / 落盘失败）。"""


@dataclass
class CronJob:
    id: str
    name: str
    enabled: bool
    cron: str
    message: str
    to: str
    channel: str = "onebot"
    tz: str | None = None
    delete_after_run: bool = False
    created_at_ms: int = 0
    # trusted（AC-cron-trusted）：到点轮次以 owner 的权限身份执行（perm_user_id
    # 只影响技能可见性/放行，会话键/预算/growth 隔离不变）。owner 由 /cron add
    # 写入当前 superuser QQ；两者任一缺失 → 按「非 trusted」执行（fail-closed，
    # 手改文件伪造 owner 无意义：能写 jobs.json 的人本就是服务器 root 级信任）。
    owner: str = ""
    trusted: bool = False
    # 运行态（sidecar 合并进来的，不落回 jobs.json）
    next_run_at_ms: int = 0
    last_run_at_ms: int = 0
    last_status: str = ""
    raw: dict = field(default_factory=dict)

    def public(self) -> dict:
        """对外形态：不含 message 全文（可能很长），只给长度与前 80 字。"""
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "cron": self.cron,
            "tz": self.tz,
            "channel": self.channel,
            "to": self.to,
            "delete_after_run": self.delete_after_run,
            "trusted": self.trusted,
            "message_len": len(self.message),
            "message_head": self.message[:80],
            "last_run_at_ms": self.last_run_at_ms,
            "last_status": self.last_status,
            "next_run_at_ms": self.next_run_at_ms,
        }


# ---------------------------------------------------------------- 路径


def cron_dir() -> Path:
    """jobs.json 所在目录：env 显式值优先，空串/纯空白回落默认。"""
    raw = (os.getenv(CRON_DIR_ENV) or "").strip()
    return Path(raw or _DEFAULT_DIR)


def jobs_path() -> Path:
    return cron_dir() / JOBS_FILENAME


def state_path() -> Path:
    return cron_dir() / STATE_FILENAME


# ---------------------------------------------------------------- 解析


def _now_ms() -> int:
    return int(time.time() * 1000)


def _slug(text: str) -> str:
    """从 name 派生 id：保留 unicode 单词字符（中文名得到中文 id），其余折叠成
    连字符。纯 ASCII 旧实现会把「每3小时用电用水报告」削成 ``"3"``——合法但
    不可读，且多个中文任务会撞成同一个 id。空结果回落 ``"job"``。

    产出口径与 ``_ID_RE``（Unicode 词字符 + 连字符）一致：M5 修订前 _ID_RE 只认
    ASCII，纯中文 name 无显式 id 的 job 会被自己派生的 id 拒之门外。
    """
    s = re.sub(r"[^\w]+", "-", (text or "").strip(), flags=re.UNICODE).strip("-")
    return s[:48] or "job"


def _validate_job(raw: Any, index: int) -> tuple[CronJob | None, str | None]:
    """把一段原始配置解析成 CronJob；返回 (job, 错误原因)，二者恰有一个非 None。"""
    if not isinstance(raw, dict):
        return None, f"jobs[{index}] 不是对象"
    name = str(raw.get("name") or "").strip() or f"job-{index + 1}"
    jid = str(raw.get("id") or "").strip() or _slug(name)
    if not _ID_RE.match(jid):
        return (
            None,
            f"id 非法（64 位以内，字母/数字/下划线/连字符/中文等词字符）：{jid!r}",
        )
    # G（REVIEW 2026-11-02）：走 boolenv——旧手解把 "enabled": "disabled"
    # 这类脏值静默按**开**处理（jobs.json 是手写的主机配置，拼错就该响）。
    # 语义对齐：空串/缺省 = 开（与旧实现的 "" 分支一致）。
    enabled = env_flag(f"jobs[{index}].enabled", raw.get("enabled"), True)

    sched = raw.get("schedule")
    if not isinstance(sched, dict):
        return None, f"{name}: schedule 必须是对象"
    kind = str(sched.get("kind") or "cron").strip().lower()
    if kind != "cron":
        return None, f"{name}: schedule.kind 只支持 cron，实为 {kind!r}"
    cron = str(sched.get("expr") or "").strip()
    if not cron:
        return None, f"{name}: schedule.expr 不能为空"
    # 表达式合法性与执行侧同一判据（apscheduler 的 from_crontab）
    from apscheduler.triggers.cron import CronTrigger

    try:
        trigger = CronTrigger.from_crontab(cron)
    except Exception:
        return None, f"{name}: cron 无法解析：{cron!r}"
    tz_raw = str(sched.get("tz") or "").strip()
    tz = tz_raw or None
    # 「能解析」不等于「会有下次执行」：`0 0 30 2 *`（2 月 30 日）from_crontab
    # 成功，get_next_fire_time 却永远返回 None。这类 job 一旦放行，执行体算不出
    # nextRunAtMs（返 0），而 tick 的到点判断 `if next_run_at_ms and ...` 把 0 当
    # 「首次运行」——于是每 60s 跑一次完整 LLM 回合 + 投递，无限烧 token，且 job
    # 永远不会被标记 failed（评审遗留项）。装载期就拒掉，错误走现成的 per-job
    # errors 通道，由 AgentTurnRunner.tick 逐条告警。
    if trigger.get_next_fire_time(None, agent_tz.now()) is None:
        return None, f"{name}: cron 表达式永触发（不存在满足条件的日期）：{cron!r}"
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        return None, f"{name}: payload 必须是对象"
    p_kind = str(payload.get("kind") or "").strip().lower()
    if p_kind not in _SUPPORTED_KINDS:
        return None, f"{name}: payload.kind 只支持 agent_turn，实为 {p_kind!r}"
    message = str(payload.get("message") or "")
    if not message.strip():
        return None, f"{name}: payload.message 不能为空"
    if len(message) > _MSG_MAX:
        return None, f"{name}: payload.message 过长（>{_MSG_MAX} 字）"
    channel = str(payload.get("channel") or "onebot").strip().lower()
    if channel not in _SUPPORTED_CHANNELS:
        return None, f"{name}: payload.channel 只支持 onebot，实为 {channel!r}"
    to = str(payload.get("to") or "").strip()
    if parse_target(to) is None:
        return None, (
            f"{name}: payload.to 非法（应为 group:<群号> / private:<QQ号>）：{to!r}"
        )
    # trusted/owner：轮次放权声明（语义见 CronJob 字段注释）。owner 缺失或
    # 非 ASCII 数字串 → 按「无 owner」处理，agent_turn 侧再叠加 trusted 判定。
    trusted_raw = raw.get("trusted", False)
    trusted = (
        str(trusted_raw).strip().lower() not in {"0", "false", "no", "off", ""}
        if isinstance(trusted_raw, str)
        else bool(trusted_raw)
    )
    owner = str(raw.get("owner") or "").strip()
    if owner and not (owner.isascii() and owner.isdigit()):
        return None, f"{name}: owner 非法（应为纯数字 QQ 号）：{owner!r}"

    # 显式注解 + 先取后判：内联三元会让 pyright 把 state 推成含 None 的联合。
    # OpenClaw 互拷进来的文件带着 state 字段：作运行态初值（缺省 next=0 即
    # 「加载后首个 tick 立即跑一次」），此后权威在 sidecar。
    raw_state = raw.get("state")
    state: dict = raw_state if isinstance(raw_state, dict) else {}
    job = CronJob(
        id=jid,
        name=name,
        enabled=enabled,
        cron=cron,
        tz=tz,
        message=message,
        channel=channel,
        to=to,
        delete_after_run=bool(raw.get("deleteAfterRun", False)),
        created_at_ms=_as_ms(raw.get("createdAtMs")),
        owner=owner,
        trusted=trusted,
        next_run_at_ms=_as_ms(state.get("nextRunAtMs")),
        last_run_at_ms=_as_ms(state.get("lastRunAtMs")),
        last_status=str(state.get("lastStatus") or ""),
        raw=raw,
    )
    return job, None


def _as_ms(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def parse_jobs(data: Any) -> tuple[list[CronJob], list[str]]:
    """解析整个 jobs.json 的字典；返回 (jobs, 逐条错误原因)。

    坏条目**跳过并报告**，不让一个错 job 把其余 job 一起废掉（与 push 同口径）。
    """
    if not isinstance(data, dict):
        raise JobsError("jobs.json 顶层必须是对象")
    raw_jobs = data.get(_JOBS_KEY)
    if raw_jobs is None:
        return [], []
    if not isinstance(raw_jobs, list):
        raise JobsError("jobs 必须是数组")
    jobs: list[CronJob] = []
    errors: list[str] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_jobs):
        job, err = _validate_job(raw, i)
        # 判 None 而非只判 err：契约是「二者恰有一个非 None」，只判 err 时
        # pyright 无法把 job 收窄成非空。
        if job is None or err:
            errors.append(err or f"jobs[{i}] 解析失败")
            continue
        if job.id in seen:
            errors.append(f"id 重复：{job.id}")
            continue
        seen.add(job.id)
        jobs.append(job)
    return jobs, errors


def _load_state(path: Path | None = None) -> dict[str, dict]:
    """读 sidecar 运行态；缺失/损坏返回空 dict（运行态丢了只影响显示，不炸任务）。"""
    p = path or state_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load(path: Path | None = None) -> tuple[list[CronJob], list[str]]:
    """读 jobs.json 并合并 sidecar 运行态；文件缺失返回 ([], [])（全新部署正常态）。"""
    p = path or jobs_path()
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], []
    except OSError as e:
        raise JobsError(f"读不到 {p}: {type(e).__name__}") from e
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise JobsError(f"{p} 不是合法 JSON: {e}") from e
    jobs, errors = parse_jobs(data)
    state = _load_state(p.parent / STATE_FILENAME if path else None)
    for job in jobs:
        ent = state.get(job.id)
        if isinstance(ent, dict):
            job.next_run_at_ms = _as_ms(ent.get("nextRunAtMs")) or job.next_run_at_ms
            job.last_run_at_ms = _as_ms(ent.get("lastRunAtMs")) or job.last_run_at_ms
            job.last_status = str(ent.get("lastStatus") or job.last_status)
    return jobs, errors


# ---------------------------------------------------------------- 落盘


def _atomic_write(path: Path, text: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(tmp, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _dump_job(job: CronJob, now_ms: int) -> dict:
    """导出回 OpenClaw 形态（字段名一致，两边互拷）——**纯配置，不含 state**
    （运行态在 sidecar；互拷方文件里的 state 字段导入时已作初值）。"""
    return {
        "id": job.id,
        "name": job.name,
        "enabled": job.enabled,
        # owner/trusted：AC-cron-trusted 的放权声明，落盘不能丢——否则
        # /cron on 等任意一次 save 会把全部 job 的 trusted 静默抹掉
        **({"owner": job.owner} if job.owner else {}),
        "trusted": job.trusted,
        "schedule": {
            "kind": "cron",
            "expr": job.cron,
            **({"tz": job.tz} if job.tz else {}),
        },
        "payload": {
            "kind": "agent_turn",
            "message": job.message,
            "channel": job.channel,
            "to": job.to,
        },
        "createdAtMs": job.created_at_ms or now_ms,
        "updatedAtMs": now_ms,
        "deleteAfterRun": job.delete_after_run,
    }


def save(jobs: list[CronJob], path: Path | None = None) -> None:
    """把 jobs 列表写回 jobs.json（纯配置；原子 + 0600 + 写后重读校验 + 改前备份）。

    刻意全量写：jobs 的权威形态就是整个数组，文件小（几条到几十条），增量补丁
    要处理数组下标漂移，复杂度不值。顶层其余键（用户在文件里的 `_说明` 备注、
    OpenClaw 后续版本的新字段）原样保留。
    """
    p = path or jobs_path()
    if p.is_symlink():
        raise JobsError("jobs_file_is_symlink")
    p.parent.mkdir(parents=True, exist_ok=True)
    now = _now_ms()
    try:
        current = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(current, dict):
            raise JobsError("jobs_json_not_object")
    except FileNotFoundError:
        current = {}
    except json.JSONDecodeError as e:
        raise JobsError(f"jobs_json_invalid: {type(e).__name__}") from e
    payload = dict(current)
    payload["version"] = current.get("version", 1)
    payload[_JOBS_KEY] = [_dump_job(j, now) for j in jobs]
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    backup = None
    if p.exists():
        import shutil

        bdir = p.parent / "backups"
        bdir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 10**6:06d}"
        backup = bdir / f"{p.name}.{stamp}.bak"
        shutil.copy2(p, backup)
        os.chmod(backup, 0o600)
        for old in sorted(bdir.glob(f"{p.name}.*.bak"))[:-_BACKUP_KEEP]:
            old.unlink(missing_ok=True)

    _atomic_write(p, text)
    # 写后重读校验：落盘内容必须能被原样读回（防半截文件/序列化丢字段）
    try:
        check, errs = parse_jobs(json.loads(p.read_text(encoding="utf-8")))
        assert not errs, errs
    except Exception:
        if backup is not None:
            import shutil

            shutil.copy2(backup, p)
        else:
            p.unlink(missing_ok=True)
        raise JobsError("jobs_verify_failed_rolled_back") from None
    if [j.id for j in check] != [j.id for j in jobs]:
        if backup is not None:
            import shutil

            shutil.copy2(backup, p)
        raise JobsError("jobs_verify_failed_rolled_back")
    logger.info("cron jobs saved: %d jobs", len(jobs))


def update_state(
    job_id: str,
    *,
    status: str,
    next_run_ms: int = 0,
    path: Path | None = None,
) -> None:
    """执行体回写某条 job 的运行状态到 **sidecar**（M4：不触碰 jobs.json）。

    ``path`` 与 :func:`load`/:func:`save` 同参数名，测试必须显式传入——否则会
    写到真实 state.json。失败只告警不抛：回写状态失败不该让「任务已经跑完」
    这件事本身失败。
    """
    try:
        sp = (path.parent / STATE_FILENAME) if path else state_path()
        data = _load_state(sp)
        data[job_id] = {
            "lastRunAtMs": _now_ms(),
            "lastStatus": status,
            "nextRunAtMs": int(next_run_ms),
        }
        sp.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(sp, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    except Exception:
        logger.warning("cron job %s 状态回写失败", job_id, exc_info=True)
