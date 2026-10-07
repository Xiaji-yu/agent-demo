"""Agent 权限级别（``AGENT_PERMISSION_LEVEL``）：low / medium / high。

设计（2026-10 管理员决策，替代原「目标白名单」制）：

- 级别只调节 **SUPERUSERS** 的能力上限；普通用户在任何级别下都看不到
  服务器操作类技能（schema 过滤 + handler 二次校验，见 FIX-*.md P0）。
  ``SUPERUSERS`` 为空 = 无人可用——fail-closed 由身份轴承担，级别缺省取
  medium（日常档）不会引入未授权可达面。
- low    = 纯只读：日志/进程/磁盘/端口/服务状态/容器查询/工作区读/
           run_command 只读子集（拒绝 zip/unzip/tar/curl）/ssh_run
- medium = 日常档：low 全部 + 工作区写删（删除保留确认码）+ run_command
           全白名单 + 服务/容器 restart|start|stop（自保护：拒 bot 自身
           unit 与 DB 容器）+ 杀进程（两段确认）+ 构建脚本 + db_query
- high   = medium 全部 + run_shell（bash -c 任意命令）

读取纪律与引擎/rag 同款：调用时读 env（改后重启生效），脏值告警后回退
默认；同一脏值只告警一次，避免每次工具调用刷屏。
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

LEVELS: dict[str, int] = {"low": 0, "medium": 1, "high": 2}
DEFAULT_LEVEL = "medium"

LEVEL_ENV = "AGENT_PERMISSION_LEVEL"

# 已告警过的脏值：同一值只 WARNING 一次（级别在每次工具调用都会读）
_warned_bad_values: set[str] = set()


def normalize_level(raw: str | None) -> str | None:
    """归一化级别字符串；非法返回 None（不做回退决策，交给调用方）。"""
    text = (raw or "").strip().lower()
    return text if text in LEVELS else None


def current_level() -> str:
    """当前生效级别：env 缺省/脏值回退 DEFAULT_LEVEL（脏值告警一次）。"""
    raw = os.getenv(LEVEL_ENV) or ""
    level = normalize_level(raw)
    if level:
        return level
    if raw.strip():
        key = raw.strip()
        if key not in _warned_bad_values:
            _warned_bad_values.add(key)
            logger.warning(
                "%s=%r 不是 low/medium/high，回退 %s（改后重启生效）",
                LEVEL_ENV,
                raw,
                DEFAULT_LEVEL,
            )
    return DEFAULT_LEVEL


def at_least(level: str) -> bool:
    """当前级别是否达到指定档位（low ≤ medium ≤ high）。"""
    if level not in LEVELS:
        raise ValueError(f"未知级别：{level!r}（合法：{sorted(LEVELS)}）")
    return LEVELS[current_level()] >= LEVELS[level]
