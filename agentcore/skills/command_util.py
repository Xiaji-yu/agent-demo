"""命令执行小工具：固定命令 + 超时 + 行数截断。

- ``run_readonly``：只读查询（运维类 skill 的查询面）。
- ``run_action``：有副作用的管理员动作（重启服务/容器等；cmd 经白名单枚举或
  正则校验，LLM 无法拼接）。
凡接受 LLM 自由参数的命令，都必须走 agentcore/workspace/runner.py 的白名单校验。
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

logger = logging.getLogger(__name__)


def _safe_env() -> dict[str, str]:
    """B2（REVIEW 2026-11-02）：子进程最小环境，不继承 bot 全环境。

    ``create_subprocess_exec`` 不传 ``env=`` 时继承当前进程环境——里面躺着
    ``LLM_API_KEY`` / ``DATABASE_URL``（含密码）等全部凭据。本模块的调用方
    （system_status 的 docker ps / tail / df 等）是固定命令，不需要它们。
    与 ``agentcore/workspace/runner.py:_minimal_env`` 同口径，但不设沙箱 HOME
    （这里的命令不是用户可控的，且 docker/journalctl 要读 /etc 下的配置）。
    """
    env = {
        "PATH": os.environ.get(
            "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        ),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    # 容器形态下 docker 客户端可能依赖这些；按需透传，不整包继承
    for k in ("DOCKER_HOST", "DOCKER_CONFIG", "SSL_CERT_FILE", "SSL_CERT_DIR"):
        if k in os.environ:
            env[k] = os.environ[k]
    return env


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
            env=_safe_env(),
            # D1（REVIEW 2026-11-02）：独立进程组，超时连孙进程一起杀
            start_new_session=True,
        )
    except FileNotFoundError:
        return f"(命令不存在: {cmd[0]})"
    except Exception as e:
        return f"(执行失败: {e})"
    try:
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            # D1（REVIEW 2026-11-02）：杀整个进程组，孙进程不成孤儿
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
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


async def run_action(
    cmd: list[str],
    *,
    timeout: int = DEFAULT_TIMEOUT,
    max_lines: int = DEFAULT_MAX_LINES,
) -> str:
    """执行**有副作用**的管理员动作命令并返回输出；失败/超时返回说明性文字。

    与 ``run_readonly`` 分开命名是刻意的：ops 类 skill 里哪些调用有副作用必须
    一眼可辨（这些 skill 也不会被加进引擎的只读并行名单）。cmd 由代码写死或
    经白名单枚举/正则校验，LLM 无法拼接任意命令。
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_safe_env(),
            # D1（REVIEW 2026-11-02）：独立进程组，超时连孙进程一起杀
            start_new_session=True,
        )
    except FileNotFoundError:
        return f"(命令不存在: {cmd[0]})"
    except Exception as e:
        return f"(执行失败: {e})"
    try:
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            # D1（REVIEW 2026-11-02）：杀整个进程组，孙进程不成孤儿
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            await proc.wait()
            return f"(命令超时 {timeout}s，已终止)"
    except Exception as e:
        logger.exception("action command failed: %s", cmd)
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
