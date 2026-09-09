"""白名单命令执行器：不经 shell、锁定工作目录、超时 + 截断 + 审计日志。"""
from __future__ import annotations

import asyncio
import logging
import os
import shlex
import shutil
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

_CMD_TIMEOUT = 20
_MAX_OUTPUT_CHARS = 4000

# 只读 git 子命令
_GIT_READONLY = {"status", "log", "diff", "show", "rev-parse", "branch", "ls-files"}

# shell 元字符：任一出现在参数里即拒绝（无法组合命令）
_SHELL_META = set(";|&`$()<>\\")
_DENIED_REDIRECT = {"|", ">", "<", "&", ";"}


def _check_path_args(args: List[str]) -> Optional[str]:
    """参数路径不得绝对化、不得越权（..）。返回值=拒绝原因或 None。"""
    for a in args:
        if a.startswith("/") or ".." in a.split("/"):
            return f"参数含绝对路径或 ..：{a!r}"
    return None


def permitted(executable: str, args: List[str]) -> Tuple[bool, str]:
    """白名单判定：返回 (是否允许, 拒绝原因)。"""
    exe = (executable or "").strip()
    if not exe:
        return False, "未指定可执行命令"
    if not shutil.which(exe):
        return False, f"命令不在系统中：{exe}"
    for a in args:
        if not a:
            continue
        if any(c in _DENIED_REDIRECT for c in a):
            return False, f"参数含 shell 元字符（不允许组合命令）：{a!r}"
        if "$(" in a or "`" in a:
            return False, "不允许命令替换"
    path_reason = _check_path_args(args)
    if path_reason:
        return False, path_reason

    if exe == "git":
        if args and args[0] not in _GIT_READONLY:
            return False, f"git 仅允许只读子命令：{sorted(_GIT_READONLY)}"
        return True, ""
    if exe in {"grep", "find", "cat", "ls", "head", "tail", "wc", "pwd"}:
        return True, ""
    if exe == "python3":
        if not args or args[0] in {"-c", "-m", "-i", "--eval"}:
            return False, "python3 仅允许运行工作区内的脚本文件，禁止 -c/-m/-i"
        return True, ""
    if exe == "node":
        if not args or args[0] in {"-e", "-p", "-i", "--eval"}:
            return False, "node 仅允许运行工作区内的脚本文件，禁止 -e/-p"
        return True, ""
    if exe == "npm":
        if args[:1] != ["run"] or len(args) < 2:
            return False, "npm 仅允许 npm run <script>"
        return True, ""
    if exe == "zip":
        return True, ""
    if exe == "unzip":
        if any(a == "-d" for a in args[:-1]):
            # 允许指定输出目录，但目录必须相对（_check_path_args 已拒绝绝对/..）
            return True, ""
        return False, "unzip 必须用 -d 指定工作区内的输出目录"
    if exe == "curl":
        urls = [a for a in args if "://" in a]
        if not urls:
            return False, "curl 需要 https URL"
        if any(not u.startswith("https://") for u in urls):
            return False, "curl 仅允许 https:// 地址"
        if "-k" in args or "--insecure" in args:
            return False, "curl 禁止跳过证书校验"
        return True, ""
    return False, f"命令不在白名单：{exe}"


class CommandRunner:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    async def run(
        self,
        user_id: str,
        executable: str,
        args: List[str],
    ) -> str:
        ok, reason = permitted(executable, args)
        if not ok:
            return f"拒绝执行：{reason}"
        cwd = self.root / user_id
        cwd.mkdir(parents=True, exist_ok=True)
        exe_path = shutil.which(executable)
        logger.info("workspace cmd: uid=%s cwd=%s cmd=%s %s", user_id, cwd, executable, args)
        try:
            proc = await asyncio.create_subprocess_exec(
                exe_path,
                *args,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=_CMD_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return f"(命令超时 {_CMD_TIMEOUT}s，已终止)"
        except Exception as e:
            logger.exception("workspace cmd failed")
            return f"(执行失败: {e})"
        text = (out or b"").decode("utf-8", errors="replace")
        if len(text) > _MAX_OUTPUT_CHARS:
            text = text[:_MAX_OUTPUT_CHARS] + f"\n…（输出过长，截断前 {_MAX_OUTPUT_CHARS} 字符）"
        return text or "(无输出)"
