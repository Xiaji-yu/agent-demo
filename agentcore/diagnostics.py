"""进程内诊断事件环形缓冲：最近发生了什么（告警 / 主备切换 / 降级）。

为什么不用读日志文件：日志是给人看的文本，解析脆、还要防把用户内容带出去；
这里存**结构化摘要**（时间 + 类型 + 少量无内容字段），定长淘汰，进程重启即丢。
Web 总览与 ``/status`` 共用它，避免每个出口各写一套"最近事件"。

纪律（与仓库 M4「日志/测试不携带用户内容」同源）：只存字段、**不存正文**——
调用方不得把消息文本、引用/转发内容、歌名等用户数据塞进 fields。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

_MAX_EVENTS = 50
_lock = threading.Lock()
_events: deque[dict[str, Any]] = deque(maxlen=_MAX_EVENTS)


def record(kind: str, **fields: Any) -> None:
    """记一条诊断事件。``kind`` 用稳定小写标识（如 ``llm_fallback``）。

    字段值只允许标量与短字符串；调用方自行保证不含用户内容（见模块 docstring）。
    """
    event: dict[str, Any] = {"ts": time.time(), "kind": str(kind)}
    event.update(fields)
    with _lock:
        _events.append(event)


def recent(limit: int = 20) -> list[dict[str, Any]]:
    """最近的事件，**最新在前**；超出 ``_MAX_EVENTS`` 的旧事件已被淘汰。"""
    with _lock:
        items = list(_events)
    if limit <= 0:
        return []
    return list(reversed(items))[:limit]


def clear() -> None:
    """清空（测试用；线上无此需求——重启进程同样有效）。"""
    with _lock:
        _events.clear()
