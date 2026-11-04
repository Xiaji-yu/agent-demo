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
        # 首建必须 0600：`open(..., "a")` 走 umask 先落 0644，对同机其它本地用户
        # 可读——文件里有 target（真实 QQ/群号）与投递计数（M12，2026-11 评审）。
        # 与 _rewrite / notify_budget 的 open_part_0600 同口径：创建即收紧，
        # 而不是事后补 chmod（那一跳已经暴露过）。
        _append_line_0600(path, line)
        _truncate(path)
    except OSError:
        logger.warning("outbox: 投递日志写入失败（%s）", path, exc_info=True)


def _append_line_0600(path: Path, line: str) -> None:
    """追加一行，文件不存在时以 0600 创建（已存在则沿用原权限）。"""
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW  # 拒符号链接：别把记账写进别人指向的文件
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def peek_uncertain() -> list[dict]:
    """读出（**不删除**）所有 uncertain 行。

    M14（2026-11 评审）的两阶段第一步：先读后删。旧流程是「读走即删、再通知」，
    通知一旦失败（协议端抖动/无 bot），悬空账目就永久丢了——而它存在的全部意义
    就是让人知道「有条投递结果不确定」。
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
    for line in raw.splitlines():
        entry = _parse(line)
        if entry is not None and entry.get("outcome") == OUTCOME_UNCERTAIN:
            uncertain.append(entry)
    return uncertain


def drop_uncertain(entries: list[dict] | None = None) -> int:
    """删除 uncertain 行，返回删除条数（0 = 无可删/读失败；写失败原样上抛）。

    ``entries``（M6，REVIEW-6867995..e079b8d）给定时只删**通知过的那一批**：
    peek 与 drop 之间隔着逐管理员跨 await 的发送（秒级窗口），窗口内新落账的
    uncertain 行不在通知文本里——全删会把「从没人知道的悬空投递」连账销掉，
    正是 M14 要防的蒸发换了个入口复发。传 None 删全部（旧语义，仅
    ``take_uncertain`` 的内部路径使用）。
    """
    path = journal_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0
    except (OSError, UnicodeDecodeError):
        return 0
    snapshot = (
        {json.dumps(e, sort_keys=True, ensure_ascii=False) for e in entries}
        if entries is not None
        else None
    )
    uncertain = 0
    kept: list[str] = []
    for line in raw.splitlines():
        entry = _parse(line)
        if entry is None:
            continue
        if entry.get("outcome") != OUTCOME_UNCERTAIN:
            kept.append(line)
            continue
        if snapshot is not None and (
            json.dumps(entry, sort_keys=True, ensure_ascii=False) not in snapshot
        ):
            kept.append(line)  # 窗口内新落账：不在通知里，保留待下次启动认账
            continue
        uncertain += 1
    if not uncertain:
        return 0
    _rewrite(path, kept)
    return uncertain


def take_uncertain() -> list[dict]:
    """读出并**原子删除**所有 uncertain 行（一次性交接）。

    语义同 ``reboot.consume_reboot_notice``。启动通知路径请优先用
    ``peek_uncertain`` + 发送成功后再 ``drop_uncertain`` 的两阶段（见 M14），
    本方法保留给「读走即消费」的既有调用方与测试。
    """
    entries = peek_uncertain()
    if entries:
        drop_uncertain()
    return entries


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
