"""白名单命令执行器：不经 shell、锁定工作目录、超时 + 截断 + 审计日志。

安全边界（管理员专用入口，技能层已校验）——按「命令 + 逐参数」双层校验：
- 全局：参数不得含 shell 元字符/命令替换；凡含 ``/`` 的参数（含 ``--opt=/path``
  与 ``-f/path`` 形式）resolve 后必须仍在工作区内；``..`` 一律拒绝
- find 仅允许搜索类动作（拒绝 -exec/-execdir/-ok/-okdir/-delete/-fls/-fprint* 等）
- git 仅只读子命令 + 安全 flag（拒绝 -c/--ext-diff/--textconv/--output 等一切
  可写文件或执行外部程序的选项）
- curl 收敛为 GET-only 参数集（无 -o/-T/-d/-F/-H/-L 等）
- unzip 必须 -d 指定输出目录，执行后清除解压出的符号链接（防链接逃逸读写）
- 子进程使用最小化环境变量（不继承 LLM API key 等），输出流式截断
- 审计日志带操作者 uid

根治方案（容器/独立低权用户）待运维落地；在此之前以本文件为安全边界。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

_CMD_TIMEOUT = 20
_MAX_OUTPUT_BYTES = 8000  # 流式读取上限（字节），超出即终止进程
_MAX_OUTPUT_CHARS = 4000  # 返回给 LLM 的字符上限

# 只读 git 子命令
_GIT_READONLY = {"status", "log", "diff", "show", "rev-parse", "branch", "ls-files"}

# git 安全 flag：写文件（--output）、执行外部程序（--ext-diff/--textconv）、
# 注入配置（-c）、更换二进制（--exec-path）等一律不在表内
_GIT_SAFE_FLAGS = {
    "--oneline", "--stat", "--short", "--graph", "--decorate", "--abbrev-commit",
    "--name-only", "--name-status", "--no-color", "--all", "--follow", "--tags",
    "--branches", "--remotes", "--patch", "--no-patch", "--abbrev", "--no-abbrev",
    "--pretty", "--format", "--date", "--max-count", "--skip", "--since", "--until",
    "--author", "--grep", "--fixed-strings", "--extended-regexp", "--invert-match",
    "--word-regexp", "--column", "--summary", "-n", "-a", "-v", "--verbose",
}
_GIT_SAFE_FLAG_KEYS = {
    "--pretty", "--format", "--date", "--max-count", "--skip", "--since",
    "--until", "--author", "--grep", "--abbrev",
}
# git log -5 / -n5 之类的数字短选项
_GIT_NUM_RE = re.compile(r"^-\d+$")

# find 仅放行搜索类动作；任何能执行命令/删除/落盘的动作都不可用
_FIND_SAFE_OPTIONS = {
    "--version", "-H", "-L", "-P",
    "-name", "-iname", "-lname", "-type", "-maxdepth", "-mindepth", "-mtime",
    "-mmin", "-size", "-newer", "-empty", "-true", "-false", "-print", "-print0",
    "-depth", "-follow", "-and", "-or", "-not", "!", "-a", "-o", "-writable",
    "-readable", "-nouser", "-nogroup",
}

# curl 收敛为 GET-only：只读展示 + 数值型超时/大小限制
_CURL_SAFE_FLAGS = {
    "-s", "-S", "-i", "-I", "-v", "--head", "--silent", "--show-error",
    "--compressed", "--http1.1", "--http2",
}
_CURL_SAFE_FLAG_KEYS = {"--max-time", "--connect-timeout", "--max-filesize"}

_DENIED_REDIRECT = {"|", ">", "<", "&", ";"}


def _denied_for_shell(args: list[str]) -> str | None:
    """组合命令/命令替换类拒绝。"""
    for a in args:
        if not a:
            continue
        if any(c in _DENIED_REDIRECT for c in a):
            return f"参数含 shell 元字符（不允许组合命令）：{a!r}"
        if "$(" in a or "`" in a:
            return "不允许命令替换"
    return None


def _path_candidate(a: str) -> str | None:
    """提取参数中需要做路径遏制检查的候选路径；None 表示无需检查。

    - URL（含 ://）不检查
    - ``--opt=/path`` 取 ``=`` 后的值；``-f/path`` 取第一个 ``/`` 起的子串
      （``-f`` 可直接附着文件名，如 grep -f/etc/passwd）
    - 普通参数本身含 ``/`` 则整个参数是路径
    """
    if "://" in a:
        return None
    if a.startswith("-"):
        if "=" in a:
            return a.split("=", 1)[1] or None
        if "/" in a:
            return a[a.index("/"):]
        return None
    return a if "/" in a else None


def _check_path_args(args: list[str], root: Path | None = None) -> tuple[bool, str]:
    """含 / 的参数必须落在工作区内；.. 一律拒绝。root 缺省时仅做词法检查。"""
    for a in args:
        if not a:
            continue
        if ".." in a.split("/"):
            return False, f"参数含 ..：{a!r}"
        cand = _path_candidate(a)
        if cand is None:
            continue
        if not root:
            if cand.startswith("/"):
                return False, f"参数含绝对路径：{a!r}"
            continue
        try:
            p = (root / cand).resolve()
        except Exception:
            return False, f"参数无法解析为安全路径：{a!r}"
        if p != root and root not in p.parents:
            return False, f"参数路径超出工作区：{a!r}"
    return True, ""


def permitted(
    executable: str, args: list[str], root: str | Path | None = None
) -> tuple[bool, str]:
    """白名单判定：返回 (是否允许, 拒绝原因)。root 提供时启用 resolve 级路径遏制。"""
    exe = (executable or "").strip()
    if not exe:
        return False, "未指定可执行命令"
    if "/" in exe:
        return False, "命令名不允许带路径"
    if not shutil.which(exe):
        return False, f"命令不在系统中：{exe}"
    reason = _denied_for_shell(args)
    if reason:
        return False, reason
    root_path = Path(root).resolve() if root else None
    ok_path, reason = _check_path_args(args, root_path)
    if not ok_path:
        return False, reason

    if exe == "git":
        if not args:
            return False, "git 需要子命令"
        if args[0] not in _GIT_READONLY:
            return False, f"git 仅允许只读子命令：{sorted(_GIT_READONLY)}"
        for a in args[1:]:
            if not a.startswith("-"):
                continue
            if a in _GIT_SAFE_FLAGS or _GIT_NUM_RE.match(a):
                continue
            if "=" in a and a.split("=", 1)[0] in _GIT_SAFE_FLAG_KEYS:
                continue
            return False, f"git 不允许该选项（仅白名单内安全选项）：{a!r}"
        return True, ""
    if exe in {"grep", "cat", "ls", "head", "tail", "wc", "pwd"}:
        # 纯读命令：无执行/落盘选项，全局路径遏制已覆盖
        return True, ""
    if exe == "find":
        for a in args:
            if a.startswith("-") and a not in _FIND_SAFE_OPTIONS:
                return False, (
                    f"find 不允许该选项（禁止 -exec/-delete/-fprint 等动作）：{a!r}"
                )
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
        for a in args:
            if not a.startswith("-") or "://" in a:
                continue
            if a in _CURL_SAFE_FLAGS:
                continue
            if "=" in a and a.split("=", 1)[0] in _CURL_SAFE_FLAG_KEYS:
                continue
            return False, f"curl 仅允许 GET-only 安全参数（无输出/上传/表单/头）：{a!r}"
        return True, ""
    return False, f"命令不在白名单：{exe}"


def _minimal_env(root: Path) -> dict:
    """子进程最小环境：不继承 shell 环境（避免泄漏 LLM API key 等）。"""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
        "HOME": str(root),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    for k in ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE"):
        if k in os.environ:
            env[k] = os.environ[k]
    return env


def strip_symlinks(root: Path) -> int:
    """递归删除 root 下所有符号链接（unzip 解压后调用，防链接逃逸）。返回删除数。"""
    removed = 0
    if not root.is_dir():
        return 0
    for p in sorted(root.rglob("*")):
        try:
            if p.is_symlink():
                p.unlink()
                removed += 1
                logger.warning("removed symlink escaped into workspace: %s", p)
        except OSError:
            logger.exception("failed to remove symlink: %s", p)
    return removed


class CommandRunner:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def check(self, executable: str, args: list[str]) -> tuple[bool, str]:
        """带本 runner 工作区根的 permitted()。"""
        return permitted(executable, args, root=self.root)

    async def run(self, executable: str, args: list[str], uid: str = "-") -> str:
        ok, reason = self.check(executable, args)
        if not ok:
            return f"拒绝执行：{reason}"
        self.root.mkdir(parents=True, exist_ok=True)
        exe_path = shutil.which(executable)
        logger.info("workspace cmd: uid=%s cwd=%s cmd=%s %s", uid, self.root, executable, args)
        try:
            proc = await asyncio.create_subprocess_exec(
                exe_path,
                *args,
                cwd=str(self.root),
                env=_minimal_env(self.root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception:
            logger.exception("workspace cmd spawn failed")
            return "(执行失败，请稍后再试)"
        try:
            data, truncated = await asyncio.wait_for(
                self._read_output(proc), timeout=_CMD_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return f"(命令超时 {_CMD_TIMEOUT}s，已终止)"
        except Exception:
            logger.exception("workspace cmd failed")
            proc.kill()
            await proc.wait()
            return "(执行失败，请稍后再试)"
        text = (data or b"").decode("utf-8", errors="replace")
        if truncated:
            text += f"\n…（输出过长，已截断至 {_MAX_OUTPUT_BYTES} 字节）"
        if len(text) > _MAX_OUTPUT_CHARS:
            text = text[:_MAX_OUTPUT_CHARS] + f"\n…（输出过长，截断前 {_MAX_OUTPUT_CHARS} 字符）"
        return text or "(无输出)"

    @staticmethod
    async def _read_output(proc) -> tuple[bytes, bool]:
        """流式读取 stdout，超限立即终止进程（避免全量缓冲大输出）。"""
        buf = b""
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                return buf, False
            buf += chunk
            if len(buf) > _MAX_OUTPUT_BYTES:
                proc.kill()
                await proc.wait()
                return buf[:_MAX_OUTPUT_BYTES], True
