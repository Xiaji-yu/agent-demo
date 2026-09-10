"""消息防抖：同一会话的连续消息在静默窗口内合并，到期后一次性处理。

- key 由调用方决定（如同会话同用户）
- push 合并累积并重置计时；窗口内无新消息时后台执行 runner(parts)
- runner 异常只记日志，不影响后续消息
- 同 key 的 runner 串行执行：LLM 长延迟下不会并发跑两次引擎、不会乱序回复，
  窗口边界到达的消息也不会把「半句+补充」拆成两次调用
- flush_all 供停机前把未到期窗口立即执行，避免消息静默丢失
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


class Debouncer:
    def __init__(self, delay: float):
        self.delay = max(0.0, float(delay))
        self._pending: dict[str, dict] = {}
        self._key_locks: dict[str, asyncio.Lock] = {}
        self._lock = asyncio.Lock()

    async def push(
        self,
        key: str,
        part: object,
        runner: Callable[[list[object]], Awaitable[None]],
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
                entry = {"parts": [part], "runner": runner, "task": None}
                self._pending[key] = entry
            entry["task"] = asyncio.create_task(self._job(key, runner))

    async def _job(self, key: str, runner) -> None:
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            return  # 窗口内又有新消息，由新 job 处理
        # pop 在锁内完成：pop 之后到达的新消息会开新窗口（并入 key 锁排队），
        # 而不是把本批 parts 拆成两次并发执行
        async with self._lock:
            entry = self._pending.pop(key, None)
        if not entry or not entry["parts"]:
            return
        await self._run_parts(key, runner, list(entry["parts"]))

    async def _run_parts(self, key: str, runner, parts: list) -> None:
        """同 key 串行执行 runner（防抖 × LLM 长延迟竞态的收口）。"""
        lock = self._key_locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                await runner(parts)
        except Exception:
            logger.exception("debounce runner failed for %s", key)
        finally:
            async with self._lock:
                if not lock.locked():
                    self._key_locks.pop(key, None)

    def pending_keys(self) -> list[str]:
        return list(self._pending.keys())

    async def cancel_all(self) -> None:
        async with self._lock:
            for entry in self._pending.values():
                entry["task"].cancel()
            self._pending.clear()

    async def flush_all(self) -> None:
        """立即执行所有待处理窗口（停机前调用，防未到期消息静默丢失）。"""
        async with self._lock:
            entries = [(k, self._pending.pop(k)) for k in list(self._pending)]
        for key, entry in entries:
            if entry["parts"]:
                await self._run_parts(key, entry["runner"], list(entry["parts"]))
