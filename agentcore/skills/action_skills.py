"""管理员动作类 skill（**仅 superuser**）：服务/容器控制、进程终止、构建脚本。

安全模型（与 ops_skills 的只读查询互补——本模块全是**有副作用**的动作）：
- 命令由代码写死或经**枚举校验**：LLM 只能给「枚举动作 + 目标名」，
  无法拼接任意命令/参数（对比 run_command：那边是通用白名单 shell -less 校验）
- 能力开关 = ``AGENT_PERMISSION_LEVEL``（见 skills/levels.py）：本模块的
  service_ctrl/docker_ctrl/proc_kill/run_build_script 需要 **medium+**（low 档
  不注册）；docker_ps/docker_logs 只读，low 即可用
- 自保护（medium+ 仍生效）：service_ctrl 拒绝本机器人自身的 systemd unit
  （/proc/self/cgroup 识别，非 systemd 部署识别不到则无此保护）、
  docker_ctrl 拒绝 ``PG_CONTAINER`` 与 bot 自身容器——自身重启走 /reboot
  （有优雅停机 + 回执），硬停 DB 容器等于自断粮
- 不可回退动作（proc_kill）走**两段确认**：第一次调用只返回确认 token 与
  目标进程清单，同参数二次调用（带 token）才真正执行——reboot.py 两段确认的
  skill 版；token 有 TTL（``AGENT_KILL_CONFIRM_TTL``），过期重发；
  拒绝 pid 1 与 bot 自身；一次最多杀 ``_MAX_KILL_PIDS`` 个
- 构建脚本只执行工作区内 ``scripts/`` 下预先写好的脚本。**内容权边界（M1，
  REVIEW-de09478..workdir）**：同档（medium+）fs_write 可写该目录、curl+tar
  可落盘攻击者压缩包，因此 LLM 对脚本内容并非完全封锁——直接覆盖会被
  ``fs.write`` 的 0600 原子写剥掉执行位（exec 失败解除了大部分危害），而
  tar/unzip 落盘的执行位由 runner 解压后按条目表统一清除。真实防线是：
  medium 档起步 + superuser 双层闸 + 输出过围栏 + 「无内容权」仅指日常路径。
  需要完整构建环境的场景因此继承进程环境（与 run_command 的最小环境不同）。
- 输出/日志经 ``agentcore.safety.fence_untrusted`` 围栏后才进 prompt
  （服务输出与构建日志视同外部数据，与 search_web 同等待遇）
- 引擎的只读并行名单**只收** docker_ps/docker_logs 这类纯查询；其余全部
  fail-closed 串行执行
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import shlex
import time

from agentcore.safety import fence_untrusted
from agentcore.skills.command_util import run_action, run_readonly, which
from agentcore.skills.levels import at_least
from agentcore.workspace.utils import is_superuser, workspace_root

logger = logging.getLogger(__name__)

# 服务名/容器名/脚本名形状（与 ops_skills._UNIT_RE 同口径）
_UNIT_RE = re.compile(r"^[A-Za-z0-9@._:-]{1,64}$")
_CONTAINER_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SCRIPT_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

_SERVICE_ACTIONS = {"restart", "start", "stop"}
_KILL_SIGNALS = {"TERM": 15, "KILL": 9}

# 参数禁用的 shell 元字符（与 workspace/runner 同一口径：argv 直送 execve 本无
# shell 解释面，这里是纵深防御 + 防审计日志伪造）
_DENIED_ARG_CHARS = set("|;&><`$\n\r")

# proc_kill：一次最多允许杀的进程数（白名单 pattern 也可能匹配到一批）
_MAX_KILL_PIDS = 10

# ---------- 两段确认的待确认表 ----------
# key=(user_id, pattern, signal) → {"token": str, "ts": float}；单事件循环下
# 读写块内无 await，天然原子；惰性清理过期项，不额外起后台任务。
_PENDING: dict[tuple, dict] = {}


def _now() -> float:
    return time.monotonic()


def _prune_pending(now: float, ttl: float) -> None:
    for k in [k for k, v in _PENDING.items() if now - v["ts"] > ttl]:
        _PENDING.pop(k, None)


def _env_int(name: str, default: int, low: int, high: int) -> int:
    """env 整数：脏值/越界告警后回退默认（与引擎/rag 同款纪律）。"""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r 不是整数，回退默认 %s", name, raw, default)
        return default
    if not low <= value <= high:
        logger.warning(
            "%s=%s 超出 [%s, %s]，回退默认 %s", name, raw, low, high, default
        )
        return default
    return value


# ---------- 自保护识别（零配置：从 /proc/self/cgroup 认出「自己」） ----------
def _cgroup_text() -> str:
    try:
        with open("/proc/self/cgroup", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _self_unit() -> str:
    """本进程的 systemd unit 名（cgroup 条目末段 ``*.service``）。

    非 systemd 部署（裸 nohup / 容器）识别不到，返回空串——此时 service_ctrl
    无自保护（README 已注明该限制）。
    """
    for entry in _cgroup_text().split():
        last = entry.rsplit("/", 1)[-1]
        if last.endswith(".service"):
            return last
    return ""


def _self_container_id() -> str:
    """本进程所在容器的 64 位 id（cgroup v1 ``/docker/<id>`` 或 v2
    ``docker-<id>.scope``）；非容器部署返回空串。"""
    for entry in _cgroup_text().split():
        last = entry.rsplit("/", 1)[-1]
        if last.startswith("docker-") and last.endswith(".scope"):
            return last[len("docker-") : -len(".scope")]
        if "/docker/" in entry:
            tail = entry.rsplit("/docker/", 1)[1]
            if tail:
                return tail.split("/")[0]
    return ""


def _unit_is_self(unit: str) -> bool:
    """unit 参数是否指向 bot 自身（``foo`` 与 ``foo.service`` 等价）。"""
    self_unit = _self_unit()
    if not self_unit:
        return False
    bare = (
        self_unit[: -len(".service")] if self_unit.endswith(".service") else self_unit
    )
    return unit in {self_unit, bare}


def _container_is_self(name: str) -> bool:
    """容器参数是否指向 bot 自身容器（完整 id 或 ≥12 位前缀，docker 两种都认）。"""
    cid = _self_container_id()
    if not cid:
        return False
    return name == cid or (len(name) >= 12 and cid.startswith(name))


# 自身容器**名字**集合缓存（M6，REVIEW-de09478..workdir）：
# cgroup 只能给出容器 id，而 docker ps 语境下用户/LLM 引用容器**名字**才是主
# 形态——只认 id 时按名操作自身容器可穿过自保护（compose 部署下 bot 容器有
# 人可读的名字）。查询走只读 docker ps（id→names 映射），TTL 缓存避免每次
# docker_ctrl 都起一次子进程；查询失败 fail-open 回退仅 id 通道（与旧行为一致，
# 非容器部署本就不需要名字通道）。
_SELF_NAMES_CACHE: tuple[float, set[str]] = (0.0, set())
_SELF_NAMES_TTL = 300


async def _self_container_names() -> set[str]:
    """本进程所在容器的全部名字（docker ps 映射）；不在容器/查询失败返回空集。"""
    global _SELF_NAMES_CACHE
    cid = _self_container_id()
    if not cid:
        return set()
    now = _now()
    ts, cached = _SELF_NAMES_CACHE
    if cached and now - ts < _SELF_NAMES_TTL:
        return cached
    names: set[str] = set()
    if which("docker"):
        try:
            out = await run_readonly(
                ["docker", "ps", "--all", "--format", "{{.ID}} {{.Names}}"],
                timeout=15,
                max_lines=200,
            )
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 2 and cid.startswith(parts[0]):
                    # Names 可为逗号分隔的多名字（compose/网络别名）
                    names.update(p for p in parts[1].split(",") if p)
        except Exception:
            logger.debug("docker ps self-name lookup failed", exc_info=True)
    _SELF_NAMES_CACHE = (now, names)
    return names


async def _container_is_self_by_name(name: str) -> bool:
    """name 通道的自保护判定（调 _self_container_names；供 docker_ctrl 用）。"""
    return bool(name) and name in await _self_container_names()


_LOW_DENIED = (
    "当前权限级别为 low：{}需要 medium 及以上（AGENT_PERMISSION_LEVEL，改后重启生效）。"
)


# ---------- 服务控制 ----------
async def service_ctrl(action: str, unit: str) -> str:
    action = (action or "").strip().lower()
    unit = (unit or "").strip()
    if action not in _SERVICE_ACTIONS:
        return f"错误：action 只能是 {sorted(_SERVICE_ACTIONS)} 之一"
    if not _UNIT_RE.match(unit):
        return "错误：服务名含非法字符"
    if not at_least("medium"):
        return _LOW_DENIED.format("服务控制")
    if _unit_is_self(unit):
        return (
            f"拒绝：{unit} 是本机器人自身的服务。硬停会跳过优雅停机（防抖队列"
            "未 flush 就退出，消息会丢），重启请走 /reboot（回复发出后按序停机）。"
        )
    if not which("systemctl"):
        return "(系统没有 systemctl)"
    out = await run_action(["systemctl", action, unit], timeout=60, max_lines=8)
    state = await run_readonly(["systemctl", "is-active", unit], max_lines=2)
    return f"systemctl {action} {unit}：\n{out}\n当前状态（is-active）：{state}"


# ---------- 容器 ----------
async def docker_ps(all: bool = False) -> str:
    if not which("docker"):
        return "(系统没有 docker)"
    argv = [
        "docker",
        "ps",
        "--format",
        "table {{.Names}}\t{{.Status}}\t{{.Ports}}",
    ]
    if all:
        argv.append("--all")
    return await run_readonly(argv, timeout=15, max_lines=25)


async def docker_logs(container: str, lines: int = 50) -> str:
    container = (container or "").strip()
    if not _CONTAINER_RE.match(container):
        return "错误：容器名含非法字符"
    lines = max(1, min(int(lines or 50), 200))
    if not which("docker"):
        return "(系统没有 docker)"
    return await run_readonly(
        ["docker", "logs", "--tail", str(lines), container],
        timeout=15,
        max_lines=lines + 5,
    )


async def docker_ctrl(action: str, container: str) -> str:
    action = (action or "").strip().lower()
    container = (container or "").strip()
    if action not in _SERVICE_ACTIONS:
        return f"错误：action 只能是 {sorted(_SERVICE_ACTIONS)} 之一"
    if not _CONTAINER_RE.match(container):
        return "错误：容器名含非法字符"
    if not at_least("medium"):
        return _LOW_DENIED.format("容器控制")
    pg = (os.getenv("PG_CONTAINER") or "").strip()
    if pg and container == pg:
        return (
            f"拒绝：{container} 是本机器人的数据库容器。停止它等于让 bot 自断粮"
            "（记忆/知识库全部不可用），确需操作请直接在宿主机执行。"
        )
    # M6：id 通道（cgroup）+ 名字通道（docker ps 映射）双判定——docker ps 语境
    # 下按名操作自身容器是主形态，只认 id 会被名字穿过。
    if _container_is_self(container) or await _container_is_self_by_name(container):
        return (
            f"拒绝：{container} 是本机器人自身的容器。停止它等于自杀，"
            "重启请走 /reboot。"
        )
    if not which("docker"):
        return "(系统没有 docker)"
    out = await run_action(["docker", action, container], timeout=60, max_lines=8)
    return f"docker {action} {container}：\n{out}"


# ---------- 进程终止（两段确认） ----------
def _scan_pids(pattern: str) -> list[int]:
    """纯 Python 扫描 /proc 的 cmdline，返回匹配 pattern 的所有 pid（pgrep -f 同义）。

    不依赖 pgrep 二进制（容器里常没有），也天然可单测。**不过滤** pid 1/自身
    （那是 _kill_targets 的职责：原始扫描结果可用于展示，过滤后用于执行）。
    匹配大小写敏感，与 pgrep -f 的默认行为一致。
    """
    found: list[int] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "replace")
        except OSError:
            continue  # 进程已退出 / 权限不足
        if pattern in cmd:
            found.append(int(entry))
    return sorted(found)


_SELF_PID = os.getpid()


def _kill_targets(pattern: str) -> list[int]:
    """过滤后的可杀 pid：排除 pid 1 与 bot 自身（不可回退动作的硬边界）。"""
    return [p for p in _scan_pids(pattern) if p > 1 and p != _SELF_PID]


async def proc_kill(
    pattern: str, signal: str = "TERM", token: str = "", user_id: str = ""
) -> str:
    """两段确认杀进程：无 token 时返回确认 token 与目标清单；带正确 token 才真杀。"""
    pattern = (pattern or "").strip()
    signal = (signal or "TERM").strip().upper()
    if signal not in _KILL_SIGNALS:
        return f"错误：signal 只能是 {sorted(_KILL_SIGNALS)} 之一"
    if not at_least("medium"):
        return _LOW_DENIED.format("杀进程")
    # pattern 白名单已随级别制移除：pattern 即调用参数（pgrep -f 语义），
    # 误杀防线 = 两段确认 + pid1/自身排除 + 匹配数上限，而非目标表

    now = _now()
    ttl = _env_int("AGENT_KILL_CONFIRM_TTL", 120, 5, 3600)
    _prune_pending(now, ttl)
    key = (user_id or "-", pattern, signal)
    pids = await asyncio.to_thread(_kill_targets, pattern)
    if len(pids) > _MAX_KILL_PIDS:
        return (
            f"拒绝：pattern {pattern!r} 匹配到 {len(pids)} 个进程"
            f"（上限 {_MAX_KILL_PIDS}）——请把 pattern 配得更具体"
        )
    if not pids:
        return f"没有匹配 {pattern!r} 的进程（pid 1 与 bot 自身被刻意排除）"

    if not token:
        new_token = secrets.token_hex(4)
        _PENDING[key] = {"token": new_token, "ts": now}
        return (
            f"确认：将向 {len(pids)} 个进程发送 SIG{signal}：{pids}\n"
            f"确认码 {new_token}（{ttl} 秒内有效）。"
            "确认无误后带上该确认码重新调用本工具即可执行；不确认则什么都不做。"
        )

    pending = _PENDING.get(key)
    if pending is None or not secrets.compare_digest(pending["token"], token):
        _PENDING.pop(key, None)
        return "错误：确认码不正确或已过期——请重新发起（不带 token 调用一次）"

    # 二次调用时**重新解析** pid：进程可能已退出/新生，按当前状态执行
    pids = await asyncio.to_thread(_kill_targets, pattern)
    _PENDING.pop(key, None)
    if not pids:
        return "目标进程已不存在，未执行任何操作"
    sig = _KILL_SIGNALS[signal]
    results = []
    for pid in pids:
        try:
            os.kill(pid, sig)
            results.append(f"{pid} 已发送 SIG{signal}")
        except ProcessLookupError:
            results.append(f"{pid} 已退出")
        except PermissionError:
            results.append(f"{pid} 无权限（拒绝）")
        except OSError as e:
            results.append(f"{pid} 失败：{e}")
    logger.warning(
        "proc_kill executed: user=%s pattern=%r signal=%s pids=%s",
        user_id,
        pattern,
        signal,
        pids,
    )
    return "执行结果：\n" + "\n".join(results)


# ---------- 构建脚本 ----------
def _args_ok(args: str, root) -> tuple[list[str] | None, str]:
    """构建脚本参数：shlex 拆分 + shell 元字符拒绝 + 路径参数遏制在工作区内。"""
    if not args or not args.strip():
        return [], ""
    try:
        parts = shlex.split(args)
    except ValueError as e:
        return None, f"错误：参数解析失败（{e}）"
    for a in parts:
        if any(c in _DENIED_ARG_CHARS for c in a):
            return None, f"错误：参数含 shell 元字符（{a!r}）"
        if ".." in re.split(r"[/\\]", a):
            return None, f"错误：参数含 ..（{a!r}）"
        if "/" in a:
            try:
                p = (root / a).resolve()
            except Exception:
                return None, f"错误：参数路径无法解析（{a!r}）"
            if p != root and root not in p.parents:
                return None, f"错误：参数路径超出工作区（{a!r}）"
    return parts, ""


async def run_build_script(name: str, args: str = "") -> str:
    """执行工作区 scripts/ 下的**预先写好的**构建脚本（仅 medium+）。"""
    name = (name or "").strip()
    if not _SCRIPT_NAME_RE.match(name):
        return "错误：脚本名含非法字符（仅限字母数字下划线连字符）"
    if not at_least("medium"):
        return _LOW_DENIED.format("构建脚本")

    root = workspace_root()
    script = root / "scripts" / f"{name}.sh"
    try:
        script = script.resolve()
    except OSError:
        return "错误：脚本路径无法解析"
    if not (script.is_file() and (root / "scripts") in script.parents):
        return f"找不到脚本：{script}"

    parts, reason = _args_ok(args, root)
    if parts is None:
        return reason

    timeout = _env_int("AGENT_BUILD_TIMEOUT", 300, 1, 3600)
    # 环境变量**继承**（与 run_command 的最小环境不同）：构建通常需要完整环境
    # （node/npm 缓存、.env 注入的前端变量等）。注意脚本内容并非对 LLM 完全
    # 封锁（同档 fs_write 可写 scripts/、curl+tar 可落盘攻击者压缩包，
    # REVIEW-de09478..workdir M1）——防守叠加：medium 档起步 + superuser 双层
    # 闸 + fs.write 的 0600 原子写剥执行位 + tar/unzip 解压后条目执行位清除 +
    # 输出过围栏（防构建日志夹带注入文本）。
    logger.info("build script: %s %s (timeout=%ss)", script, parts, timeout)
    try:
        proc = await asyncio.create_subprocess_exec(
            str(script),
            *parts,
            cwd=str(root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except Exception as e:
        return f"(启动失败：{e})"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return f"(构建超时 {timeout}s，已终止)"
    except Exception as e:
        logger.exception("build script failed")
        return f"(执行失败：{e})"
    text = (out or b"").decode("utf-8", errors="replace").strip()
    lines = text.splitlines()
    if len(lines) > 60:
        text = "\n".join(lines[:60]) + f"\n…（仅显示前 60 行，共 {len(lines)} 行）"
    if len(text) > 4000:
        text = text[:4000] + "\n…（输出过长，已截断至 4000 字符）"
    text = text or "(无输出)"
    return fence_untrusted("构建脚本输出", text, "脚本输出")


# ---------- 注册 ----------
def register_action_skills(registry) -> None:
    """注册管理员动作技能（仅 superuser 可见可用）。

    注册分两段：docker_ps/docker_logs 只读，low+ 注册；其余四个有副作用的
    动作只在 medium+ 注册（low 档 schema 不可见，幻觉调用得 unknown）。
    handler 内 ``is_superuser`` 二次校验是刻意保留的纵深（P0）：registry 层
    权限曾被 config.yaml 通配符击穿，而本模块全部是敏感操作。
    """

    @registry.register(
        "docker_ps",
        "查看 docker 容器列表（仅管理员，只读）。all=true 时包含已停止的容器。",
        {
            "type": "object",
            "properties": {
                "all": {"type": "boolean", "description": "是否包含已停止容器"}
            },
            "required": [],
        },
        permission="superuser",
    )
    async def docker_ps_skill(all: bool = False, user_id: str = "") -> str:
        if not is_superuser(user_id):
            return "无权限：仅管理员可查看容器。"
        return await docker_ps(all)

    @registry.register(
        "docker_logs",
        "查看 docker 容器日志（仅管理员，只读）。lines 最多 200 行。",
        {
            "type": "object",
            "properties": {
                "container": {"type": "string", "description": "容器名"},
                "lines": {
                    "type": "integer",
                    "description": "最后多少行，默认 50，最多 200",
                },
            },
            "required": ["container"],
        },
        permission="superuser",
    )
    async def docker_logs_skill(
        container: str, lines: int = 50, user_id: str = ""
    ) -> str:
        if not is_superuser(user_id):
            return "无权限：仅管理员可查看容器日志。"
        return await docker_logs(container, lines)

    # ---- 有副作用的动作类：medium+ 才注册（低级别 schema 不可见）----
    if not at_least("medium"):
        return

    @registry.register(
        "service_ctrl",
        "重启/启动/停止 systemd 服务（仅管理员，medium+）。任意 unit 可控，"
        "但本机器人自身的服务受保护——重启 bot 请走 /reboot（优雅停机+回执）。",
        {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["restart", "start", "stop"],
                    "description": "动作",
                },
                "unit": {"type": "string", "description": "服务名，如 nginx.service"},
            },
            "required": ["action", "unit"],
        },
        permission="superuser",
    )
    async def service_ctrl_skill(action: str, unit: str, user_id: str = "") -> str:
        if not is_superuser(user_id):
            return "无权限：仅管理员可控制服务。"
        return await service_ctrl(action, unit)

    @registry.register(
        "docker_ctrl",
        "重启/启动/停止 docker 容器（仅管理员，medium+）。任意容器可控，但"
        "数据库容器（PG_CONTAINER）与 bot 自身容器受保护。",
        {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["restart", "start", "stop"],
                    "description": "动作",
                },
                "container": {"type": "string", "description": "容器名"},
            },
            "required": ["action", "container"],
        },
        permission="superuser",
    )
    async def docker_ctrl_skill(action: str, container: str, user_id: str = "") -> str:
        if not is_superuser(user_id):
            return "无权限：仅管理员可控制容器。"
        return await docker_ctrl(action, container)

    @registry.register(
        "proc_kill",
        "终止匹配 pattern 的进程（仅管理员，**不可回退**）。两段确认：首次"
        "调用返回确认码与目标 pid 清单，带确认码二次调用才真正发送信号；"
        "拒绝 pid 1 与机器人自身；一次最多杀 10 个（pattern 配宽了会被拒）。",
        {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "进程匹配串（pgrep -f 语义，如卡死的构建进程名）",
                },
                "signal": {
                    "type": "string",
                    "enum": ["TERM", "KILL"],
                    "description": "信号，默认 TERM",
                },
                "token": {
                    "type": "string",
                    "description": "确认码（首次调用留空，二次调用带上首次返回的确认码）",
                },
            },
            "required": ["pattern"],
        },
        permission="superuser",
    )
    async def proc_kill_skill(
        pattern: str, signal: str = "TERM", token: str = "", user_id: str = ""
    ) -> str:
        if not is_superuser(user_id):
            return "无权限：仅管理员可终止进程。"
        return await proc_kill(pattern, signal, token, user_id)

    @registry.register(
        "run_build_script",
        "执行工作区 scripts/ 下的构建脚本（仅管理员，medium+）。"
        "脚本名只能选、参数受限制；内容面见模块 docstring 的 M1 边界说明。",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "脚本名（不含 .sh）"},
                "args": {
                    "type": "string",
                    "description": "可选：传给脚本的参数（禁 shell 元字符，路径限工作区内）",
                },
            },
            "required": ["name"],
        },
        permission="superuser",
    )
    async def run_build_script_skill(
        name: str, args: str = "", user_id: str = ""
    ) -> str:
        if not is_superuser(user_id):
            return "无权限：仅管理员可执行构建脚本。"
        return await run_build_script(name, args)
