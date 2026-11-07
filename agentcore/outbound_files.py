"""最近发出的文件登记：识别协议端回传的「自己发出去的文件」（自回声）。

线上（2026-11-06/07）：协议端把 bot 经 ``upload_private_file`` 发出的文件消息
作为普通 ``message`` 事件回传，``user_id`` 却是对方而不是 bot——``post_type``
不是 ``message_sent``，关闭 ``reportSelfMessage`` 也拦不住。适配层看到的是一条
只有 ``file`` 段、没有任何文字的消息，于是当成「用户发了空消息」回了固定回执
（「没收到文字或图片」），而用户那边明明刚收到文件。

这里按 (收件人, file_id) 精确登记；协议端不返回 file_id 时退化为
(收件人, 文件名, 大小)。适配层在 TTL 内据此把回声静默丢弃；**命中即消费整条
登记**，用户之后重发同名文件仍会交给模型处理。
"""

from __future__ import annotations

import threading
import time

# 回声到达时间取决于协议端/客户端同步时机（线上实测 10+ 分钟），留足窗口。
_TTL_SECONDS = 6 * 3600.0
_MAX_ENTRIES = 500

# 单进程内由事件循环写入/读取；加锁只为防御未来在线程池里调用。
_lock = threading.Lock()
# 每条 = (target, file_id, name, size, ts)；一次投递只占一条，命中即整条删除。
_entries: list[tuple[str, str, str, int, float]] = []
_clock = time.monotonic  # 测试可替换


def _norm_size(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _sweep(now: float) -> None:
    if not _entries:
        return
    _entries[:] = [e for e in _entries if now - e[4] <= _TTL_SECONDS]


def note_sent_file(
    target: str, *, file_id: str = "", name: str = "", size: int = 0
) -> None:
    """登记一次成功的文件投递；``target`` 是收件人 QQ 号。"""
    target = str(target or "").strip()
    if not target:
        return
    file_id = str(file_id or "").strip()
    name = str(name or "").strip()
    size = _norm_size(size)
    if not file_id and not name:
        return
    now = _clock()
    with _lock:
        _sweep(now)
        _entries.append((target, file_id, name, size, now))
        # 只保留最近的 N 条（从最旧的丢），防长期运行无界增长
        if len(_entries) > _MAX_ENTRIES:
            del _entries[: len(_entries) - _MAX_ENTRIES]


def is_recent_sent_file(
    target: str,
    *,
    file_id: str = "",
    name: str = "",
    size: int = 0,
    consume: bool = True,
) -> bool:
    """是否是我们刚发出、正被协议端回传的文件。默认命中即消费该登记。"""
    target = str(target or "").strip()
    if not target:
        return False
    file_id = str(file_id or "").strip()
    name = str(name or "").strip()
    size = _norm_size(size)
    now = _clock()
    with _lock:
        _sweep(now)
        for index, entry in enumerate(_entries):
            entry_target, entry_id, entry_name, entry_size, _ts = entry
            if entry_target != target:
                continue
            matched = False
            if file_id and entry_id and file_id == entry_id:
                matched = True
            elif name and entry_name and name == entry_name:
                # 回声没带 file_size 时按文件名匹配（不区分大小）
                matched = size == 0 or size == entry_size
            if matched:
                if consume:
                    del _entries[index]
                return True
    return False


def reset() -> None:
    """测试用：清空登记。"""
    with _lock:
        _entries.clear()
