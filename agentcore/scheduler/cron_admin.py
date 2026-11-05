"""cron 命令与 /openclaw 导入的业务逻辑（纯函数，无 nonebot 依赖）。

插件壳（plugins/qq_agent_adapter/cron_cmd.py）只做 ACL 与文本搬运；
本模块可被单测在无 nonebot 初始化的环境独立 import（P2b）。
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from typing import Any

logger = logging.getLogger(__name__)

# 两段确认的内存暂存：token → (时间戳, 数据)；5 分钟过期
_pending: dict[str, tuple[float, dict]] = {}
_PENDING_TTL = 300
# 试跑器引用：接线处（plugins）把 AgentTurnRunner 挂进来，/cron run 复用真实
# engine/sink——命令逻辑不直接持有 engine 引用。
_RUNNER_REF: list = [None]


def set_runner_ref(runner) -> None:
    _RUNNER_REF.clear()
    _RUNNER_REF.append(runner)


def _confirm_token() -> str:
    return secrets.token_hex(8)


def _pop_pending(token: str) -> dict | None:

    ent = _pending.pop(token, None)
    if ent is None:
        return None
    at, payload = ent
    if time.time() - at > _PENDING_TTL:
        return None
    return payload


_CMD_PREFIX_RE = re.compile(r"^[/!！]?(cron|定时任务)[\s/]*", re.IGNORECASE)
_OPENCLAW_CMD_RE = re.compile(r"^[/!！]?(openclaw)[\s/]*", re.IGNORECASE)


def parse_cron_cmd(raw: str) -> tuple[str, str]:
    """解析 /cron 的参数，返回 (action, rest)：
    - ("list", "")         列表（空参数/未知动词都回落 list，与 /persona 同口径）
    - ("add", json_text)
    - ("del"|"on"|"off"|"run"|"confirm", job_id 或 token)

    入参可能是**完整消息**（matcher 传 ``str(event.get_message())``，含
    ``/cron`` 前缀），也可能是裸参数（单测）。不剥前缀时 ``/cron on xxx``
    的首 token 是 ``/cron`` → 永远回落 list，所有子命令静默失效
    （2026-11-06 线上「``/cron on`` 始终开不了任务」）。与 /kb /memory
    /persona 的前缀剥离同口径。
    """
    text = _CMD_PREFIX_RE.sub("", (raw or "").strip()).strip()
    tokens = text.split(None, 1)
    if not tokens:
        return "list", ""
    action = tokens[0].strip().lower()
    rest = tokens[1].strip() if len(tokens) > 1 else ""
    if action in {"add", "del", "on", "off", "run", "confirm"}:
        return action, rest
    return "list", text


def parse_openclaw_cmd(raw: str) -> str | None:
    """从完整消息/裸参数里取 ``import <目录>`` 的目录；用法不对返回 None。

    与 :func:`parse_cron_cmd` 同源 bug：matcher 传的是完整消息，直接
    ``startswith("import")`` 永远为假 → ``/openclaw import ...`` 只会回用法。
    """
    text = _OPENCLAW_CMD_RE.sub("", (raw or "").strip()).strip()
    if not text.startswith("import"):
        return None
    return text[len("import") :].strip()


def _parse_job_payload(text: str) -> tuple[Any | None, str | None]:
    """把 add 的 JSON 文本解析成单条 job 配置；返回 (job, 错误)。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return None, f"不是合法 JSON: {e}"
    from agentcore.scheduler import jobs as jobs_mod

    jobs, errors = jobs_mod.parse_jobs({"jobs": [data]})
    if errors:
        return None, errors[0]
    if not jobs:
        return None, "没有可解析的任务"
    return jobs[0], None


def _record(kind: str, **kw) -> None:
    from agentcore.diagnostics import record

    record(kind, **kw)


def _fmt_list(manager, jobs_path=None) -> str:
    from agentcore.scheduler import jobs as jobs_mod

    try:
        jobs, errors = jobs_mod.load(jobs_path)
    except jobs_mod.JobsError as e:
        return f"cron 配置读取失败：{e}"
    if errors:
        head = "\n".join("· " + e for e in errors[:5]) + "\n"
    else:
        head = ""
    if not jobs:
        return (
            (head or "")
            + "当前没有 cron 任务。\n模板：docs/examples/cron-jobs.example.json\n"
            "新增：/cron add <JSON>（见模板字段）"
        )
    lines = []
    from agentcore.tz import fmt_ts

    for j in jobs:
        state = "✓" if j.enabled else "停用"
        next_run = (
            f"，下次 {fmt_ts(j.next_run_at_ms / 1000, '%m-%d %H:%M')}"
            if j.next_run_at_ms
            else ""
        )
        last = f"，上次 {j.last_status}" if j.last_status else ""
        lines.append(f"· [{state}] {j.name}（{j.id}）{j.cron} → {j.to}{next_run}{last}")
    return head + "\n".join(lines)


def _apply_add(job_payload: Any, jobs_path=None, owner: str = "") -> tuple[bool, str]:
    from agentcore.scheduler import jobs as jobs_mod

    # CronJob 对象与 dict 都接受（dataclass → dict 取运行字段）
    if not isinstance(job_payload, dict):
        job_payload = vars(job_payload)

    # M9：load→改→save 是全量写，整个临界区进**一把**锁（内不得 await，见
    # jobs._LOCKS 的形态说明）。id 查重也必须读锁内的列表——锁外读到的现有 id
    # 集合可能已过期，两个轮次会放过同一个 id。
    try:
        with jobs_mod.write_lock(jobs_path):
            jobs, errors = jobs_mod.load(jobs_path)
            if any(j.id == job_payload["id"] for j in jobs):
                return False, (
                    f"id 已存在：{job_payload['id']}（先 /cron del 再加，或换 id）"
                )
            new = dict(job_payload)
            new["enabled"] = False  # 新任务先停用：/cron run 试跑后再 on
            raw_payload = {
                "id": new["id"],
                "name": new["name"],
                "enabled": False,
                # owner：/cron add 的操作者（superuser）——trusted 轮次的权限身份来源
                **({"owner": owner} if owner else {}),
                # trusted：轮次放权声明（默认 False；见 CronJob 字段注释）
                "trusted": bool(new.get("trusted", False)),
                "schedule": {
                    "kind": "cron",
                    "expr": new["cron"],
                    **({"tz": new["tz"]} if new.get("tz") else {}),
                },
                "payload": {
                    "kind": "agent_turn",
                    "message": new["message"],
                    "channel": new["channel"],
                    "to": new["to"],
                },
            }
            parsed, errors = jobs_mod.parse_jobs({"jobs": [raw_payload]})
            if errors or not parsed:
                return False, "；".join(errors) or "解析失败"
            jobs.append(parsed[0])
            jobs_mod.save(jobs, jobs_path)
    except jobs_mod.JobsError as e:
        return False, f"任务配置读写失败：{e}"
    _record("cron_updated", by=owner or "qq", action="add", job_id=new["id"])
    return True, (
        f"已新增（停用状态，试跑后再 /cron on {new['id']}）：\n"
        f"/cron run {new['id']} 试跑 · /cron on {new['id']} 启用"
    )


def run_cron_action(action: str, rest: str, jobs_path=None, owner: str = "") -> str:
    """执行 /cron 动作并返回回复文本（与 matcher 解耦，纯函数可测）。

    ``owner``：操作者 QQ（superuser），落进 job 的 owner 字段（trusted 轮次的
    权限身份来源）与审计 ``by``。
    """
    from agentcore.scheduler import jobs as jobs_mod

    if action == "list":
        return _fmt_list(None, jobs_path)

    if action == "add":
        if not rest:
            return (
                "用法：/cron add <JSON>（字段见 docs/examples/cron-jobs.example.json）"
            )
        job, err = _parse_job_payload(rest)
        if job is None:
            return f"解析失败：{err}"
        token = _confirm_token()
        _pending[token] = (
            __import__("time").time(),
            {
                "action": "add",
                "job": job,
                "owner": owner,
                # add 与 confirm 必须同一落盘目标：不透传的话 confirm 会
                # 写到默认路径（测试/多实例部署下路径分裂）
                "jobs_path": jobs_path,
            },
        )
        trust_note = (
            "· ⚠ trusted：到点轮次将以你的权限执行（可调 superuser 技能）\n"
            if getattr(job, "trusted", False)
            else ""
        )
        return (
            f"新增预览（{job.id}）：\n· 名称 {job.name}\n· 周期 {job.cron}"
            f"（{job.tz or '进程时区'}）\n· 投递 → {job.to}\n{trust_note}"
            f"· 内容 {job.message[:120]}\n"
            f"确认落盘（停用状态）：/cron confirm {token}"
        )

    if action == "confirm":
        payload = _pop_pending(rest)
        if payload is None:
            return "确认令牌无效或已过期（5 分钟），请重新发起。"
        if payload.get("action") == "add":
            ok, msg = _apply_add(
                payload["job"], payload.get("jobs_path"), owner=payload.get("owner", "")
            )
            return msg
        return "未知的确认类型。"

    if not rest:
        return "缺任务 id。"
    jid = rest.strip()

    if action == "del":
        # M9：load→改→save 临界区进锁（内不得 await，见 jobs._LOCKS）
        try:
            with jobs_mod.write_lock(jobs_path):
                jobs, _ = jobs_mod.load(jobs_path)
                kept = [j for j in jobs if j.id != jid]
                if len(kept) == len(jobs):
                    return f"没有 id 为 {jid} 的任务。"
                jobs_mod.save(kept, jobs_path)
        except jobs_mod.JobsError as e:
            return f"任务配置读写失败：{e}"
        _record("cron_updated", by=owner or "qq", action="del", job_id=jid)
        return f"已删除：{jid}"

    if action in {"on", "off"}:
        # M9：同上——enabled 翻转与落盘必须在同一把锁内
        try:
            with jobs_mod.write_lock(jobs_path):
                jobs, _ = jobs_mod.load(jobs_path)
                hit = next((j for j in jobs if j.id == jid), None)
                if hit is None:
                    return f"没有 id 为 {jid} 的任务。"
                hit.enabled = action == "on"
                jobs_mod.save(jobs, jobs_path)
        except jobs_mod.JobsError as e:
            return f"任务配置读写失败：{e}"
        _record("cron_updated", by=owner or "qq", action=action, job_id=jid)
        return f"已{'启用' if action == 'on' else '停用'}：{jid}"

    if action == "run":
        try:
            # 线1-L1（REVIEW-6867995..e079b8d）：六个读写动作里这里曾是唯一
            # 漏透传 jobs_path 的——测试/多实例部署下会去读默认路径
            jobs, _ = jobs_mod.load(jobs_path)
        except jobs_mod.JobsError as e:
            return f"读取失败：{e}"
        hit = next((j for j in jobs if j.id == jid), None)
        if hit is None:
            return f"没有 id 为 {jid} 的任务。"
        runner = _RUNNER_REF[0]
        if runner is None:
            return "试跑器未接线（bot 尚未完成启动）。"
        # 后台试跑（run_job 可能要几十秒），立即回执；结果投递到配置的目标，
        # 状态看 /cron 列表
        import asyncio

        asyncio.get_running_loop().create_task(runner.run_job(hit))
        return f"试跑已发起（{jid}）：结果将投递到 {hit.to}，完成后状态见 /cron。"

    return f"未知动作：{action}"


def import_openclaw(root: str, *, persona_manager=None, jobs_path=None) -> str:
    """扫描 OpenClaw 工作区目录并分拣导入；返回报告文本。

    - SOUL.md → 用户人格（frontmatter 缺 name 时取名 soul；name 含人格名
      非法字符时折叠为合法名并在报告注明）
    - cron/jobs.json → 合并进 data/cron/jobs.json（按 id 去重，逐条报告；
      导入条目一律先停用——/cron run 试跑认可后再 /cron on）
    - memory/ 暂不支持（格式转换另议）
    全程只增不改：已存在的同名人格/id 一律跳过并报告，绝不覆盖现网配置。
    """
    import re as _re
    from pathlib import Path as _Path

    from agentcore.scheduler import jobs as jobs_mod

    src = _Path(root).expanduser()
    if not src.is_dir():
        return f"目录不存在：{src}"
    report: list[str] = []

    soul = src / "SOUL.md"
    if soul.is_file():
        text = soul.read_text(encoding="utf-8", errors="replace")
        m = _re.search(r"^name:\s*(.+)$", text, _re.MULTILINE)
        name = (m.group(1).strip() if m else "soul") or "soul"
        if persona_manager is not None:
            pm = persona_manager
            if pm.get(name) is None:
                # M4（REVIEW-63f9444..858afcc）：人格名只允许 A-Za-z0-9_-，
                # 中文名是 OpenClaw 迁移最常见形态——旧实现直接把原名交给
                # pm.save 抛 ValueError 且无人捕获，命令崩溃、无回复、无审计。
                # 折叠非法字符（纯非法回落 soul）再落盘，报告里说明改名。
                safe = _re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") or "soul"
                try:
                    pm.save(safe, f"OpenClaw 导入（{src.name}）", text, default=False)
                except ValueError as e:
                    report.append(f"人格：{name} 落盘失败，已跳过（{e}）")
                else:
                    if safe != name:
                        report.append(
                            f"人格：SOUL.md → {safe}（name 含非法字符，已折叠）"
                        )
                    else:
                        report.append(f"人格：SOUL.md → {safe}")
            else:
                report.append(f"人格：{name} 已存在，跳过（不覆盖）")

    jobs_file = src / "cron" / "jobs.json"
    if jobs_file.is_file():
        try:
            incoming, errors = jobs_mod.load(jobs_file)
        except jobs_mod.JobsError as e:
            report.append(f"cron：读取失败（{e}）")
            incoming = []
        # M9：导入也是 load→append→save 的全量写，临界区进锁（内不得 await）。
        # 入站文件的解析留在锁外——它不碰 jobs.json。
        try:
            with jobs_mod.write_lock(jobs_path):
                current, _ = jobs_mod.load(jobs_path)
                have = {j.id for j in current}
                added = skipped = 0
                for j in incoming:
                    if j.id in have:
                        skipped += 1
                        continue
                    # M8（REVIEW-63f9444..858afcc）：导入一律先停用——无人审核的导入
                    # 条目（enabled=true + 无 state 即 next=0）会在下个 tick 立即向
                    # 目标投递 LLM 产物，与 _apply_add「先停用试跑再启用」同一姿态。
                    j.enabled = False
                    current.append(j)
                    have.add(j.id)
                    added += 1
                jobs_mod.save(current, jobs_path)
        except jobs_mod.JobsError as e:
            report.append(f"cron：落盘失败（{e}）")
        else:
            note = "（均已停用：/cron run 试跑后再 /cron on 逐条启用）" if added else ""
            report.append(f"cron：导入 {added} 条{note}，跳过已存在 {skipped} 条")

    if (src / "memory").is_dir():
        report.append("memory：暂不支持自动导入（格式转换另议），文件已保留原处")

    if not report:
        return "目录里没有可识别的内容（SOUL.md / cron/jobs.json）。"
    return "\n".join(report)
