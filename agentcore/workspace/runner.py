"""白名单命令执行器：不经 shell、锁定工作目录、超时 + 截断 + 审计日志。

安全边界（管理员专用入口，技能层已校验）——按「命令 + 逐参数」双层校验：
- 全局：参数不得含 shell 元字符/命令替换；凡含 ``/`` 的参数（含 ``--opt=/path``
  与 ``-f/path`` 形式）resolve 后必须仍在工作区内；``..`` 一律拒绝
- find 仅允许搜索类动作（拒绝 -exec/-execdir/-ok/-okdir/-delete/-fls/-fprint* 等）
- git 仅只读子命令 + 安全 flag（拒绝 -c/--ext-diff/--textconv/--output 等一切
  可写文件或执行外部程序的选项）
- curl 收敛为 GET-only 参数集（无 -o/-T/-d/-F/-H/-L 等）；URL host 为 IP 字面量
  （含 inet_aton 数字形态：2130706433 / 0177.0.0.1 / 0x7f000001 / 127.1）或
  本机/内网主机名（localhost / *.local / *.internal 等）时直接拒绝（与
  web_fetch 的出网防护对称）。**已披露残留**：其余域名形态的 host 不做 DNS
  解析，DNS rebinding 风险与 web_fetch 相同
- unzip 只放行 -d + 纯解压开关（-o/-q/-j/-n）；-d 目标 resolve 后必须落在工作区
  内**且不得是工作区根**（解压异常清理会 rmtree 输出目录，指向根等于删库）；
  解压前扫描压缩包条目名，拒绝 .. 穿越 / 绝对路径 / .git* 配置条目；执行后清除
  解压出的符号链接；只要检出过 symlink，整个解压输出目录即废弃（L18）
- tar 只放行解压（-x/--extract）与压缩格式/输出目录类开关；连写短选项逐字过
  白名单（``-xf a.tar`` 的**粘连取值**形态如 ``-xfa.tar`` 因此被拒，取值用
  空格分开写）；必须且各只能有一个 ``-C``/``--directory`` 输出目录与
  ``-f``/``--file`` 压缩包（多 -f 会让条目预扫描扫错包）；-C 必须指向工作区内
  **子目录**（与 unzip 的 -d 同构：异常清理要 rmtree 输出目录，指向根等于删库），
  且 spawn 前先建好该目录（GNU tar 不像 unzip 会自动创建）；解压前扫条目，拒绝
  .. 穿越 / 绝对路径 / .git* 配置条目 / **符号链接与硬链接条目**（比 unzip 多拒
  链接条目——解压前即拦掉，不留 strip 窗口）；解压后仍按 unzip 同款 strip +
  检出即废弃输出目录
- 子进程使用最小化环境变量（不继承 LLM API key 等），输出流式截断
- 审计日志带操作者 uid
- 只读运维命令（ps/top/free/df/du/uptime/uname/nproc/whoami/id/ss/netstat/lscpu）
  免参数白名单：这些程序没有任何写文件或执行代码的选项，输入面已由全局四道闸
  （shell 元字符 / ``..`` / 路径遏制 / 超时截断）全覆盖；``top`` 额外要求 ``-b``
  批量模式（拒绝交互态，防 ``k``/``r``/``W`` 按键动作）
- ``systemctl``/``journalctl``/``dmesg``/``hostname`` 走「子命令/flag 白名单」：
  只读子命令 + 过滤排序类 flag 放行；动作型子命令（restart/mask/edit…）、
  写内核环形缓冲（``dmesg -C/-c``）、读任意文件（``journalctl --file/--root/--directory``）、
  删日志（``--rotate/--vacuum-*``）、跨机（``--host``/``-M``）一律拒绝。
  **刻意不放行 kill/任意 systemctl 动作**：重启本服务走 reboot 流程（三次校验 + 退路），
  把「杀掉进程」的执行权留给 LLM 自由拼参数的结果不可回退

**配置注入面的封堵（H1）**——命令的执行行为受配置文件影响，仅校验「命令 + 参数」
并不足够，攻击者只要能在工作区落一个配置文件即可绕过全部参数校验：

1. 全局/系统层：``GIT_CONFIG_GLOBAL`` / ``GIT_CONFIG_SYSTEM`` 指向 ``/dev/null``，
   ``GIT_CONFIG_NOSYSTEM=1``；``HOME`` 不再指向可写工作区而是专用沙箱目录
   （``$HOME/.gitconfig``、``$HOME/.curlrc`` 曾可被 fs_write 写入）
2. 命令层：所有 git 调用前缀注入 ``_GIT_HARDENING``（``-c`` 覆盖优先级高于
   repo-local 配置），并对 ``git diff`` 追加 ``--no-ext-diff``
3. 仓库层（``-c`` 单例覆盖无法穷举）：``_repo_exec_guard`` 在 spawn 前检查
   工作区仓库是否声明了**可执行外部命令的驱动**（``filter.*.clean|smudge|process``
   等键按跨点匹配，覆盖 ``[diff "a.b"]`` 带点小节名；``.gitattributes`` 里的
   ``filter=``/``diff=`` 属性**无条件扫描**），命中即拒绝执行并说明原因（fail-closed）
4. 写入面根治：fs 技能的创建/修改/删除操作拒绝触及 ``.git``（含 worktree 指针文件）、
   ``.gitattributes``、``.gitmodules``（读不受限）——配置注入进不来，守卫只是兜底

已知残留风险：git 的配置驱动执行面较宽，第 3 层为「拒绝已知形态」而非完备证明；
沙箱 curl 对域名形态 host 不做 DNS 解析（rebinding 残留与 web_fetch 相同，已披露）。
根治方案仍是容器/独立低权用户，待运维落地；在此之前以本文件为安全边界。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from agentcore.safety import ip_literal_is_safe

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
    "--oneline",
    "--stat",
    "--short",
    "--graph",
    "--decorate",
    "--abbrev-commit",
    "--name-only",
    "--name-status",
    "--no-color",
    "--all",
    "--follow",
    "--tags",
    "--branches",
    "--remotes",
    "--patch",
    "--no-patch",
    "--abbrev",
    "--no-abbrev",
    "--pretty",
    "--format",
    "--date",
    "--max-count",
    "--skip",
    "--since",
    "--until",
    "--author",
    "--grep",
    "--fixed-strings",
    "--extended-regexp",
    "--invert-match",
    "--word-regexp",
    "--column",
    "--summary",
    "-n",
    "-a",
    "-v",
    "--verbose",
}
_GIT_SAFE_FLAG_KEYS = {
    "--pretty",
    "--format",
    "--date",
    "--max-count",
    "--skip",
    "--since",
    "--until",
    "--author",
    "--grep",
    "--abbrev",
}
# git log -5 / -n5 之类的数字短选项；systemctl/journalctl/dmesg/hostname/top 的
# flag 白名单语境里同样按「取值」处理（如 journalctl -b -1 指上一次启动）
_NUM_SHORT_RE = re.compile(r"^-\d+$")

# find 仅放行搜索类动作；任何能执行命令/删除/落盘的动作都不可用
_FIND_SAFE_OPTIONS = {
    "--version",
    "-H",
    "-L",
    "-P",
    "-name",
    "-iname",
    "-lname",
    "-type",
    "-maxdepth",
    "-mindepth",
    "-mtime",
    "-mmin",
    "-size",
    "-newer",
    "-empty",
    "-true",
    "-false",
    "-print",
    "-print0",
    "-depth",
    "-follow",
    "-and",
    "-or",
    "-not",
    "!",
    "-a",
    "-o",
    "-writable",
    "-readable",
    "-nouser",
    "-nogroup",
}

# curl 收敛为 GET-only：只读展示 + 数值型超时/大小限制
_CURL_SAFE_FLAGS = {
    "-s",
    "-S",
    "-i",
    "-I",
    "-v",
    "--head",
    "--silent",
    "--show-error",
    "--compressed",
    "--http1.1",
    "--http2",
}
_CURL_SAFE_FLAG_KEYS = {"--max-time", "--connect-timeout", "--max-filesize"}

# H1（REVIEW-a604023..679c9b3）：zip 只放行这几个纯打包开关。
# Info-ZIP 的 ``-T`` 会执行测试命令（``-TT cmd`` / ``--unzip-command=cmd`` 可替换
# unzip 从而执行任意命令），``-m``/``-d`` 会删文件，``-@``/``-P``/``-e`` 等不可控；
# 含 ``=`` 的**开关**一律拒绝（直接封掉 ``--unzip-command=...`` 形态）；
# 非开关操作数（压缩包/文件名）允许含 ``=``（``report_v=2.zip`` 是合法文件名，
# L5：原先无差别拒 ``=`` 会误伤它们——而带 = 的开关本来就活不过白名单）。
_ZIP_SAFE_FLAGS = {"-r", "-q", "-9", "-j"}

# unzip 只放行纯解压开关：-o 覆盖 / -q 安静 / -j 扁平化路径 / -n 不覆盖。
# -Z(zipinfo 模式，可带格式串)/-p(落 stdout)/-x(排除表)/-P(密码)/-M(pager) 等
# 一律不在表内——与 zip 同理，白名单穷举，不猜哪个"看起来无害"。
_UNZIP_SAFE_FLAGS = {"-o", "-q", "-j", "-n"}
# 写入面与 fs 技能同一口径：解压产物不得触及 git 配置（runner docstring 第 4 条）
_UNZIP_ENTRY_FORBIDDEN = {".git", ".gitattributes", ".gitmodules"}

# tar：只放行解压（-x/--extract）与压缩格式/输出目录/展示类开关。
# 刻意不在表内：-P/--absolute-names（绝对路径写盘）、--to-command（把条目喂给
# 任意命令执行）、--checkpoint/--checkpoint-action（同上有执行面）、-O/--to-stdout
# （绕过文件遏制）、-c/-t/-u/--delete（创建/列表/更新/删除模式）等；连写短选项
# （-xzf）逐字拆开后仍在此表内校验，粘连取值形态（-xfa.tar）因含表外字符被整体拒绝。
_TAR_SAFE_SHORT = set("xzjJvfC")
_TAR_SAFE_LONG = {
    "--extract",
    "--verbose",
    "--gzip",
    "--bzip2",
    "--xz",
    "--no-same-owner",
    "--no-same-permissions",
    "--no-overwrite-dir",
    "--skip-old-files",
    "--keep-old-files",
    "--wildcards",
}
_TAR_SAFE_FLAG_KEYS = {"--file", "--directory", "--strip-components"}
# 归一化后的短选项也要逐个过表：与长选项合并成一张完整白名单
_TAR_ALL_SAFE_FLAGS = _TAR_SAFE_LONG | {f"-{c}" for c in _TAR_SAFE_SHORT}
# 解压前扫条目上限：病态大包不无限遍历（超限告警放行，与 _MAX_ATTR_SCAN 同思路）
_MAX_TAR_MEMBERS = 200_000

# 只读运维命令：无参数白名单（全局四道闸已覆盖其全部输入）。
# 共同点：没有任何写文件/执行代码/删数据的选项——ps 只是列进程，df/du 只读
# /proc 与目录 inode，netstat 只读 socket 表。top 单独走 -b 批量白名单
# （交互态有 k 杀进程 / r renice / W 写 ~/.toprc 三个写向）。
# **ss 不在此表**：iproute2 ss 有 -K/--kill（按过滤器批量强断 socket，破坏性
# 不可回退，REVIEW-3ce6e0a..de09478 H1 实锤），单独走展示 flag 白名单。
_READONLY_OPS = {
    "ps",
    "free",
    "df",
    "du",
    "uptime",
    "uname",
    "nproc",
    "whoami",
    "id",
    "netstat",
    "lscpu",
}

# ss：展示/过滤类 flag 白名单。-K/--kill（断 socket）、-D（dump 到文件）、
# -A/--datasets（任意表枚举）都不在表内即拒；位置参数是只读过滤器表达式
# （state/dst/sport 等），随全局四道闸走。
_SS_SAFE_FLAGS = {
    "-t",
    "-u",
    "-w",
    "-x",
    "-d",
    "-i",
    "-s",
    "-n",
    "-r",
    "-p",
    "-a",
    "-l",
    "-o",
    "-e",
    "-m",
    "-4",
    "-6",
    "-0",
}
_SS_SAFE_FLAG_KEYS = {"-f", "--family"}
_SS_GLUED_SINGLE = set("tuwxdinsrpaloeem460")

# top：必须 -b（非 tty 本来也会报错，显式要求是拒绝交互态），其余为展示/数量类 flag
_TOP_BATCH_FLAG_RE = re.compile(r"^-b")
_TOP_SAFE_FLAGS = {"-b", "-H", "-S", "-c", "-i", "-E", "-e", "-1"}
_TOP_SAFE_FLAG_KEYS = {"-n", "-d", "-p", "-u", "-U", "-o", "-O", "-w", "-s"}

# systemctl：只读子命令 + flag 白名单。restart/stop/mask/edit/daemon-reload 等
# 动作型子命令与 --host/-H/-M/--machine（借 ssh 管远程主机的 systemd）、
# --root（换根目录读文件）都不在表内；--output 等取值是格式名不是路径。
_SYSTEMCTL_READONLY_SUBCMDS = {
    "is-active",
    "is-enabled",
    "is-failed",
    "status",
    "show",
    "list-units",
    "list-unit-files",
    "list-jobs",
}
_SYSTEMCTL_SAFE_GLOBAL_FLAGS = {
    "--no-pager",
    "--plain",
    "--no-legend",
    "--system",
    "-q",
    "--quiet",
}
_SYSTEMCTL_SAFE_FLAGS = {
    "-l",
    "--full",
    "--value",
    "--all",
    "-a",
    "--failed",
    "--recursive",
    "--reverse",
    "--with-dependencies",
}
_SYSTEMCTL_SAFE_FLAG_KEYS = {
    "-n",
    "--lines",
    "-p",
    "--property",
    "--state",
    "--type",
    "-o",
    "--output",
    "--job-mode",
}

# journalctl：过滤/输出类 flag 白名单。--file/--root/--directory 读任意文件、
# --rotate/--vacuum-time/--vacuum-size/--vacuum-files 轮转删除日志、
# --relinquish-var 交出 /var/log/journal 所有权——一律不在表内即拒绝。
_JOURNALCTL_SAFE_FLAGS = {
    "--no-pager",
    "--list-boots",
    "-k",
    "--dmesg",
    "-b",
    "--this-boot",
    "-r",
    "--reverse",
    "-x",
    "--catalog",
    "-q",
    "--quiet",
    "--no-hostname",
    "-e",
    "--pager-end",
}
_JOURNALCTL_SAFE_FLAG_KEYS = {
    "-u",
    "--unit",
    "-n",
    "--lines",
    "--since",
    "--until",
    "-p",
    "--priority",
    "-o",
    "--output",
    "--identifier",
    "-b",
    "--boot",
    "--facility",
}

# dmesg：读内核环形缓冲；-w 跟随、-C/--clear 清空、-c/--read-clear 读后清空、
# -D/-E 开关控制台、-n 设控制台级别都是写向，不在表内即拒绝。
_DMESG_SAFE_FLAGS = {
    "-r",
    "--raw",
    "-T",
    "--ctime",
    "-t",
    "--notime",
    "-x",
    "--decode",
    "-H",
    "--human",
    "-k",
    "--kernel",
    "-u",
    "--userspace",
}
_DMESG_SAFE_FLAG_KEYS = {"-l", "--level", "-f", "--facility", "-s", "--buffer-size"}

# hostname：只允许查询 flag；位置参数会**改主机名**，-F/--file 从文件读新主机名，
# 都不放行（-F 不在表内；位置参数在此显式拒绝）。
_HOSTNAME_SAFE_FLAGS = {
    "-I",
    "--all-ip-addresses",
    "-i",
    "--ip-address",
    "-f",
    "--fqdn",
    "-A",
    "--all-fqdns",
    "-d",
    "--domain",
    "-s",
    "--short",
    "-y",
    "--yp",
}
# strip_symlinks 的扫描上限：异常巨大的解压产物不无限遍历（超限告警并停止，
# symlink 清理退化为"尽力而为"，与原行为一致只是不再无界）
_MAX_SYMLINK_SCAN = 50_000

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
    "-c",
    "core.fsmonitor=false",
    "-c",
    f"core.hooksPath={os.devnull}",
    "-c",
    "core.pager=cat",
    "-c",
    "core.editor=false",
    "-c",
    "sequence.editor=false",
    "-c",
    "core.sshCommand=false",
    "-c",
    "core.gitProxy=",
    "-c",
    "core.alternatesRefsCommand=",
    "-c",
    "credential.helper=",
    "-c",
    "diff.external=",
    "-c",
    "protocol.ext.allow=never",
    "-c",
    "protocol.file.allow=never",
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

# 仓库层守卫：这些配置键/属性会让 git 执行外部命令，且驱动名任意、无法用 -c 穷举。
# 驱动名/小节名按「跨点」匹配（``.+`` 而非 ``[^.]+``）：``[diff "a.b"]`` 这种带点
# 小节名会解析出 ``diff.a.b.textconv``，旧正则漏判即绕过守卫（H1）——宁可误报，
# fail-closed。
_GIT_EXEC_CONFIG_KEY_RE = re.compile(
    r"^(?:filter\..+\.(?:clean|smudge|process)"
    r"|diff\..+\.(?:command|textconv)"
    r"|core\.(?:fsmonitor|pager|editor|sshcommand|hookspath|gitproxy|alternatesrefscommand)"
    r"|sequence\.editor|credential\.helper|include\.path|includeif\..+\.path)$"
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

    attributes **无条件扫描**（H1 第二层）：曾实现过「配置里没有驱动定义就跳过扫描」
    的捷径，但配置解析是近似的（不展开 include、不覆盖全部语法），「本地没看到驱动
    定义」不等于「运行时不存在驱动」——只要 attributes 出现 ``filter=``/``diff=``，
    无论配置命中与否，一律拒绝（递归扫描有上限，避免超大工作区拖慢）。
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
    """组合命令/命令替换类拒绝。换行/回车虽无 shell 解释面（argv 直送 execve），
    但可向审计日志伪造多行记录，一并拒绝。"""
    for a in args:
        if not a:
            continue
        if any(c in _DENIED_REDIRECT for c in a):
            return f"参数含 shell 元字符（不允许组合命令）：{a!r}"
        if "\n" in a or "\r" in a:
            return "参数含换行符"
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
            return a[m.start() :]
        return None
    return a if _SEP_RE.search(a) else None


def _check_path_args(args: list[str], root: Path | None = None) -> tuple[bool, str]:
    """凡参数被用作文件路径（含分隔符参数与**裸名操作数**）都必须落在工作区内；
    ``..`` 一律拒绝。root 缺省时仅做词法检查。

    同时识别 ``/`` 与 ``\\`` 以及盘符/UNC 绝对形式——旧实现只按 ``/`` 分词，
    Windows 下 ``..\\..\\x``、``\\\\host\\share`` 等会被直接跳过检查（L1）。

    裸名操作数（``cat lnk`` / ``zip -r out.zip lnk``）也要遏制：工作区内的
    符号链接 resolve 后会落在 root 外，等于读写逃逸口（评审 P2）。
    """
    for a in args:
        if not a:
            continue
        if ".." in _SEP_RE.split(a):
            return False, f"参数含 ..：{a!r}"
        cand = _path_candidate(a)
        if cand is None:
            # 选项参数与 URL 不做文件路径遏制；其余裸名操作数按相对路径检查
            if root and not a.startswith("-") and "://" not in a:
                try:
                    p = (root / a).resolve()
                except Exception:
                    return False, f"参数无法解析为安全路径：{a!r}"
                if p != root and root not in p.parents:
                    return False, f"参数路径超出工作区：{a!r}"
            continue
        # 绝对路径（/ 开头、盘符、UNC）无论有无 root 都要拒绝：
        # 之前只在「无 root」词法分支里查，导致带 root 时 `C:\...`/`\\host\...`
        # 在 POSIX 上被当成 root 内的普通文件名放行（L1）
        if _ABS_WIN_RE.match(cand):
            return False, f"参数含绝对路径：{a!r}"
        if not root:
            continue
        try:
            p = (root / cand).resolve()
        except Exception:
            return False, f"参数无法解析为安全路径：{a!r}"
        if p != root and root not in p.parents:
            return False, f"参数路径超出工作区：{a!r}"
    return True, ""


def _url_host(u: str) -> str | None:
    """取 URL 的 host（小写、IPv6 不带方括号）；解析失败返回 None。"""
    try:
        return urlsplit(u).hostname
    except ValueError:
        return None


def _whitelist_flags(
    args: list[str],
    safe_flags: set[str],
    safe_flag_keys: set[str],
    *,
    deny_reason: str,
) -> str | None:
    """flag 白名单：只放行无取值的 safe_flags 与取值型 safe_flag_keys。

    取值型 flag 的三种写法都放行：``--key=value``、``--key value``（``--key`` 单独
    出现，值是下一个参数，随全局校验走）、``-n5``（短选项粘合）。纯数字负值
    （``-1``，如 ``journalctl -b -1`` 的上一次启动）按**取值**而非开关处理。
    ``--`` 之后一律拒（不在表内）。返回 None 表示全部通过，否则是拒绝原因
    （调用方各自给更具体的原因文案）。
    """
    for a in args:
        if not a.startswith("-"):
            continue  # 非 flag 的操作数（unit 名 / boot id 等）已随全局校验走
        if a in safe_flags or a in safe_flag_keys:
            continue
        if _NUM_SHORT_RE.match(a):
            continue  # -1 / -2 这类负值是取值（boot id 等），不是开关
        if "=" in a and a.split("=", 1)[0] in safe_flag_keys:
            continue
        if len(a) > 2 and a[1] != "-" and f"-{a[1]}" in safe_flag_keys:
            continue
        return f"{deny_reason}：{a!r}"
    return None


def _top_ok(args: list[str]) -> tuple[bool, str]:
    """top：必须 -b 批量模式（交互态有 k 杀进程 / r renice / W 写 ~/.toprc）。"""
    if not any(_TOP_BATCH_FLAG_RE.match(a) for a in args):
        return False, "top 仅允许 -b 批量模式（拒绝交互态；杀进程请走管理员命令）"
    # -bn1 / -bH 这类粘合形态拆成单项再逐项过白名单（top 已知的写向全是交互态
    # 命令而非 CLI flag，但组合形态仍逐项校验，不留「以 -b 开头即放行」的口子）
    rest: list[str] = []
    for a in args:
        if _TOP_BATCH_FLAG_RE.match(a):
            tail = a[2:]
            if tail.isdigit():
                continue
            rest.extend(f"-{ch}" for ch in tail)
        else:
            rest.append(a)
    reason = _whitelist_flags(
        rest,
        _TOP_SAFE_FLAGS,
        _TOP_SAFE_FLAG_KEYS,
        deny_reason="top 仅允许批量/展示/数量类选项",
    )
    if reason:
        return False, reason
    return True, ""


def _ss_ok(args: list[str]) -> tuple[bool, str]:
    """ss：展示/过滤 flag 白名单（-K/--kill 断 socket、-D dump 文件全拒）。

    ``-tulnp`` 这类连写短 flag 拆成单项逐个过表（与 top 的 -bn1 同思路）：
    粘合字符集里没有 K/D/A，`-tK` / `-Kx` 这类组合进不来。
    """
    rest: list[str] = []
    for a in args:
        if a.startswith("-") and not a.startswith("--") and len(a) > 2:
            chars = a[1:]
            if not all(c in _SS_GLUED_SINGLE for c in chars):
                return False, (
                    "ss 仅允许展示/过滤类选项"
                    "（-K/--kill 强断 socket、-D dump 到文件，一律拒）：" + repr(a)
                )
            rest.extend(f"-{c}" for c in chars)
        else:
            rest.append(a)
    reason = _whitelist_flags(
        rest,
        _SS_SAFE_FLAGS,
        _SS_SAFE_FLAG_KEYS,
        deny_reason=(
            "ss 仅允许展示/过滤类选项（-K/--kill 强断 socket、-D dump 到文件，一律拒）"
        ),
    )
    if reason:
        return False, reason
    return True, ""


def _systemctl_ok(args: list[str]) -> tuple[bool, str]:
    """systemctl：只读子命令 + flag 白名单（含拒跨机 --host/-M）。"""
    if not args:
        return False, "systemctl 需要只读子命令"
    for a in args:
        if a in {"-H", "--host", "-M", "--machine"}:
            return False, f"systemctl 不允许跨机形式（借 ssh 管远程主机）：{a!r}"
    reason = _whitelist_flags(
        args,
        _SYSTEMCTL_SAFE_GLOBAL_FLAGS | _SYSTEMCTL_SAFE_FLAGS,
        _SYSTEMCTL_SAFE_FLAG_KEYS,
        deny_reason="systemctl 仅允许只读查询选项",
    )
    if reason:
        return False, reason
    # 第一个非 flag 参数即子命令（unit 名等操作数随全局校验走）
    subs = [a for a in args if not a.startswith("-")]
    if not subs or subs[0] not in _SYSTEMCTL_READONLY_SUBCMDS:
        return False, (
            f"systemctl 仅允许只读子命令：{sorted(_SYSTEMCTL_READONLY_SUBCMDS)}"
        )
    return True, ""


def _journalctl_ok(args: list[str]) -> tuple[bool, str]:
    """journalctl：过滤/输出类 flag 白名单（读任意文件/删日志的选项全拒）。"""
    reason = _whitelist_flags(
        args,
        _JOURNALCTL_SAFE_FLAGS,
        _JOURNALCTL_SAFE_FLAG_KEYS,
        deny_reason=(
            "journalctl 仅允许过滤/输出类选项"
            "（--file/--root/--directory 读任意文件、"
            "--rotate/--vacuum-* 删日志，一律拒）"
        ),
    )
    if reason:
        return False, reason
    return True, ""


def _dmesg_ok(args: list[str]) -> tuple[bool, str]:
    """dmesg：读取类 flag 白名单（-w 跟随 / -C -c 清空 / -D -E -n 写向全拒）。"""
    reason = _whitelist_flags(
        args,
        _DMESG_SAFE_FLAGS,
        _DMESG_SAFE_FLAG_KEYS,
        deny_reason=(
            "dmesg 仅允许读取/格式化选项（-w 跟随、-C/--clear、"
            "-c/--read-clear、-D/-E、-n 控制台级别都是写向，一律拒）"
        ),
    )
    if reason:
        return False, reason
    return True, ""


def _hostname_ok(args: list[str]) -> tuple[bool, str]:
    """hostname：只允许查询 flag（位置参数会改主机名，-F/--file 从文件读新名）。"""
    for a in args:
        if not a.startswith("-"):
            return False, f"hostname 仅允许查询 flag（位置参数会修改主机名）：{a!r}"
    reason = _whitelist_flags(
        args,
        _HOSTNAME_SAFE_FLAGS,
        set(),
        deny_reason="hostname 仅允许查询 flag",
    )
    if reason:
        return False, reason
    return True, ""


def permitted(
    executable: str,
    args: list[str],
    root: str | Path | None = None,
    *,
    readonly_only: bool = False,
) -> tuple[bool, str]:
    """白名单判定：返回 (是否允许, 拒绝原因)。root 提供时启用 resolve 级路径遏制。

    ``readonly_only``（权限级别 low 档）：在白名单内再收掉四个「有写向/出网」
    的命令——zip/unzip/tar 在工作区落盘，curl 出网。其余白名单成员本来就是
    纯只读（git 仅只读子命令、systemctl 仅只读子命令、find 仅搜索动作）。
    """
    exe = (executable or "").strip()
    if not exe:
        return False, "未指定可执行命令"
    if "/" in exe:
        return False, "命令名不允许带路径"
    if readonly_only and exe in {"zip", "unzip", "tar", "curl"}:
        return False, (
            f"当前权限级别为 low：仅允许只读命令（{exe} 需要 medium 及以上级别，"
            "可用 AGENT_PERMISSION_LEVEL 调整，改后重启生效）"
        )
    if not shutil.which(exe):
        return False, f"命令不在系统中：{exe}"
    # curl 的 URL 查询串合法地包含 & / ;（argv 直送 execve，本无 shell 解释），
    # 元字符检查对 curl 只看选项参数；其余命令维持全参数检查
    meta_args = [a for a in args if a.startswith("-")] if exe == "curl" else args
    reason = _denied_for_shell(meta_args)
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
            if a in _GIT_SAFE_FLAGS or _NUM_SHORT_RE.match(a):
                continue
            if "=" in a and a.split("=", 1)[0] in _GIT_SAFE_FLAG_KEYS:
                continue
            return False, f"git 不允许该选项（仅白名单内安全选项）：{a!r}"
        return True, ""
    if exe in {"grep", "cat", "ls", "head", "tail", "wc", "pwd"}:
        # 纯读命令：无执行/落盘选项，全局路径遏制已覆盖
        return True, ""
    if exe in _READONLY_OPS:
        # 只读运维命令：无写文件/执行代码选项（top 的交互写向由 -b 要求排除）
        return True, ""
    if exe == "top":
        return _top_ok(args)
    if exe == "ss":
        return _ss_ok(args)
    if exe == "hostname":
        return _hostname_ok(args)
    if exe == "systemctl":
        return _systemctl_ok(args)
    if exe == "journalctl":
        return _journalctl_ok(args)
    if exe == "dmesg":
        return _dmesg_ok(args)
    if exe == "find":
        for a in args:
            if a.startswith("-") and a not in _FIND_SAFE_OPTIONS:
                return False, (
                    f"find 不允许该选项（禁止 -exec/-delete/-fprint 等动作）：{a!r}"
                )
        return True, ""
    if exe == "zip":
        # H1：参数零校验时，``-T``/``-TT``/``--unzip-command=`` 会执行任意命令
        # （Info-ZIP 内部走 system()），``-m``/``-d`` 会删文件 → 收敛为白名单开关
        for a in args:
            if a.startswith("-") and ("=" in a or a not in _ZIP_SAFE_FLAGS):
                return False, (
                    f"zip 仅允许 {sorted(_ZIP_SAFE_FLAGS)} 这些打包开关，"
                    f"且开关不得含 '='（-T/-TT/--unzip-command= 等会执行命令或删文件）：{a!r}"
                )
        return True, ""
    if exe == "unzip":
        # 开关白名单（与 zip 同理穷举）；-d 是唯一带取值的开关
        for a in args:
            if a.startswith("-") and a != "-d" and a not in _UNZIP_SAFE_FLAGS:
                return False, (
                    f"unzip 仅允许 {sorted(_UNZIP_SAFE_FLAGS | {'-d'})} 这些开关"
                    f"（-Z/-p/-x/-P/-M 等不在白名单）：{a!r}"
                )
        if args.count("-d") != 1:
            return False, "unzip 必须且只能用一个 -d 指定输出目录"
        if root_path is not None:
            out_dir = _unzip_output_dir(root_path, args)
            if out_dir is None:
                return False, "unzip 输出目录无法安全解析（须落在工作区内）"
            if out_dir == root_path:
                # 解压检出符号链接时整个输出目录会被 rmtree 丢弃——指向根等于删库
                return False, ("unzip 不允许解压到工作区根目录，请用 -d 指定一个子目录")
        return True, ""
    if exe == "tar":
        # 解压-only：开关白名单 + -C 输出目录遏制 + 条目预扫描（见 _tar_entry_problem）
        return _tar_ok(args, root_path)
    if exe == "curl":
        # H2：不能只挑含 '://' 的参数校验——curl 会把裸 ``host:port/path`` 当
        # ``http://`` 请求，于是"https 诱饵 + 裸内网地址"即可 SSRF（明文 http）。
        # 现在把所有**非选项参数**一律视为 URL 候选，逐个要求 https:// + 公网 IP。
        # 合法选项的取值都是内联 ``--key=value`` 形态，不会产生额外裸参数。
        candidates = [a for a in args if not a.startswith("-")]
        if not candidates:
            return False, "curl 需要 https URL"
        for u in candidates:
            if not u.startswith("https://"):
                return False, (
                    f"curl 仅允许 https:// 地址（裸主机名/其他协议一律拒绝）：{u!r}"
                )
        for u in candidates:
            # L20：host 为 IP 字面量时直接判定安全性（与 web_fetch 的出网防护对称），
            # 内网/loopback/链路本地/保留/组播地址一律拒绝；inet_aton 数字形态
            # （2130706433 / 0x7f000001 / 127.1）与 localhost/*.internal 等内部
            # 主机名同样在此判 False。其余域名形态不做 DNS 解析（保持离线可用/
            # 可测），其 DNS rebinding 残留与 web_fetch 相同，已另行披露。
            if ip_literal_is_safe(_url_host(u)) is False:
                return False, (
                    "curl 拒绝访问内网/链路本地/保留地址"
                    f"（含数字型 IP 与内部主机名，SSRF 防护）：{u!r}"
                )
        for a in args:
            if not a.startswith("-"):
                continue  # 非选项参数已作为 URL 候选逐个校验过
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

    L16：``/tmp`` 下的固定路径可能被同主机其他用户抢先创建（任意属主/权限），
    复用前 stat 校验属主与 0700 权限；不符则改用随机新目录，不共享不可信目录。
    """
    home = Path(tempfile.gettempdir()) / "agent-demo-sandbox-home"
    try:
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = home.stat()
        getuid = getattr(os, "getuid", None)  # 无 getuid 的平台跳过属主校验
        owner_ok = getuid is None or st.st_uid == getuid()
        if not (owner_ok and st.st_mode & 0o777 == 0o700):
            raise OSError(f"owner={st.st_uid} mode={oct(st.st_mode & 0o777)}")
        return home
    except OSError as e:
        logger.warning("sandbox home %s 预检失败（%s），改用随机临时目录", home, e)
    # mkdtemp 失败时宁可拒绝执行，也不回落到可能被抢占的固定目录
    return Path(tempfile.mkdtemp(prefix="agent-demo-sandbox-home-"))


def _minimal_env(root: Path) -> dict:
    """子进程最小环境：不继承 shell 环境（避免泄漏 LLM API key 等）。

    HOME/USERPROFILE/CURL_HOME 指向工作区外的专用沙箱目录，并叠加 git 环境层加固，
    封堵 ``$HOME/.gitconfig``、``$HOME/.curlrc`` 这类配置文件注入。
    """
    home = _sandbox_home()
    env = {
        "PATH": os.environ.get(
            "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        ),
        "HOME": str(home),
        "USERPROFILE": str(home),  # Windows 上 git/curl 也可能读它
        "CURL_HOME": str(home),  # curl 读 $CURL_HOME/.curlrc
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


def _unzip_archive_operand(args: list[str]) -> str | None:
    """取 unzip 的压缩包操作数（第一个非开关、且不是 -d 取值的参数）。"""
    skip_next = False
    for a in args:
        if skip_next:
            skip_next = False
            continue
        if a == "-d":
            skip_next = True
            continue
        if a.startswith("-"):
            continue
        return a
    return None


def _zip_scan(archive: Path) -> tuple[str | None, list[str]]:
    """解压前扫描 zip 条目名；返回 ``(拒绝原因, 条目名列表)``。

    拦三类：``..`` 穿越条目、绝对路径条目（含盘符/UNC/反斜杠形态）、以及
    ``.git``/``.gitattributes``/``.gitmodules`` 配置条目——runner docstring
    第 4 条的「写入面拒绝触及 git 配置」此前对 unzip 分支不成立（评审 P2）。
    条目名列表供 M1 的解压后执行位精确清理用（见 strip_entry_exec_bits）。
    """
    try:
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
    except Exception:
        return None, []  # 不是合法 zip：让 unzip 本身报错，不在这里越权判死
    for name in names:
        parts = re.split(r"[\\/]", name)
        if name.startswith(("/", "\\")) or (len(name) > 1 and name[1] == ":"):
            return f"压缩包含绝对路径条目：{name[:60]!r}", names
        if ".." in parts:
            return f"压缩包含 .. 穿越条目：{name[:60]!r}", names
        if any(p in _UNZIP_ENTRY_FORBIDDEN for p in parts):
            return f"压缩包含 git 配置条目（写入面禁止触及）：{name[:60]!r}", names
    return None, names


def _zip_entry_problem(archive: Path) -> str | None:
    """薄包装：只取拒绝原因（存量调用方/测试用）。"""
    return _zip_scan(archive)[0]


def _tar_normalize(args: list[str]) -> tuple[list[str] | None, str]:
    """连写短选项逐字拆开再逐个过白名单（与 _top_ok/_ss_ok 同思路）。

    ``-xf a.tar`` → ``['-x','-f','a.tar']``；粘连取值形态（``-xfa.tar``）会拆出
    表外字符（a/r…）而被整体拒绝——取值请用空格分开写，这是**有意**的收敛。
    """
    rest: list[str] = []
    for a in args:
        if a.startswith("-") and not a.startswith("--") and len(a) > 1:
            chars = a[1:]
            if not all(c in _TAR_SAFE_SHORT for c in chars):
                return None, (
                    "tar 仅允许解压/压缩格式/输出目录/文件名这些短选项"
                    f"（粘连取值形态请改为空格分隔，如 -xf a.tar）：{a!r}"
                )
            rest.extend(f"-{c}" for c in chars)
        else:
            rest.append(a)
    return rest, ""


def _resolve_in_root(root: Path, value: str) -> Path | None:
    """把 -C/--directory 的取值 resolve 到工作区内；根目录/越界返回 None。"""
    try:
        p = (root / value).resolve()
    except Exception:
        return None
    return p if (p != root and root in p.parents) else None


def _tar_output_dir(root: Path, rest: list[str]) -> Path | None:
    """取 tar 输出目录（``-C dir`` / ``--directory dir`` / ``--directory=dir``）。

    必须落在工作区内且**不是工作区根**（与 unzip 的 -d 同口径：解压异常清理会
    rmtree 输出目录，指向根等于删库）。归一化后 ``-C`` 必为独立 token。
    """
    for i, a in enumerate(rest):
        if a in {"-C", "--directory"} and i + 1 < len(rest):
            return _resolve_in_root(root, rest[i + 1])
        if a.startswith("--directory="):
            return _resolve_in_root(root, a.split("=", 1)[1])
    return None


def _tar_archive_operand(rest: list[str]) -> str | None:
    """取 tar 的压缩包：``-f``/``--file`` 的取值（含 ``--file=`` 内联形态）。

    与 unzip 不同：tar 的压缩包操作数**就是 -f 的取值**，不存在「第一个非开关
    参数」（位置操作数是成员名）。调用方（_tar_ok）保证 -f/--file 恰好一个。
    """
    for i, a in enumerate(rest):
        if a in {"-f", "--file"} and i + 1 < len(rest):
            return rest[i + 1]
        if a.startswith("--file="):
            return a.split("=", 1)[1]
    return None


def _tar_ok(args: list[str], root: Path | None) -> tuple[bool, str]:
    """tar：解压-only 校验。root 缺省时仅做词法检查（与 unzip 分支对称）。"""
    rest, reason = _tar_normalize(args)
    if rest is None:
        return False, reason
    reason = _whitelist_flags(
        rest,
        _TAR_ALL_SAFE_FLAGS,
        _TAR_SAFE_FLAG_KEYS,
        deny_reason=(
            "tar 仅允许解压/压缩格式/输出目录类选项"
            "（-P 绝对路径、--to-command/--checkpoint-action 执行命令、"
            "-O/--to-stdout 绕过文件遏制，一律拒）"
        ),
    )
    if reason:
        return False, reason
    if not any(a in {"-x", "--extract"} for a in rest):
        return False, "tar 仅允许解压（-x/--extract），其他子命令模式不在白名单"
    out_flags = sum(
        1 for a in rest if a in {"-C", "--directory"} or a.startswith("--directory=")
    )
    if out_flags != 1:
        return False, "tar 必须且只能用一个 -C/--directory 指定输出目录"
    file_flags = sum(
        1 for a in rest if a in {"-f", "--file"} or a.startswith("--file=")
    )
    if file_flags != 1:
        return False, (
            "tar 必须且只能用一个 -f/--file 指定压缩包（多 -f 会让条目预扫描扫错包）"
        )
    if root is not None:
        out_dir = _tar_output_dir(root, rest)
        if out_dir is None:
            return False, (
                "tar 输出目录无法安全解析（必须落在工作区内且不得为工作区根目录）"
            )
    return True, ""


def _tar_scan(archive: Path) -> tuple[str | None, list[str]]:
    """解压前扫描 tar 条目；返回 ``(拒绝原因, 条目名列表)``。

    比 zip 扫描多拒**链接条目**（symlink/hardlink）与设备条目：zip 侧依赖
    「解压后 strip + 废弃目录」兜底，tar 在解压前即拦掉，不留写入窗口（runner
    docstring tar 条）。条目名列表供 M1 的解压后执行位精确清理用。
    TarError/OSError（不存在、非 tar）返回 (None, [])，让 tar 自身报错——
    不在预扫描里越权判死，也不做清理（没有可信条目表）。
    """
    try:
        with tarfile.open(archive) as tf:
            names: list[str] = []
            for i, m in enumerate(tf):
                if i > _MAX_TAR_MEMBERS:
                    logger.warning(
                        "tar entry scan exceeded %s members: %s",
                        _MAX_TAR_MEMBERS,
                        archive,
                    )
                    break
                name = m.name or ""
                names.append(name)
                parts = re.split(r"[\\/]", name)
                if name.startswith(("/", "\\")) or (len(name) > 1 and name[1] == ":"):
                    return f"压缩包含绝对路径条目：{name[:60]!r}", names
                if ".." in parts:
                    return f"压缩包含 .. 穿越条目：{name[:60]!r}", names
                if any(p in _UNZIP_ENTRY_FORBIDDEN for p in parts):
                    return (
                        f"压缩包含 git 配置条目（写入面禁止触及）：{name[:60]!r}",
                        names,
                    )
                if m.issym() or m.islnk():
                    return (
                        "压缩包含链接条目（拒绝解压，符号/硬链接可逃逸输出目录"
                        f"写任意路径）：{name[:60]!r}",
                        names,
                    )
                if m.isdev():
                    return f"压缩包含设备条目（拒绝解压）：{name[:60]!r}", names
    except (tarfile.TarError, OSError):
        return None, []  # 不是合法 tar：让 tar 本身报错，不在这里越权判死
    return None, names


def _tar_entry_problem(archive: Path) -> str | None:
    """薄包装：只取拒绝原因（存量调用方/测试用）。"""
    return _tar_scan(archive)[0]


def strip_symlinks(root: Path) -> int:
    """递归删除 root 下所有符号链接（unzip 解压后调用，防链接逃逸）。返回删除数。"""
    removed = 0
    if not root.is_dir():
        return 0
    scanned = 0
    for p in sorted(root.rglob("*")):
        scanned += 1
        if scanned > _MAX_SYMLINK_SCAN:
            logger.warning(
                "strip_symlinks: %s 条目数超过扫描上限 %d，提前停止（清理为尽力而为）",
                root,
                _MAX_SYMLINK_SCAN,
            )
            break
        try:
            if p.is_symlink():
                p.unlink()
                removed += 1
                logger.warning("removed symlink escaped into workspace: %s", p)
        except OSError:
            logger.exception("failed to remove symlink: %s", p)
    return removed


def strip_entry_exec_bits(base: Path, names: list[str]) -> int:
    """只清除**压缩包条目对应路径**的执行位（base 为解压落点）。返回处理数。

    M1（REVIEW-de09478..workdir）：tar/unzip 会把压缩包里的条目权限位原样
    落盘——0777 条目在 umask 022 下落成 **0755 可执行**。于是 medium 档可用
    curl 拉攻击者 tarball → ``tar -xzf -C scripts`` → 落盘 0755 →
    run_build_script 执行 → 继承完整进程环境，绕过「LLM 没有内容权」的声称。
    按条目精确清理而**不扫整个目录**：管理员在宿主机预置的 scripts/*.sh
    执行位必须保留（run_build_script 依赖它）；工作区工具链（run_command
    白名单）本就不能执行任意二进制，清解压产物执行位不削弱合法用途。
    """
    cleared = 0
    for name in names:
        try:
            p = (base / name).resolve()
        except Exception:
            continue
        if p != base and base not in p.parents:
            continue  # 预扫描已拒穿越条目；双保险
        try:
            if p.is_file() and not p.is_symlink():
                mode = p.stat().st_mode
                if mode & 0o111:
                    p.chmod(mode & ~0o111)
                    cleared += 1
                    logger.info("extracted entry exec bit cleared: %s", p)
        except OSError:
            logger.exception("failed to clear exec bit: %s", p)
    return cleared


class CommandRunner:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def check(
        self, executable: str, args: list[str], *, readonly_only: bool = False
    ) -> tuple[bool, str]:
        """带本 runner 工作区根的 permitted()。"""
        return permitted(executable, args, root=self.root, readonly_only=readonly_only)

    async def run(
        self,
        executable: str,
        args: list[str],
        uid: str = "-",
        *,
        readonly_only: bool = False,
    ) -> str:
        ok, reason = self.check(executable, args, readonly_only=readonly_only)
        if not ok:
            return f"{MSG_REFUSED}：{reason}"
        self.root.mkdir(parents=True, exist_ok=True)

        argv = list(args)
        if executable == "git":
            # 仓库层：驱动型配置无法用 -c 穷举，命中即 fail-closed 拒绝
            blocked = _git_exec_guard(self.root)
            if blocked:
                logger.warning(
                    "workspace git refused by exec guard: uid=%s %s", uid, blocked
                )
                return f"{MSG_REFUSED}：{blocked}"
            # 命令层：-c 覆盖 + diff 禁用外部 diff 驱动
            if args and args[0] == "diff":
                argv = [*_GIT_HARDENING, "diff", "--no-ext-diff", *args[1:]]
            else:
                argv = [*_GIT_HARDENING, *args]
        elif executable == "curl":
            # -q 必须是首个参数：禁止读取 $HOME/.curlrc 等配置文件
            argv = ["-q", *args]

        # 解压产物条目表（M1）：解压后按条目精确清执行位；扫描失败/非压缩包
        # 则为空表，清理自然为空操作
        entry_names: list[str] = []
        extract_base: Path | None = None
        if executable == "unzip":
            # 解压前扫描压缩包条目：.. 穿越 / 绝对路径 / .git* 配置条目直接拒绝
            # （docstring 第 4 条「写入面拒绝触及 git 配置」对解压路径同样生效）
            archive = _unzip_archive_operand(args)
            if archive is not None:
                problem, entry_names = await asyncio.to_thread(
                    _zip_scan, self.root / archive
                )
                if problem:
                    logger.warning(
                        "workspace unzip refused by entry guard: uid=%s %s",
                        uid,
                        problem,
                    )
                    return f"{MSG_REFUSED}：{problem}"
            extract_base = _unzip_output_dir(self.root, args)

        if executable == "tar":
            # 解压前扫条目：.. 穿越 / 绝对路径 / .git* 配置条目 / 链接与设备条目
            # 直接拒绝（tar docstring 条）；归一化后 -C/-f 为独立 token
            rest, _reason = _tar_normalize(args)
            if rest is not None:
                archive = _tar_archive_operand(rest)
                if archive is not None:
                    problem, entry_names = await asyncio.to_thread(
                        _tar_scan, self.root / archive
                    )
                    if problem:
                        logger.warning(
                            "workspace tar refused by entry guard: uid=%s %s",
                            uid,
                            problem,
                        )
                        return f"{MSG_REFUSED}：{problem}"
                # GNU tar 不会自动创建 -C 输出目录（unzip 会）：路径已被 permitted
                # 遏制在工作区内，先建好再 spawn，避免 tar 报 "Cannot open" 半途而废
                out_dir = _tar_output_dir(self.root, rest)
                if out_dir is not None:
                    try:
                        await asyncio.to_thread(
                            out_dir.mkdir, parents=True, exist_ok=True
                        )
                    except OSError:
                        logger.exception("tar output dir create failed: %s", out_dir)
                        return "(创建 tar 输出目录失败，请检查路径权限)"
                # tar 未指定 -C 时条目落在 cwd（= 工作区根）：精确清理以根为基点
                extract_base = out_dir if out_dir is not None else self.root

        exe_path = shutil.which(executable)
        logger.info(
            "workspace cmd: uid=%s cwd=%s cmd=%s %s", uid, self.root, executable, argv
        )
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
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return f"(命令超时 {_CMD_TIMEOUT}s，已终止)"
        except Exception:
            logger.exception("workspace cmd failed")
            proc.kill()
            await proc.wait()
            return "(执行失败，请稍后再试)"

        # M1/L18：unzip 解压后清除符号链接（docstring 宣称的安全属性）。清除发生在
        # 解压完成之后，存在「解压期经 symlink 穿透写入」的窗口——因此只要检出过
        # symlink，整个解压输出目录即废弃，不让任何内容留在工作区。
        # （先 strip 再 rmtree：链接已断开，rmtree 不会沿符号链接逃逸。）
        if executable == "unzip":
            out_dir = _unzip_output_dir(self.root, args)
            if out_dir is not None:
                removed = await asyncio.to_thread(strip_symlinks, out_dir)
                if removed:
                    logger.warning(
                        "unzip: %s symlink(s) under %s; dropping entire output",
                        removed,
                        out_dir,
                    )
                    if out_dir == self.root:
                        # permitted 已拒绝 -d 指向工作区根；此守卫防御未来回归——
                        # 数据丢失比清理更糟，宁可保留现场
                        logger.error("unzip: output dir is workspace root, skip rmtree")
                        return (
                            f"解压内容含符号链接（{removed} 个），但输出目录为工作区根，"
                            "为避免误删整个工作区未做清理，请人工检查后处理。"
                        )
                    await asyncio.to_thread(shutil.rmtree, out_dir, True)
                    return (
                        f"解压内容含符号链接（{removed} 个），已丢弃全部解压结果："
                        "符号链接可能逃逸出输出目录读写任意路径。"
                        "请确认压缩包来源可信后再试。"
                    )

        # M1/L18：tar 解压后同样清除符号链接并检出即废弃输出目录（与 unzip 同口径；
        # 链接条目本已在解压前被 _tar_scan 拒绝，这里是纵深防御——解压过程中经
        # 竞态/新形态产生的链接也要清掉）。
        if executable == "tar":
            rest, _reason = _tar_normalize(args)
            if rest is not None:
                out_dir = _tar_output_dir(self.root, rest)
                if out_dir is not None:
                    removed = await asyncio.to_thread(strip_symlinks, out_dir)
                    if removed:
                        logger.warning(
                            "tar: %s symlink(s) under %s; dropping entire output",
                            removed,
                            out_dir,
                        )
                        # permitted 已拒绝 -C 指向工作区根，out_dir 理论上不可能
                        # 等于 root；真出现即防御未来回归——保留现场不删
                        if out_dir == self.root:
                            logger.error(
                                "tar: output dir is workspace root, skip rmtree"
                            )
                            return (
                                f"解压内容含符号链接（{removed} 个），但输出目录为工作区根，"
                                "为避免误删整个工作区未做清理，请人工检查后处理。"
                            )
                        await asyncio.to_thread(shutil.rmtree, out_dir, True)
                        return (
                            f"解压内容含符号链接（{removed} 个），已丢弃全部解压结果："
                            "符号链接可能逃逸出输出目录读写任意路径。"
                            "请确认压缩包来源可信后再试。"
                        )

        # M1（REVIEW-de09478..workdir）：tar/unzip 解压后按**条目表**精确清除
        # 执行位（0755 的 scripts/*.sh 会被 medium 档 run_build_script 直接执行，
        # 构成「curl 拉包 → 解压 → 执行」的任意代码 + 完整环境链）。只清压缩包
        # 真实写下的条目——管理员在宿主机预置的 scripts/*.sh 执行位必须保留
        # （run_build_script 靠它 execve）。目录已因符号链接废弃时为 0。
        if executable in {"unzip", "tar"} and entry_names:
            cleared = await asyncio.to_thread(
                strip_entry_exec_bits,
                extract_base if extract_base is not None else self.root,
                entry_names,
            )
            if cleared:
                logger.info("%s: 清除 %d 个解压产物的执行位（M1）", executable, cleared)

        text = (data or b"").decode("utf-8", errors="replace")
        if truncated:
            text += f"\n…（输出过长，已截断至 {_MAX_OUTPUT_BYTES} 字节）"
        if len(text) > _MAX_OUTPUT_CHARS:
            text = (
                text[:_MAX_OUTPUT_CHARS]
                + f"\n…（输出过长，截断前 {_MAX_OUTPUT_CHARS} 字符）"
            )
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
