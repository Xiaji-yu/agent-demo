"""出站投递日志（send journal）：把 Sink 的三态结果落盘，跨重启可查。

由来（对照 dsh-superagent 的 outbox/receipts）：那边出站有持久队列与回执，
uncertain 可以事后再认账。本仓的出站铁律是「结果不确定绝不重发」（重发 =
用户收两遍），所以**刻意不做持久发送队列**——这里只做记账：每条投递的结果
一行 JSONL，让三件事在进程重启后仍然成立：

1. 运维能回答「刚才那次提醒到底发出去了吗」——uncertain/failed 不再只活在
   滚动日志里，重启即无从查证；
2. 新进程启动后能拿到一份「悬空投递」清单（``take_uncertain``）通知主人，
   而不是让它在日志里被淹没；
3. 交付审计：定时推送/提醒是否按时送出，有据可查。

内容纪律（对齐 ``agentcore/diagnostics.py``「只存字段不存正文」）：每行只记
target / outcome / 时间 / 原因类型 / ref（调用方给的业务标识，如 reminder id、
push job 名、cron job id），**绝不存消息正文**——``data/`` 是运行期产物，
公开仓库脱敏纪律要求真实会话内容不进任何可备份文件。

绝不做重发：本模块不 import bot、不提供任何发送能力。任何「读出来再发一遍」
的做法都会把 uncertain 变成重复投递（AGENTS.md 出站铁律）。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

ENV_FILE = "AGENT_OUTBOX_FILE"
DEFAULT_FILE = "data/state/outbox.jsonl"
# 行数上限：超了从头截断、保最新。uncertain 行在下次启动即被 take_uncertain
# 认账消费，不会被历史行挤掉；截的是「已认账」的旧账。
MAX_LINES = 500

OUTCOME_SENT = "sent"
OUTCOME_FAILED = "failed"
OUTCOME_UNCERTAIN = "uncertain"

_DETAIL_MAX = 200


def journal_path() -> Path:
    """日志文件路径（env 覆盖；默认 ``data/state/outbox.jsonl``）。

    与 reboot 回执同款约定：默认落在 ``data/state/``（compose 唯一持久化的
    data 子目录，见 compose_init 的 STATE_REDIRECTS），裸机部署由
    ``record`` 首次写入时建目录。
    """
    raw = (os.getenv(ENV_FILE) or "").strip()
    return Path(raw) if raw else Path(DEFAULT_FILE)


def record(target: str, outcome: str, *, ref: str = "", detail: str = "") -> None:
    """追加一行投递结果。任何失败只记日志——记账是增益，不能反噬投递本身。"""
    path = journal_path()
    line = json.dumps(
        {
            "ts": round(time.time(), 3),
            "target": str(target or ""),
            "outcome": str(outcome or ""),
            "ref": str(ref or "")[:64],
            "detail": str(detail or "")[:_DETAIL_MAX],
        },
        ensure_ascii=False,
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        _truncate(path)
    except OSError:
        logger.warning("outbox: 投递日志写入失败（%s）", path, exc_info=True)


def take_uncertain() -> list[dict]:
    """读出并**原子删除**所有 uncertain 行（一次性交接，供启动后通知）。

    语义同 ``reboot.consume_reboot_notice``：读失败**不删**（保留到下次启动
    再认账，比丢账好）；坏行直接丢弃——它不占 MAX_LINES，也不该阻断其余认账。
    """
    path = journal_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError):
        logger.warning("outbox: 投递日志读取失败（保留待下次启动认账）：%s", path)
        return []
    uncertain: list[dict] = []
    kept: list[str] = []
    for line in raw.splitlines():
        entry = _parse(line)
        if entry is None:
            continue
        if entry.get("outcome") == OUTCOME_UNCERTAIN:
            uncertain.append(entry)
        else:
            kept.append(line)
    if not uncertain:
        return []
    _rewrite(path, kept)
    return uncertain


def recent(limit: int = 50) -> list[dict]:
    """最近 N 行（非正 → 空）。只读，不消费、不改文件。"""
    if limit <= 0:
        return []
    path = journal_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    out: list[dict] = []
    for line in raw.splitlines()[-limit:]:
        entry = _parse(line)
        if entry is not None:
            out.append(entry)
    return out


def _parse(line: str) -> dict | None:
    try:
        data = json.loads(line)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _rewrite(path: Path, lines: list[str]) -> None:
    """原子重写（tmp 创建即 0600 + replace）：半截文件比旧文件更糟。"""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")
    os.replace(tmp, path)


def _truncate(path: Path) -> None:
    """超 MAX_LINES 从头截断。读失败/坏行不阻断本次记账。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return
    if len(lines) <= MAX_LINES:
        return
    _rewrite(path, lines[-MAX_LINES:])
