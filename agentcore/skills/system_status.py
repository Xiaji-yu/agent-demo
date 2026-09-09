"""主机状态查询 skill：只读、白名单命令，供 LLM 回答「机器/服务器状态」类问题。

安全边界：
- 只允许预先定义好的只读命令，LLM 不能传任意命令
- 每条命令带超时，输出长度受限
- 需要外部程序（nvidia-smi/docker）不存在时自动跳过该项
"""
from __future__ import annotations

import asyncio
import os
import shutil
from typing import List

from agentcore.skills.registry import SkillRegistry

_CMD_TIMEOUT = 5
_MAX_LINES = 12


def _which(name: str) -> bool:
    return shutil.which(name) is not None


async def _run(cmd: List[str]) -> str:
    """执行只读命令并返回输出；失败/超时返回占位说明。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=_CMD_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return "(命令执行超时)"
        text = (out or b"").decode("utf-8", errors="replace").strip()
        lines = text.splitlines()
        if len(lines) > _MAX_LINES:
            text = "\n".join(lines[:_MAX_LINES]) + f"\n…（仅显示前 {_MAX_LINES} 行）"
        return text or "(无输出)"
    except FileNotFoundError:
        return f"(命令不存在: {' '.join(cmd)})"
    except Exception as e:
        return f"(执行失败: {e})"


_ITEMS = {
    "os": lambda: _run(["uname", "-a"]),
    "cpu": lambda: _run(["uptime"]),
    "mem": lambda: _run(["free", "-h"]),
    "disk": lambda: _run(["df", "-h", "/"]),
    "proc": lambda: _run(
        ["ps", "-eo", "pcpu,pmem,comm", "--sort=-pcpu", "--no-headers"]
    ),
    "gpu": lambda: _run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu", "--format=csv,noheader"]),
    "docker": lambda: _run(["docker", "ps", "--format", "table {{.Names}}\t{{.Status}}\t{{.Image}}"]),
}

_ITEM_LABELS = {
    "os": "系统",
    "cpu": "CPU/负载",
    "mem": "内存",
    "disk": "磁盘",
    "proc": "进程 Top",
    "gpu": "GPU",
    "docker": "容器",
}

# 缺外部程序时自动跳过的项
_OPTIONAL = {"gpu": "nvidia-smi", "docker": "docker"}


def _resolve_items(requested: str) -> List[str]:
    want = [x.strip().lower() for x in (requested or "").split(",") if x.strip()]
    if not want or "all" in want:
        return list(_ITEMS.keys())
    return [k for k in _ITEMS if k in want]


async def run_system_status(requested: str = "all") -> str:
    """查询主机只读状态，requested 为逗号分隔项：os,cpu,mem,disk,proc,gpu,docker 或 all。"""
    items = _resolve_items(requested)
    if not items:
        return "支持的查询项：os,cpu,mem,disk,proc,gpu,docker（可逗号分隔）"

    results = []
    for key in items:
        if key in _OPTIONAL and not _which(_OPTIONAL[key]):
            continue
        out = await _ITEMS[key]()
        results.append(f"## {_ITEM_LABELS.get(key, key)}\n{out}")
    if not results:
        return "（没有可查询的系统信息，或所需命令缺失）"
    return "==== 主机状态 ====\n" + "\n\n".join(results)


def register_system_skills(registry: SkillRegistry) -> None:
    @registry.register(
        "system_status",
        "查询 agent 运行所在 Linux 主机/服务器的只读状态：CPU 负载、内存、磁盘、系统信息、Top 进程、GPU（nvidia-smi）、Docker 容器。当用户问'机器/服务器/本机状态''CPU 内存占用'等时调用。参数 items 用逗号分隔，如 os,cpu,mem,disk,proc,gpu,docker，默认 all。",
        {
            "type": "object",
            "properties": {
                "items": {
                    "type": "string",
                    "description": "要查询的项目，逗号分隔：os,cpu,mem,disk,proc,gpu,docker；缺省或 all 表示全部",
                }
            },
            "required": [],
        },
        permission="public",
    )
    async def system_status_skill(items: str = "all") -> str:
        return await run_system_status(items)
