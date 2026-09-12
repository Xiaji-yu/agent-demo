"""只读命令执行小工具：固定命令 + 超时 + 行数截断。

仅用于「参数由代码写死、LLM 无法拼接」的只读查询（运维类 skill）。
凡是接受 LLM 自由参数的命令，都必须走 agentcore/workspace/runner.py 的白名单校验。
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 6
DEFAULT_MAX_LINES = 20


async def run_readonly(
    cmd: list[str],
    *,
    timeout: int = DEFAULT_TIMEOUT,
    max_lines: int = DEFAULT_MAX_LINES,
) -> str:
    """执行只读命令并返回输出；失败/超时返回说明性文字（不抛异常）。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        return f"(命令不存在: {cmd[0]})"
    except Exception as e:
        return f"(执行失败: {e})"
    try:
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return f"(命令超时 {timeout}s，已终止)"
    except Exception as e:
        logger.exception("readonly command failed: %s", cmd)
        return f"(执行失败: {e})"
    text = (out or b"").decode("utf-8", errors="replace").strip()
    lines = text.splitlines()
    if len(lines) > max_lines:
        text = (
            "\n".join(lines[:max_lines])
            + f"\n…（仅显示前 {max_lines} 行，共 {len(lines)} 行）"
        )
    return text or "(无输出)"


def which(name: str) -> bool:
    import shutil

    return shutil.which(name) is not None
