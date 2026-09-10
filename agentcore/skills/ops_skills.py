"""运维类 skill（**仅管理员**）：进程 / 磁盘 / 端口 / 服务 / 日志 的只读查询。

安全边界：
- 命令与参数由代码写死，LLM 只能给「数量、排序方式、端口号、服务名、日志路径」这类
  受限参数，且全部经过白名单/正则/范围校验
- 日志只能读 `AGENT_LOG_ALLOWLIST` 指定目录下的文件（默认 /var/log），纯 Python 尾部读取
- 全部只读，不提供任何 start/stop/restart/写入能力
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

from agentcore.skills.command_util import run_readonly, which

DEFAULT_LOG_ROOTS = "/var/log"
_MAX_LOG_BYTES = 256 * 1024
_MAX_LOG_LINES = 200
_UNIT_RE = re.compile(r"^[A-Za-z0-9@._:-]{1,64}$")
_PORT_RANGES = {"tcp": "/proc/net/tcp", "tcp6": "/proc/net/tcp6"}


# ---------- 进程 ----------
async def proc_detail(top: int = 10, sort: str = "cpu") -> str:
    top = max(1, min(int(top or 10), 30))
    sort = (sort or "cpu").lower()
    if sort not in {"cpu", "mem"}:
        return "错误：sort 只能是 cpu 或 mem"
    if not which("ps"):
        return "(系统没有 ps 命令)"
    key = "-pcpu" if sort == "cpu" else "-pmem"
    return await run_readonly(
        ["ps", "-eo", "pid,ppid,pcpu,pmem,etime,comm", f"--sort={key}", "--no-headers"],
        max_lines=top,
    )


# ---------- 磁盘 ----------
async def disk_usage(path: str = "") -> str:
    if not which("df"):
        return "(系统没有 df 命令)"
    if path:
        # 只允许绝对路径，且必须是已存在的目录/文件；df 本身只读
        p = Path(path)
        if not str(path).startswith("/") or ".." in str(path):
            return "错误：path 必须是不含 .. 的绝对路径"
        if not p.exists():
            return f"路径不存在：{path}"
        return await run_readonly(["df", "-h", str(p)], max_lines=6)
    return await run_readonly(["df", "-h", "-x", "tmpfs", "-x", "devtmpfs"], max_lines=25)


# ---------- 端口 ----------
def _listening_ports() -> dict[int, str]:
    """从 /proc/net/tcp(6) 读取处于 LISTEN(0A) 的端口 → 地址说明。纯 Python，无需 ss/netstat。"""
    found: dict[int, str] = {}
    for label, path in _PORT_RANGES.items():
        try:
            with open(path, encoding="utf-8") as f:
                next(f, None)  # 跳过表头
                for line in f:
                    parts = line.split()
                    if len(parts) < 4 or parts[3] != "0A":
                        continue
                    addr, port_hex = parts[1].rsplit(":", 1)
                    port = int(port_hex, 16)
                    found[port] = f"{label} {addr}"
        except (OSError, ValueError):
            continue
    return found


async def port_check(port: int) -> str:
    try:
        port = int(port)
    except (TypeError, ValueError):
        return "错误：port 必须是整数"
    if not 1 <= port <= 65535:
        return "错误：端口范围 1~65535"
    listening = await asyncio.to_thread(_listening_ports)
    if port in listening:
        return f"端口 {port} 正在监听（{listening[port]}）"
    # 未监听时给一份当前监听清单，方便排查
    sample = ", ".join(str(p) for p in sorted(listening)[:25]) or "（读不到 /proc/net/tcp）"
    return f"端口 {port} 未在监听。当前监听中的端口：{sample}"


# ---------- 服务 ----------
async def service_status(unit: str) -> str:
    unit = (unit or "").strip()
    if not _UNIT_RE.match(unit):
        return "错误：服务名含非法字符"
    if not which("systemctl"):
        return "(系统没有 systemctl)"
    active = await run_readonly(["systemctl", "is-active", unit], max_lines=2)
    detail = await run_readonly(
        ["systemctl", "status", "--no-pager", "-n", "3", unit], max_lines=12
    )
    return f"is-active: {active}\n{detail}"


# ---------- 日志 ----------
def _log_roots() -> list[Path]:
    raw = os.getenv("AGENT_LOG_ALLOWLIST", DEFAULT_LOG_ROOTS)
    roots = []
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                roots.append(Path(part).resolve())
            except OSError:
                continue
    return roots


def _tail_sync(path: Path, lines: int, keyword: str) -> str:
    size = path.stat().st_size
    with open(path, "rb") as f:
        if size > _MAX_LOG_BYTES:
            f.seek(size - _MAX_LOG_BYTES)
            f.readline()  # 丢掉可能被截断的半行
        data = f.read()
    text_lines = data.decode("utf-8", errors="replace").splitlines()
    if keyword:
        text_lines = [ln for ln in text_lines if keyword in ln]
    if not text_lines:
        return "(没有匹配的日志行)"
    tail = text_lines[-lines:]
    head = f"…（仅显示最后 {len(tail)} 行" + (f"，过滤 {keyword!r}" if keyword else "") + "）\n"
    return head + "\n".join(tail)


async def log_tail(path: str, lines: int = 50, keyword: str = "") -> str:
    lines = max(1, min(int(lines or 50), _MAX_LOG_LINES))
    if not path or ".." in path:
        return "错误：路径不合法"
    target = Path(path)
    try:
        resolved = target.resolve()
    except OSError:
        return "错误：路径无法解析"
    roots = _log_roots()
    if not roots:
        return "未配置可读日志目录（AGENT_LOG_ALLOWLIST）"
    if not any(resolved == r or r in resolved.parents for r in roots):
        return f"拒绝：只允许读取 {'、'.join(str(r) for r in roots)} 下的日志"
    if not resolved.is_file():
        return f"不是普通文件：{resolved}"
    try:
        return await asyncio.to_thread(_tail_sync, resolved, lines, keyword)
    except OSError as e:
        return f"读取失败：{e}"


def register_ops_skills(registry) -> None:
    """注册运维查询技能（仅管理员可见）。"""

    @registry.register(
        "proc_detail",
        "查看服务器占用最高的进程（仅管理员）。sort=cpu|mem，top 最多 30。",
        {
            "type": "object",
            "properties": {
                "top": {"type": "integer", "description": "返回条数，默认 10"},
                "sort": {"type": "string", "enum": ["cpu", "mem"], "description": "排序依据"},
            },
            "required": [],
        },
        permission="superuser",
    )
    async def proc_detail_skill(top: int = 10, sort: str = "cpu") -> str:
        return await proc_detail(top, sort)

    @registry.register(
        "disk_usage",
        "查看磁盘使用情况（仅管理员）。给 path 则只看该路径所在分区。",
        {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "可选：绝对路径"}},
            "required": [],
        },
        permission="superuser",
    )
    async def disk_usage_skill(path: str = "") -> str:
        return await disk_usage(path)

    @registry.register(
        "port_check",
        "检查某个端口是否在监听，并列出当前监听端口（仅管理员）。",
        {
            "type": "object",
            "properties": {"port": {"type": "integer", "description": "端口号 1~65535"}},
            "required": ["port"],
        },
        permission="superuser",
    )
    async def port_check_skill(port: int) -> str:
        return await port_check(port)

    @registry.register(
        "service_status",
        "查看 systemd 服务状态（仅管理员，只读，不能启动/停止服务）。",
        {
            "type": "object",
            "properties": {"unit": {"type": "string", "description": "服务名，如 nginx.service"}},
            "required": ["unit"],
        },
        permission="superuser",
    )
    async def service_status_skill(unit: str) -> str:
        return await service_status(unit)

    @registry.register(
        "log_tail",
        "查看服务器日志文件末尾（仅管理员）。只能读 AGENT_LOG_ALLOWLIST 指定目录下的文件"
        "（默认 /var/log），可用 keyword 过滤。",
        {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "日志文件的绝对路径"},
                "lines": {"type": "integer", "description": "查看最后多少行，默认 50，最多 200"},
                "keyword": {"type": "string", "description": "可选：只显示包含该关键字的行"},
            },
            "required": ["path"],
        },
        permission="superuser",
    )
    async def log_tail_skill(path: str, lines: int = 50, keyword: str = "") -> str:
        return await log_tail(path, lines, keyword)
