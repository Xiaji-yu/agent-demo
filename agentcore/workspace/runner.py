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

**配置注入面的封堵（H1）**——命令的执行行为受配置文件影响，仅校验「命令 + 参数」
并不足够，攻击者只要能在工作区落一个配置文件即可绕过全部参数校验：

1. 全局/系统层：``GIT_CONFIG_GLOBAL`` / ``GIT_CONFIG_SYSTEM`` 指向 ``/dev/null``，
   ``GIT_CONFIG_NOSYSTEM=1``；``HOME`` 不再指向可写工作区而是专用沙箱目录
   （``$HOME/.gitconfig``、``$HOME/.curlrc`` 曾可被 fs_write 写入）
2. 命令层：所有 git 调用前缀注入 ``_GIT_HARDENING``（``-c`` 覆盖优先级高于
   repo-local 配置），并对 ``git diff`` 追加 ``--no-ext-diff``
3. 仓库层（``-c`` 单例覆盖无法穷举）：``_repo_exec_guard`` 在 spawn 前检查
   工作区仓库是否声明了**可执行外部命令的驱动**（``filter.*.clean|smudge|process``、
   ``diff.*.command|textconv``、``include.path``、``.gitattributes`` 里的
   ``filter=``/``diff=`` 属性），命中即拒绝执行并说明原因（fail-closed）

已知残留风险：git 的配置驱动执行面较宽，第 3 层为「拒绝已知形态」而非完备证明。
根治方案仍是容器/独立低权用户，待运维落地；在此之前以本文件为安全边界。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# 用户可见结果标记：生产与测试共用（文案改动只需改这里）
MSG_REFUSED = "拒绝执行"

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

# 路径分隔符：'/' 与 '\\' 同等对待（Windows 下仅按 '/' 分词会漏判，见 L1）
_SEP_RE = re.compile(r"[/\\]")
_ABS_WIN_RE = re.compile(r"^(?:[A-Za-z]:|[\\/])")

# ---------------------------------------------------------------------------
# H1：配置注入面封堵
# ---------------------------------------------------------------------------
# git 命令层加固：``-c`` 的优先级高于 repo-local 配置，可覆盖已知「会执行外部命令
# 或改写落盘位置」的单例配置键。（无法穷举的驱动型配置由 _git_exec_guard 兜底）
_GIT_HARDENING: tuple[str, ...] = (
    "-c", "core.fsmonitor=false",
    "-c", f"core.hooksPath={os.devnull}",
    "-c", "core.pager=cat",
    "-c", "core.editor=false",
    "-c", "sequence.editor=false",
    "-c", "core.sshCommand=false",
    "-c", "core.gitProxy=",
    "-c", "core.alternatesRefsCommand=",
    "-c", "credential.helper=",
    "-c", "diff.external=",
    "-c", "protocol.ext.allow=never",
    "-c", "protocol.file.allow=never",
)

# git 环境层加固：全局/系统配置、外部 diff、分页器、交互提示
_GIT_ENV_HARDENING = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_EXTERNAL_DIFF": "",
    "GIT_PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
}

# 仓库层守卫：这些配置键/属性会让 git 执行外部命令，且驱动名任意、无法用 -c 穷举
_GIT_EXEC_CONFIG_KEY_RE = re.compile(
    r"^(?:filter\.[^.]+\.(?:clean|smudge|process)"
    r"|diff\.[^.]+\.(?:command|textconv)"
    r"|core\.(?:fsmonitor|pager|editor|sshcommand|hookspath|gitproxy|alternatesrefscommand)"
    r"|sequence\.editor|credential\.helper|include\.path|includeif\.[^.]*\.path)$"
)
_GIT_EXEC_ATTR_RE = re.compile(r"(?:^|\s)(?:filter|diff)=", re.IGNORECASE)
_CFG_SECTION_RE = re.compile(r'^\s*\[\s*([A-Za-z0-9._-]+)\s*(?:"(.*?)")?\s*\]\s*$')
_CFG_KV_RE = re.compile(r"^\s*([A-Za-z0-9._-]+)\s*=\s*(.*)$")
_MAX_ATTR_SCAN = 2000


def _parse_git_config_keys(text: str) -> set[str]:
    """粗略解析 git config 文本，返回小写的 ``section[.subsection].key`` 集合。

    只用于识别危险键，不求完整实现（不处理 include 展开与多行续行）。
    """
    keys: set[str] = set()
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        m = _CFG_SECTION_RE.match(line)
        if m:
            section = m.group(1).lower()
            if m.group(2):
                section += "." + m.group(2).lower()
            continue
        m = _CFG_KV_RE.match(line)
        if m and section:
            keys.add(f"{section}.{m.group(1).lower()}")
    return keys


def _resolve_gitdir(root: Path) -> Path | None:
    """定位仓库的 git 目录：``.git`` 目录，或 worktree/submodule 的 ``gitdir:`` 指针。"""
    dotgit = root / ".git"
    if dotgit.is_dir():
        return dotgit
    if dotgit.is_file():
        try:
            line = dotgit.read_text(errors="replace").strip()
        except OSError:
            return None
        if line.lower().startswith("gitdir:"):
            target = line.split(":", 1)[1].strip()
            p = Path(target)
            return p if p.is_absolute() else (root / p).resolve()
    return None


def _find_attributes_files(root: Path, gitdir: Path | None) -> list[Path]:
    """收集工作区内的 ``.gitattributes``（有上限，避免超大工作区拖慢）。"""
    found: list[Path] = []
    if not root.is_dir():
        return found
    seen = 0
    try:
        for p in root.rglob(".gitattributes"):
            seen += 1
            if seen > _MAX_ATTR_SCAN:
                logger.warning("attributes scan exceeded %s entries", _MAX_ATTR_SCAN)
                break
            if p.is_file():
                found.append(p)
    except OSError:
        logger.exception("attributes scan failed")
    return found


def _git_exec_guard(root: Path) -> str:
    """检测工作区仓库是否声明了「可执行外部命令」的驱动；返回拒绝原因，空串放行。

    已验证的真实逃逸（H1）：``.gitattributes`` 里 ``* filter=evil`` + repo-local
    ``filter.evil.clean = sh -c '...'``，即可让白名单内的 ``git diff`` 执行任意命令。
    我们无法预知驱动名，故 fail-closed：命中即拒绝在该仓库执行 git。

    性能：``filter=`` / ``diff=`` 这类 attribute 只有在**配置里定义了对应驱动**时才可能
    被执行（全局/系统配置已被环境层关闭），因此先查配置；配置里没有驱动定义就跳过
    递归扫描 attributes——避免每次 git 调用都遍历整个工作区。
    """
    gitdir = _resolve_gitdir(root)
    if gitdir is None:
        return ""  # 非仓库：git 自身会报错，无需守卫
    hits: list[str] = []
    for name in ("config", "config.worktree"):
        cfg = gitdir / name
        if not cfg.is_file():
            continue
        try:
            keys = _parse_git_config_keys(cfg.read_text(errors="replace"))
        except OSError:
            return "仓库配置无法读取，已拒绝执行 git（fail-closed）"
        for k in sorted(keys):
            if _GIT_EXEC_CONFIG_KEY_RE.match(k):
                hits.append(f"{name}: {k}")
    if hits:
        # 配置里确实有驱动定义：再查 attributes 以给出更完整的拒绝原因
        for p in [gitdir / "info" / "attributes", *_find_attributes_files(root, gitdir)]:
            if not p.is_file():
                continue
            try:
                text = p.read_text(errors="replace")
            except OSError:
                return "仓库 attributes 无法读取，已拒绝执行 git（fail-closed）"
            for line in text.splitlines():
                s = line.strip()
                if s and not s.startswith("#") and _GIT_EXEC_ATTR_RE.search(s):
                    hits.append(f"{p.name}: {s[:60]}")
    if hits:
        return (
            "该仓库声明了可执行外部命令的自定义驱动（filter/diff），"
            "沙箱拒绝在此仓库执行 git：" + "；".join(hits[:5])
        )
    return ""



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
    - ``--opt=/path`` 取 ``=`` 后的值；``-f/path`` 取第一个分隔符起的子串
      （``-f`` 可直接附着文件名，如 grep -f/etc/passwd）
    - 普通参数本身含分隔符（``/`` 或 ``\\``）则整个参数是路径
    """
    if "://" in a:
        return None
    if a.startswith("-"):
        if "=" in a:
            return a.split("=", 1)[1] or None
        m = _SEP_RE.search(a)
        if m:
            return a[m.start():]
        return None
    return a if _SEP_RE.search(a) else None


def _check_path_args(args: list[str], root: Path | None = None) -> tuple[bool, str]:
    """含分隔符的参数必须落在工作区内；``..`` 一律拒绝。root 缺省时仅做词法检查。

    同时识别 ``/`` 与 ``\\`` 以及盘符/UNC 绝对形式——旧实现只按 ``/`` 分词，
    Windows 下 ``..\\..\\x``、``\\\\host\\share`` 等会被直接跳过检查（L1）。
    """
    for a in args:
        if not a:
            continue
        if ".." in _SEP_RE.split(a):
            return False, f"参数含 ..：{a!r}"
        cand = _path_candidate(a)
        if cand is None:
            continue
        if not root:
            if _ABS_WIN_RE.match(cand):
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


def _sandbox_home() -> Path:
    """子进程专属 HOME：位于工作区**之外**，fs 技能无法写入。

    旧实现把 HOME 设为工作区，而工作区可写——等于让 fs_write 能落
    ``$HOME/.gitconfig`` / ``$HOME/.curlrc`` 来劫持后续命令（H1）。
    """
    home = Path(tempfile.gettempdir()) / "agent-demo-sandbox-home"
    try:
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        logger.warning("failed to create sandbox home: %s", home)
    return home


def _minimal_env(root: Path) -> dict:
    """子进程最小环境：不继承 shell 环境（避免泄漏 LLM API key 等）。

    HOME/USERPROFILE/CURL_HOME 指向工作区外的专用沙箱目录，并叠加 git 环境层加固，
    封堵 ``$HOME/.gitconfig``、``$HOME/.curlrc`` 这类配置文件注入。
    """
    home = _sandbox_home()
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
        "HOME": str(home),
        "USERPROFILE": str(home),  # Windows 上 git/curl 也可能读它
        "CURL_HOME": str(home),    # curl 读 $CURL_HOME/.curlrc
        "TMPDIR": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    env.update(_GIT_ENV_HARDENING)
    for k in ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE"):
        if k in os.environ:
            env[k] = os.environ[k]
    return env


def _unzip_output_dir(root: Path, args: list[str]) -> Path | None:
    """取 ``unzip -d`` 指定的输出目录（须落在工作区内）；否则返回 None。"""
    for i, a in enumerate(args):
        if a == "-d" and i + 1 < len(args):
            try:
                p = (root / args[i + 1]).resolve()
            except Exception:
                return None
            return p if (p == root or root in p.parents) else None
    return None


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
            return f"{MSG_REFUSED}：{reason}"
        self.root.mkdir(parents=True, exist_ok=True)

        argv = list(args)
        if executable == "git":
            # 仓库层：驱动型配置无法用 -c 穷举，命中即 fail-closed 拒绝
            blocked = _git_exec_guard(self.root)
            if blocked:
                logger.warning("workspace git refused by exec guard: uid=%s %s", uid, blocked)
                return f"{MSG_REFUSED}：{blocked}"
            # 命令层：-c 覆盖 + diff 禁用外部 diff 驱动
            if args and args[0] == "diff":
                argv = [*_GIT_HARDENING, "diff", "--no-ext-diff", *args[1:]]
            else:
                argv = [*_GIT_HARDENING, *args]
        elif executable == "curl":
            # -q 必须是首个参数：禁止读取 $HOME/.curlrc 等配置文件
            argv = ["-q", *args]

        exe_path = shutil.which(executable)
        logger.info("workspace cmd: uid=%s cwd=%s cmd=%s %s", uid, self.root, executable, argv)
        try:
            proc = await asyncio.create_subprocess_exec(
                exe_path,
                *argv,
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

        # M1：unzip 解压后清除符号链接（docstring 宣称的安全属性，旧实现从未接线）
        if executable == "unzip":
            out_dir = _unzip_output_dir(self.root, args)
            if out_dir is not None:
                removed = await asyncio.to_thread(strip_symlinks, out_dir)
                if removed:
                    logger.warning("unzip: stripped %s symlink(s) under %s", removed, out_dir)

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
