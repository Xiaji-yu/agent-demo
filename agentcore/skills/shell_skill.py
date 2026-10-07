"""run_shell（**仅 high 级管理员**）：bash -c 执行任意命令行。

这是「沙箱无任意命令」不变量的**唯一显式例外**（AGENTS.md §4 / BACKLOG 有记录），
设计取舍（2026-10 管理员决策——部署者是资深运维，high 档自担风险）：

- **三层闸**：registry ``permission="superuser"``（schema 可见性）→ handler
  ``is_superuser``（env SUPERUSERS）→ ``at_least("high")``（级别）。low/medium
  档本技能**不注册**，这里只是纵深兜底。
- **最小环境执行**（与 workspace runner 同源）：绝不继承 bot 进程的完整环境——
  否则 LLM 一句 ``echo $LLM_API_KEY`` 就把密钥读进输出。需要完整环境的场景
  走 run_build_script（那边脚本内容是管理员写的，可信）。
- 保留的底线只有三道：**超时**（120s，到点杀整个进程组）、**输出截断**
  （8000B 流式 / 4000 字符）、**审计日志**（uid + 命令全文）。白名单、shell
  元字符过滤、路径遏制在 bash -c 语义下全部不适用——这就是 high 档的含义。
- 不进引擎的只读并行名单（registry 默认非只读，fail-closed）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal

from agentcore.skills.levels import at_least
from agentcore.skills.registry import SkillRegistry
from agentcore.workspace.runner import CommandRunner, _minimal_env
from agentcore.workspace.utils import is_superuser, workspace_root

logger = logging.getLogger(__name__)

_SHELL_TIMEOUT = 120  # 秒；run_command 是 20s，这里放宽——high 档常跑构建/重启类
_MAX_OUTPUT_BYTES = 8000  # 流式读取上限（字节），超出即终止进程
_MAX_OUTPUT_CHARS = 4000  # 返回给 LLM 的字符上限


async def run_shell(command: str, uid: str = "-") -> str:
    """``bash -c`` 执行一条命令行；最小环境 + 超时杀进程组 + 截断 + 审计。"""
    cmd = (command or "").strip()
    if not cmd:
        return "错误：命令为空"
    if shutil.which("bash") is None:
        return "(系统没有 bash，无法执行)"
    # 审计：命令全文进日志（与 runner 的 workspace cmd 行同风格，带 uid）。
    # L8（REVIEW-de09478..workdir）：用 %r 打印 raw 命令——bash -c 的输入可含
    # 换行，直接 %s 落盘会把一行审计记录伪造成多行（后加的字符看起来像独立
    # 日志事件）。runner 侧 workspace cmd 行同威胁已设防（_DENIED_ARG_CHARS
    # 禁换行），shell 侧因 bash -c 语义不能禁，故在审计表示上对齐：repr 会把
    # \n 转义成字面两字符，一行始终是一行。
    logger.info("run_shell: uid=%s cwd=%s cmd=%r", uid, workspace_root(), cmd)
    try:
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            cmd,
            cwd=str(workspace_root()),
            env=_minimal_env(workspace_root()),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            # 独立进程组：超时能连子子进程一起杀（npm/nohup 这类会派生的）
            start_new_session=True,
        )
    except Exception:
        logger.exception("run_shell spawn failed")
        return "(执行失败，请稍后再试)"
    try:
        data, truncated = await asyncio.wait_for(
            CommandRunner._read_output(proc), timeout=_SHELL_TIMEOUT
        )
    except TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        await proc.wait()
        return f"(命令超时 {_SHELL_TIMEOUT}s，已终止整个进程组)"
    except Exception:
        logger.exception("run_shell failed")
        proc.kill()
        await proc.wait()
        return "(执行失败，请稍后再试)"

    text = (data or b"").decode("utf-8", errors="replace")
    if truncated:
        text += f"\n…（输出过长，已截断至 {_MAX_OUTPUT_BYTES} 字节）"
    if len(text) > _MAX_OUTPUT_CHARS:
        text = (
            text[:_MAX_OUTPUT_CHARS]
            + f"\n…（输出过长，截断前 {_MAX_OUTPUT_CHARS} 字符）"
        )
    return text or "(无输出)"


def register_shell_skill(registry: SkillRegistry) -> None:
    """注册 run_shell（仅 high 级别调用方应注册；handler 内再兜两层）。"""

    @registry.register(
        "run_shell",
        "在服务器上用 bash -c 执行任意命令行（仅管理员，**high 级别专属**）。"
        "保留：120s 超时（杀整个进程组）、输出截断、审计日志；无白名单/路径限制"
        "——误操作不可回退，高危命令请先确认。构建脚本请用 run_build_script。",
        {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "完整命令行，如 systemctl status nginx",
                }
            },
            "required": ["command"],
        },
        permission="superuser",
    )
    async def run_shell_skill(command: str, user_id: str = "") -> str:
        if not is_superuser(user_id):
            return "无权限：仅管理员可执行命令。"
        if not at_least("high"):
            return (
                "当前权限级别未开放任意命令执行（需 AGENT_PERMISSION_LEVEL=high，"
                "改后重启生效）。"
            )
        return await run_shell(command, uid=user_id)
