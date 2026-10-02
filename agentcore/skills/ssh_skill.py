"""SSH 远程只读诊断（仅管理员）：按 alias 从 .env 预置 host/凭据，命令表写死。

安全边界：
- host/端口/用户只来自 ``AGENT_SSH_HOSTS``（``alias=user@host:port``，逗号分隔），
  LLM **无法指定主机**——不存在 SSRF/内网漫游面；未登记的 alias 一律拒绝
  （fail-closed，不配 ``AGENT_SSH_HOSTS`` 等于整个功能关闭）
- 远端命令是代码写死的常量（action 菜单），LLM 只报动作名。ssh 会把命令行交给
  **远端用户 shell** 执行，自由拼接等于把远端 RCE 权交给 LLM——故此处不容许
  任何字符串插值，action 之外没有任何输入能进入远端 shell
- 凭据只来自 ``AGENT_SSH_KEY_<ALIAS>``（私钥路径，优先）或
  ``AGENT_SSH_PASSWORD_<ALIAS>``；密码经环境变量注入 sshpass，**不进 argv、
  不进审计日志、不进返回值**（tool call 参数会全量落 agent.log）
- 连接参数：``BatchMode=yes``（禁交互）、``ConnectTimeout=5``、
  ``StrictHostKeyChecking=accept-new`` + 专用 known_hosts（TOFU：首次连接自动
  登记并在输出里留下 Warning，之后严格校验——注意**不能**设 ``LogLevel=ERROR``，
  那会把 INFO 级的 TOFU Warning 一并吞掉，REVIEW-3ce6e0a..de09478 M4 实测）、
  ``-F /dev/null``（不读 bot 用户自己的 ~/.ssh/config，防 ProxyCommand 这类
  配置驱动执行）、``IdentitiesOnly=yes``
- 输出 20s 超时 + 8000B 流式截断；失败/超时只返回说明文字，不重试
- known_hosts 若落在 fs 可写工作区（``WORKSPACE_DIR``）内则拒绝执行——那里
  fs_write 可达，等于允许预埋 pinned host key（REVIEW 同报告 L15）

已知残余风险：``accept-new`` 的首次连接可被同网段攻击者 MITM（换来的是一次远程
只读命令执行权），需要收紧就预置 known_hosts 指纹；远端 busybox/GNU 工具差异
导致的命令失败只影响可用性，不影响边界。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from pathlib import Path

from agentcore.skills.registry import SkillRegistry
from agentcore.workspace.utils import is_superuser, workspace_root

logger = logging.getLogger(__name__)

# 远端只读诊断动作表：LLM 只能点名，命令串是常量（见模块 docstring）
_ACTIONS: dict[str, str] = {
    "free": "free -m",
    "uptime": "uptime",
    "hostname": "hostname",
    "uname": "uname -a",
    "ps": "ps w",
    "ps_mem": "ps ww",
    "top": "top -b -n 1",
    "meminfo": "cat /proc/meminfo",
    "cpuinfo": "cat /proc/cpuinfo | head -40",
    "disks": "df -h",
    "mounts": "mount",
    "dmesg": "dmesg | tail -30",
    "logread": "logread | tail -50",
    "netstat": "netstat -tuln",
    "ifconfig": "ifconfig",
    "services": "ls /etc/init.d",
    "users": "who",
}

_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_USER_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9._-]{1,253}$")

_SSH_TIMEOUT = 20  # 与 workspace runner 同口径
_CONNECT_TIMEOUT = 5
_MAX_OUTPUT_BYTES = 8000  # 流式读取上限（字节），超出即终止进程
_MAX_OUTPUT_CHARS = 4000  # 返回给 LLM 的字符上限

_DEFAULT_KNOWN_HOSTS = "data/ssh/known_hosts"


def _env_key(alias: str) -> str:
    """alias → env 变量名后缀：大写、非字母数字转下划线。"""
    return re.sub(r"[^A-Z0-9]", "_", alias.upper())


def parse_hosts(raw: str) -> dict[str, dict[str, str | int]]:
    """解析 ``AGENT_SSH_HOSTS``：``alias=user@host:port``，逗号分隔。

    非法条目记 WARNING 后跳过（不静默吞掉，否则管理员以为配上了）。
    """
    hosts: dict[str, dict[str, str | int]] = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        alias, sep, rest = part.partition("=")
        if not sep or not _ALIAS_RE.match(alias):
            logger.warning("ssh hosts: 非法条目（alias 形态不对）：%r", part)
            continue
        user, sep, hostport = rest.rpartition("@")
        if not sep or not _USER_RE.match(user):
            logger.warning("ssh hosts: 非法条目（user 形态不对）：%r", part)
            continue
        host, sep, port = hostport.rpartition(":")
        if not sep or not _HOST_RE.match(host):
            logger.warning("ssh hosts: 非法条目（host 形态不对）：%r", part)
            continue
        try:
            port_no = int(port)
        except ValueError:
            logger.warning("ssh hosts: 非法条目（端口不是数字）：%r", part)
            continue
        if not 1 <= port_no <= 65535:
            logger.warning("ssh hosts: 非法条目（端口越界）：%r", part)
            continue
        hosts[alias] = {"user": user, "host": host, "port": port_no}
    return hosts


def load_hosts() -> dict[str, dict[str, str | int]]:
    return parse_hosts(os.getenv("AGENT_SSH_HOSTS") or "")


def _known_hosts_path() -> Path:
    p = Path(os.getenv("AGENT_SSH_KNOWN_HOSTS") or _DEFAULT_KNOWN_HOSTS)
    try:
        p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        logger.warning("ssh known_hosts 目录创建失败：%s", p.parent)
    return p


def _credential(alias: str) -> tuple[str, str]:
    """返回 ("key"|"password"|"missing", 值)。密钥优先；两者都无即未配置。"""
    key = (os.getenv(f"AGENT_SSH_KEY_{_env_key(alias)}") or "").strip()
    if key:
        p = Path(key)
        try:
            st = p.stat()
        except OSError:
            return "missing", ""
        if not p.is_file():
            return "missing", ""
        if st.st_mode & 0o077:
            # ssh 自己也会拒绝 group/world 可读私钥；这里提前给明确原因
            logger.warning(
                "ssh key %s 权限过宽（%o），已按缺失处理", key, st.st_mode & 0o777
            )
            return "missing", ""
        return "key", str(p)
    pw = os.getenv(f"AGENT_SSH_PASSWORD_{_env_key(alias)}") or ""
    if pw:
        return "password", pw
    return "missing", ""


def _base_argv(host: dict[str, str | int]) -> list[str]:
    return [
        "ssh",
        "-p",
        str(host["port"]),
        "-F",
        os.devnull,  # 不读 bot 用户自己的 ~/.ssh/config（ProxyCommand 等）
        "-o",
        "BatchMode=yes",  # 绝不弹交互
        "-o",
        f"ConnectTimeout={_CONNECT_TIMEOUT}",
        "-o",
        "StrictHostKeyChecking=accept-new",  # TOFU：首次登记，之后严格
        "-o",
        f"UserKnownHostsFile={_known_hosts_path()}",
        "-o",
        "IdentitiesOnly=yes",
    ]


def _child_env(password: str | None) -> dict[str, str]:
    """最小环境：不继承 LLM API key 等；HOME 指向不存在的位置。"""
    env = {
        "PATH": os.environ.get(
            "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        ),
        "HOME": "/nonexistent",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    if password is not None:
        # SSH_ASKPASS 环境变量只有同 uid/root 能读；argv 与日志都不会出现它
        env["SSHPASS"] = password
    return env


async def _read_capped(proc) -> tuple[bytes, bool]:
    """流式读取，超限立即终止（避免把 GB 级输出全缓冲进内存）。"""
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


async def ssh_run(alias: str, action: str) -> str:
    """在 .env 登记的远端主机上执行一个**写死的只读**诊断命令。"""
    alias = (alias or "").strip()
    action = (action or "").strip()
    if not _ALIAS_RE.match(alias):
        return f"拒绝：alias 形态不合法（{alias!r}，需小写字母/数字/中划线/下划线开头）"
    hosts = load_hosts()
    host = hosts.get(alias)
    if host is None:
        configured = "、".join(sorted(hosts)) or "（未配置任何主机）"
        return (
            f"拒绝：alias「{alias}」未在 AGENT_SSH_HOSTS 登记。"
            f"当前可用：{configured}。请让管理员先在 .env 登记后重启生效。"
        )
    remote_cmd = _ACTIONS.get(action)
    if remote_cmd is None:
        return (
            f"拒绝：未知 action「{action}」。"
            f"可用动作：{'、'.join(sorted(_ACTIONS))}（命令由系统写死，不可自定义）"
        )

    mode, cred = _credential(alias)
    if mode == "missing":
        return (
            f"拒绝：alias「{alias}」未配置凭据。"
            f"请在 .env 设置 AGENT_SSH_KEY_{_env_key(alias)}（私钥路径，推荐）"
            f"或 AGENT_SSH_PASSWORD_{_env_key(alias)} 后重启。"
        )

    # known_hosts 落在 fs 可写工作区内 = 注入内容可经 fs_write 预埋/替换
    # pinned host key（TOFU 的信任根失守），fail-closed 拒绝。
    kh = _known_hosts_path().resolve()
    wr = workspace_root()
    if kh == wr or wr in kh.parents:
        return (
            f"拒绝：known_hosts（{kh}）落在 fs 可写工作区（{wr}）内，"
            "可被预埋 host key。请把 AGENT_SSH_KNOWN_HOSTS 指到工作区外。"
        )

    argv = _base_argv(host)
    password: str | None = None
    if mode == "key":
        argv += ["-i", cred]
    else:
        if not shutil.which("sshpass"):
            return "(执行失败：系统没有 sshpass，无法用密码模式登录)"
        argv = ["sshpass", "-e", *argv]
        password = cred
    # `--` 在 destination **之前**：终结本地选项解析（_HOST_RE 允许 host 以
    # `-` 开头，不隔离会被 ssh 当成选项）；destination 之后的 remote_cmd 本就
    # 不参与选项解析，`--` 放那里是无效位置（REVIEW-3ce6e0a..de09478 L13）。
    argv += ["--", f"{host['user']}@{host['host']}", remote_cmd]

    started = time.monotonic()
    logger.info(
        "ssh_run: alias=%s action=%s mode=%s cmd=%s",
        alias,
        action,
        mode,
        remote_cmd,
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            env=_child_env(password),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except Exception:
        logger.exception("ssh_run spawn failed: alias=%s", alias)
        return "(执行失败，请稍后再试)"
    try:
        data, truncated = await asyncio.wait_for(
            _read_capped(proc), timeout=_SSH_TIMEOUT
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        logger.warning("ssh_run timeout: alias=%s action=%s", alias, action)
        return f"(SSH 命令超时 {_SSH_TIMEOUT}s，已终止)"
    except Exception:
        logger.exception("ssh_run failed: alias=%s", alias)
        proc.kill()
        await proc.wait()
        return "(执行失败，请稍后再试)"

    elapsed = round(time.monotonic() - started, 2)
    logger.info(
        "ssh_run done: alias=%s action=%s elapsed=%ss bytes=%s",
        alias,
        action,
        elapsed,
        len(data or b""),
    )
    text = (data or b"").decode("utf-8", errors="replace")
    if truncated:
        text += f"\n…（输出过长，已截断至 {_MAX_OUTPUT_BYTES} 字节）"
    if len(text) > _MAX_OUTPUT_CHARS:
        text = (
            text[:_MAX_OUTPUT_CHARS]
            + f"\n…（输出过长，截断前 {_MAX_OUTPUT_CHARS} 字符）"
        )
    return text or "(无输出)"


def register_ssh_skills(registry: SkillRegistry) -> None:
    """注册 SSH 只读诊断技能（仅管理员）。"""

    @registry.register(
        "ssh_run",
        "SSH 登录管理员在 .env 预置的内网主机，执行**写死的只读**诊断命令（仅管理员）。"
        "alias 必须是 AGENT_SSH_HOSTS 登记过的名字；action 只能是："
        + "、".join(sorted(_ACTIONS))
        + "。远程命令不可自定义（没有 shell/管道权限）。"
        "查本机直接用 proc_detail/system_status/run_command。",
        {
            "type": "object",
            "properties": {
                "alias": {
                    "type": "string",
                    "description": "AGENT_SSH_HOSTS 里登记的主机别名，如 istore",
                },
                "action": {
                    "type": "string",
                    "enum": sorted(_ACTIONS),
                    "description": "只读诊断动作名",
                },
            },
            "required": ["alias", "action"],
        },
        permission="superuser",
    )
    async def ssh_run_skill(alias: str, action: str, user_id: str = "") -> str:
        if not is_superuser(user_id):
            return "无权限：仅管理员可执行 SSH 诊断。"
        return await ssh_run(alias, action)
