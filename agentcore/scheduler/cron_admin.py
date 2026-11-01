"""cron 命令与 /openclaw 导入的业务逻辑（纯函数，无 nonebot 依赖）。

插件壳（plugins/qq_agent_adapter/cron_cmd.py）只做 ACL 与文本搬运；
本模块可被单测在无 nonebot 初始化的环境独立 import（P2b）。
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Any

logger = logging.getLogger(__name__)

# 两段确认的内存暂存：token → (时间戳, 数据)；5 分钟过期
_pending: dict[str, tuple[float, dict]] = {}
_PENDING_TTL = 300
# 试跑器引用：接线处（plugins）把 AgentTurnRunner 挂进来，/cron run 复用真实
# engine/sink——命令逻辑不直接持有 engine 引用。
_RUNNER_REF: list = []


def set_runner_ref(runner) -> None:
    _RUNNER_REF[0] = runner


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


def parse_cron_cmd(raw: str) -> tuple[str, str]:
    """解析 /cron 的参数，返回 (action, rest)：
    - ("list", "")         列表（空参数/未知动词都回落 list，与 /persona 同口径）
    - ("add", json_text)
    - ("del"|"on"|"off"|"run"|"confirm", job_id 或 token)
    """
    tokens = (raw or "").strip().split(None, 1)
    if not tokens:
        return "list", ""
    action = tokens[0].strip().lower()
    rest = tokens[1].strip() if len(tokens) > 1 else ""
    if action in {"add", "del", "on", "off", "run", "confirm"}:
        return action, rest
    return "list", (raw or "").strip()


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


def _apply_add(job_payload: Any, jobs_path=None) -> tuple[bool, str]:
    from agentcore.scheduler import jobs as jobs_mod

    # CronJob 对象与 dict 都接受（dataclass → dict 取运行字段）
    if not isinstance(job_payload, dict):
        job_payload = vars(job_payload)

    try:
        jobs, errors = jobs_mod.load(jobs_path)
    except jobs_mod.JobsError as e:
        return False, f"读现有配置失败：{e}"
    if any(j.id == job_payload["id"] for j in jobs):
        return False, f"id 已存在：{job_payload['id']}（先 /cron del 再加，或换 id）"
    new = dict(job_payload)
    new["enabled"] = False  # 新任务先停用：/cron run 试跑后再 on
    raw_payload = {
        "id": new["id"],
        "name": new["name"],
        "enabled": False,
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
    try:
        jobs_mod.save(jobs, jobs_path)
    except jobs_mod.JobsError as e:
        return False, f"落盘失败：{e}"
    _record("cron_updated", by="qq", action="add", job_id=new["id"])
    return True, (
        f"已新增（停用状态，试跑后再 /cron on {new['id']}）：\n"
        f"/cron run {new['id']} 试跑 · /cron on {new['id']} 启用"
    )


def run_cron_action(action: str, rest: str, jobs_path=None) -> str:
    """执行 /cron 动作并返回回复文本（与 matcher 解耦，纯函数可测）。"""
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
        _pending[token] = (__import__("time").time(), {"action": "add", "job": job})
        return (
            f"新增预览（{job.id}）：\n· 名称 {job.name}\n· 周期 {job.cron}"
            f"（{job.tz or '进程时区'}）\n· 投递 → {job.to}\n· 内容 {job.message[:120]}\n"
            f"确认落盘（停用状态）：/cron confirm {token}"
        )

    if action == "confirm":
        payload = _pop_pending(rest)
        if payload is None:
            return "确认令牌无效或已过期（5 分钟），请重新发起。"
        if payload.get("action") == "add":
            ok, msg = _apply_add(payload["job"], payload.get("jobs_path"))
            return msg
        return "未知的确认类型。"

    if not rest:
        return "缺任务 id。"
    jid = rest.strip()

    if action == "del":
        try:
            jobs, _ = jobs_mod.load(jobs_path)
        except jobs_mod.JobsError as e:
            return f"读取失败：{e}"
        kept = [j for j in jobs if j.id != jid]
        if len(kept) == len(jobs):
            return f"没有 id 为 {jid} 的任务。"
        try:
            jobs_mod.save(kept, jobs_path)
        except jobs_mod.JobsError as e:
            return f"落盘失败：{e}"
        _record("cron_updated", by="qq", action="del", job_id=jid)
        return f"已删除：{jid}"

    if action in {"on", "off"}:
        try:
            jobs, _ = jobs_mod.load(jobs_path)
        except jobs_mod.JobsError as e:
            return f"读取失败：{e}"
        hit = next((j for j in jobs if j.id == jid), None)
        if hit is None:
            return f"没有 id 为 {jid} 的任务。"
        hit.enabled = action == "on"
        try:
            jobs_mod.save(jobs, jobs_path)
        except jobs_mod.JobsError as e:
            return f"落盘失败：{e}"
        _record("cron_updated", by="qq", action=action, job_id=jid)
        return f"已{'启用' if action == 'on' else '停用'}：{jid}"

    if action == "run":
        try:
            jobs, _ = jobs_mod.load()
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

    - SOUL.md → 用户人格（frontmatter 缺 name 时取名 soul）
    - cron/jobs.json → 合并进 data/cron/jobs.json（按 id 去重，逐条报告）
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
                pm.save(name, f"OpenClaw 导入（{src.name}）", text, default=False)
                report.append(f"人格：SOUL.md → {name}")
            else:
                report.append(f"人格：{name} 已存在，跳过（不覆盖）")

    jobs_file = src / "cron" / "jobs.json"
    if jobs_file.is_file():
        try:
            incoming, errors = jobs_mod.load(jobs_file)
        except jobs_mod.JobsError as e:
            report.append(f"cron：读取失败（{e}）")
            incoming = []
        try:
            current, _ = jobs_mod.load(jobs_path)
        except jobs_mod.JobsError:
            current = []
        have = {j.id for j in current}
        added = skipped = 0
        for j in incoming:
            if j.id in have:
                skipped += 1
                continue
            current.append(j)
            have.add(j.id)
            added += 1
        try:
            jobs_mod.save(current, jobs_path)
        except jobs_mod.JobsError as e:
            report.append(f"cron：落盘失败（{e}）")
        else:
            report.append(f"cron：导入 {added} 条，跳过已存在 {skipped} 条")

    if (src / "memory").is_dir():
        report.append("memory：暂不支持自动导入（格式转换另议），文件已保留原处")

    if not report:
        return "目录里没有可识别的内容（SOUL.md / cron/jobs.json）。"
    return "\n".join(report)
