"""cron 定时任务的 LLM 自然语言技能（cron_job_add / list / cancel）。

让 superuser 可以直接在对话中用自然语言建立/管理 agent_turn 定时任务，
不再需要手写 /cron add <JSON>。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from agentcore import tz
from agentcore.scheduler.jobs import (
    CronJob,
    JobsError,
    load,
    parse_target,
    save,
)
from agentcore.scheduler.reminder import parse_when

logger = logging.getLogger(__name__)

# 5段 cron 正则粗筛
_CRON_5_RE = re.compile(
    r"^\s*([0-9*,/-]+)\s+([0-9*,/-]+)\s+([0-9*,/-]+)\s+([0-9*,/-]+)\s+([0-9*,/-]+)\s*$"
)


def _resolve_cron_schedule(when: str) -> tuple[str | None, str | None, str | None]:
    """解析时间描述为 (cron_expr, human_label, error)。

    支持：
    1. 标准 5 段 cron 表达式（如 "30 14 * * *"）
    2. 自然语言周期描述（如 "每天下午2点半"、"工作日9点"、"每周一10点"）
    """
    raw = (when or "").strip()
    if not raw:
        return None, None, "时间描述不能为空"

    # 先查是否是直接传的 5 段 cron
    if _CRON_5_RE.match(raw):
        from apscheduler.triggers.cron import CronTrigger

        try:
            CronTrigger.from_crontab(raw)
            return raw, f"cron: {raw}", None
        except Exception as e:
            return None, None, f"cron 表达式非法: {e}"

    # 尝试自然语言解析
    parsed = parse_when(raw)
    if not parsed.get("ok"):
        return None, None, f"没能理解时间「{raw}」：{parsed.get('error')}"

    cron_expr = parsed.get("cron")
    if not cron_expr:
        # 如果是单次时间（如「10分钟后」），agent_turn 架构是周期任务
        # 若需要支持一次性，可以提示用户或转为特定日期的 cron
        run_at = parsed.get("run_at")
        if run_at:
            # 转为该日期的 cron: "分 时 日 月 *"
            import datetime as dt

            dt_val = dt.datetime.fromtimestamp(run_at, tz=tz.zoneinfo())
            cron_expr = f"{dt_val.minute} {dt_val.hour} {dt_val.day} {dt_val.month} *"
            return cron_expr, f"{parsed['human']}（单次执行）", None
        return None, None, f"「{raw}」未生成有效的周期调度表达式"

    return cron_expr, parsed.get("human", raw), None


def register_cron_skills(
    registry,
    jobs_path: Path | None = None,
) -> None:
    """注册自然语言 cron 管理技能（仅 superuser 可见可用，私聊/群聊均可）。"""

    @registry.register(
        "cron_job_add",
        "为当前用户或群聊创建定时自主任务（agent_turn）。到点后 agent 会自动以指定任务书"
        "执行一轮对话，并可调用工具（发文件、执行脚本、查数据等），结果自动发到目标会话。"
        "when 支持自然语言如「每天下午2点半」「工作日9点」「每周一10点」或标准 5 段 cron 表达式（如「30 14 * * *」）；"
        "task 是交给 agent 的具体指令（如「读取工作区 memorandum.md 用 send_markdown_file 发给我」）；"
        "target 缺省时自动使用当前会话（私聊发给提问者，群聊发到当前群）。",
        {
            "type": "object",
            "properties": {
                "when": {
                    "type": "string",
                    "description": "触发时间，如「每天14:30」「工作日9点」「30 14 * * *」",
                },
                "task": {
                    "type": "string",
                    "description": "到点要执行的具体任务内容/指令说明",
                },
                "name": {
                    "type": "string",
                    "description": "任务名称（简短，如「每日备忘录」；缺省自动生成）",
                },
                "target": {
                    "type": "string",
                    "description": "投递目标会话，如 private:123456789 或 group:123456789；缺省自动推导",
                },
                "trusted": {
                    "type": "boolean",
                    "description": "是否以管理员最高权限执行任务（可调 run_shell/db_query 等；默认 true）",
                },
            },
            "required": ["when", "task"],
        },
        permission="superuser",
        sessions="all",
    )
    async def cron_job_add_skill(
        when: str,
        task: str,
        name: str = "",
        target: str = "",
        trusted: bool = True,
        user_id: str = "",
        group_id: str = "",
    ) -> str:
        if not user_id:
            return "错误：缺失当前提问者身份，无法验证权限"

        task = (task or "").strip()
        if not task:
            return "错误：任务指令 (task) 不能为空"

        cron_expr, human_time, err = _resolve_cron_schedule(when)
        if err or not cron_expr:
            return f"错误：{err}"

        # 确定目标 target
        final_target = (target or "").strip()
        if not final_target:
            if group_id:
                final_target = f"group:{group_id}"
            else:
                final_target = f"private:{user_id}"

        if parse_target(final_target) is None:
            return f"错误：目标会话格式不合法（{final_target}），应为 group:<群号> 或 private:<QQ号>"

        # 任务名称与 id
        job_name = (name or "").strip()
        if not job_name:
            job_name = task[:16].replace("\n", " ")

        from agentcore.scheduler.jobs import _slug

        job_id = _slug(job_name)

        try:
            current_jobs, _ = load(jobs_path)
        except JobsError as e:
            return f"错误：读取现有任务失败：{e}"

        # 避免 id 冲突
        base_id = job_id
        counter = 1
        existing_ids = {j.id for j in current_jobs}
        while job_id in existing_ids:
            job_id = f"{base_id}-{counter}"
            counter += 1

        new_job = CronJob(
            id=job_id,
            name=job_name,
            enabled=True,
            cron=cron_expr,
            tz=str(tz.zoneinfo()),
            message=task,
            channel="onebot",
            to=final_target,
            owner=user_id,
            trusted=trusted,
        )

        current_jobs.append(new_job)
        try:
            save(current_jobs, jobs_path)
        except JobsError as e:
            return f"错误：任务落盘失败：{e}"

        from agentcore.diagnostics import record

        record(
            "cron_updated",
            by=user_id,
            action="add",
            job_id=job_id,
            via="llm_skill",
        )

        trust_desc = "（已开启超管权限）" if trusted else "（普通沙箱权限）"
        return (
            f"已创建并启用定时任务 #{job_id}（{job_name}）：\n"
            f"· 触发周期：{human_time}（cron: {cron_expr}）\n"
            f"· 投递目标：{final_target}\n"
            f"· 权限模式：{trust_desc}\n"
            f"· 任务内容：{task}"
        )

    @registry.register(
        "cron_job_list",
        "列出当前所有的定时自主任务（agent_turn jobs）。",
        {"type": "object", "properties": {}},
        permission="superuser",
        sessions="all",
    )
    async def cron_job_list_skill(user_id: str = "") -> str:
        if not user_id:
            return "错误：缺失当前会话身份"

        try:
            jobs, _ = load(jobs_path)
        except JobsError as e:
            return f"错误：读取任务列表失败：{e}"

        if not jobs:
            return "当前没有任何定时自主任务。"

        lines = [f"当前共有 {len(jobs)} 个定时自主任务："]
        for j in jobs:
            state = "启用" if j.enabled else "停用"
            trust = " [trusted]" if j.trusted else ""
            lines.append(
                f"- #{j.id} [{state}]{trust} {j.name} | 周期: {j.cron} | 目标: {j.to} | 归属: {j.owner or '-'}"
            )
        return "\n".join(lines)

    @registry.register(
        "cron_job_cancel",
        "取消或删除一个定时自主任务，参数为任务编号 id（如从 cron_job_list 查看到的 id）。",
        {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "要取消的任务 id",
                }
            },
            "required": ["job_id"],
        },
        permission="superuser",
        sessions="all",
    )
    async def cron_job_cancel_skill(job_id: str, user_id: str = "") -> str:
        if not user_id:
            return "错误：缺失当前会话身份"

        jid = (job_id or "").strip()
        if not jid:
            return "错误：请提供要取消的任务 id"

        try:
            jobs, _ = load(jobs_path)
        except JobsError as e:
            return f"错误：读取任务列表失败：{e}"

        hit = next((j for j in jobs if j.id == jid), None)
        if not hit:
            return f"未找到编号为 {jid} 的定时任务"

        kept = [j for j in jobs if j.id != jid]
        try:
            save(kept, jobs_path)
        except JobsError as e:
            return f"错误：保存修改失败：{e}"

        from agentcore.diagnostics import record

        record(
            "cron_updated",
            by=user_id,
            action="del",
            job_id=jid,
            via="llm_skill",
        )
        return f"已成功删除定时任务 #{jid}（{hit.name}）"
