"""白名单命令执行器：不经 shell、锁定工作目录、超时 + 截断 + 审计日志。

安全边界（管理员专用入口，技能层已校验）：
- 白名单：git(只读子命令)、grep/find/cat/ls/head/tail/wc/pwd、zip/unzip(须 -d)、curl(仅 https)
- 禁止 shell 元字符（无组合）、绝对路径、..
- 已移除 python3/node/npm：运行任意脚本等效任意代码执行，风险高，管理员已禁用
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import List, Tuple

logger = logging.getLogger(__name__)

_CMD_TIMEOUT = 20
_MAX_OUTPUT_CHARS = 4000

# 只读 git 子命令
_GIT_READONLY = {"status", "log", "diff", "show", "rev-parse", "branch", "ls-files"}

_DENIED_REDIRECT = {"|", ">", "<", "&", ";"}


def _check_path_args(args: List[str]) -> Tuple[bool, str]:
    """参数路径不得绝对化、不得越权（..）。"""
    for a in args:
        if a.startswith("/") or ".." in a.split("/"):
            return False, f"参数含绝对路径或 ..：{a!r}"
    return True, ""


def permitted(executable: str, args: List[str]) -> Tuple[bool, str]:
    """白名单判定：返回 (是否允许, 拒绝原因)。"""
    exe = (executable or "").strip()
    if not exe:
        return False, "未指定可执行命令"
    for a in args:
        if not a:
            continue
        if any(c in _DENIED_REDIRECT for c in a):
            return False, f"参数含 shell 元字符（不允许组合命令）：{a!r}"
        if "$(" in a or "`" in a:
            return False, "不允许命令替换"
    if not shutil.which(exe):
        return False, f"命令不在系统中：{exe}"
    ok_path, reason = _check_path_args(args)
    if not ok_path:
        return False, reason

    if exe == "git":
        if args and args[0] not in _GIT_READONLY:
            return False, f"git 仅允许只读子命令：{sorted(_GIT_READONLY)}"
        return True, ""
    if exe in {"grep", "find", "cat", "ls", "head", "tail", "wc", "pwd"}:
        return True, ""
    if exe == "zip":
        return True, ""
    if exe == "unzip":
        if "-d" in args:
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

    async def run(self, executable: str, args: List[str]) -> str:
        ok, reason = permitted(executable, args)
        if not ok:
            return f"拒绝执行：{reason}"
        self.root.mkdir(parents=True, exist_ok=True)
        exe_path = shutil.which(executable)
        logger.info("workspace cmd: cwd=%s cmd=%s %s", self.root, executable, args)
        try:
            proc = await asyncio.create_subprocess_exec(
                exe_path,
                *args,
                cwd=str(self.root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=_CMD_TIMEOUT)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return f"(命令超时 {_CMD_TIMEOUT}s，已终止)"
        except Exception:
            logger.exception("workspace cmd failed")
            return "(执行失败，请稍后再试)"
        text = (out or b"").decode("utf-8", errors="replace")
        if len(text) > _MAX_OUTPUT_CHARS:
            text = text[:_MAX_OUTPUT_CHARS] + f"\n…（输出过长，截断前 {_MAX_OUTPUT_CHARS} 字符）"
        return text or "(无输出)"
