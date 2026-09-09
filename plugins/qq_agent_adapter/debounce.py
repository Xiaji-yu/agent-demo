"""消息防抖：同一会话的连续消息在静默窗口内合并，到期后一次性处理。

- key 由调用方决定（如同会话同用户）
- push 合并累积并重置计时；窗口内无新消息时后台执行 runner(parts)
- runner 异常只记日志，不影响后续消息
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, List

logger = logging.getLogger(__name__)


class Debouncer:
    def __init__(self, delay: float):
        self.delay = max(0.0, float(delay))
        self._pending: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    async def push(
        self,
        key: str,
        part: object,
        runner: Callable[[List[object]], Awaitable[None]],
    ) -> None:
        """登记一条消息（part）。窗口内合并；窗口到期后台执行 runner(parts)。"""
        if self.delay <= 0:
            await runner([part])
            return
        async with self._lock:
            entry = self._pending.get(key)
            if entry:
                entry["task"].cancel()
                entry["parts"].append(part)
            else:
                entry = {"parts": [part], "task": None}
                self._pending[key] = entry
            entry["task"] = asyncio.create_task(self._job(key, runner))

    async def _job(self, key: str, runner) -> None:
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            return  # 窗口内又有新消息，由新 job 处理
        entry = self._pending.pop(key, None)
        if not entry or not entry["parts"]:
            return
        try:
            await runner(list(entry["parts"]))
        except Exception:
            logger.exception("debounce runner failed for %s", key)

    def pending_keys(self) -> list[str]:
        return list(self._pending.keys())

    async def cancel_all(self) -> None:
        async with self._lock:
            for entry in self._pending.values():
                entry["task"].cancel()
            self._pending.clear()
