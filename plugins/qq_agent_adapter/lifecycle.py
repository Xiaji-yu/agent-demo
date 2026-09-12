"""停机收尾顺序：**flush 防抖必须在关闭 memory 池之前**。

背景（H5，`review/REVIEW-a604023..679c9b3.md`）：NoneBot 的停机钩子按注册顺序
**逆序**执行（`nonebot/internal/driver/_lifespan.py` 用 `reversed(self._shutdown_funcs)`）。
此前插件钩子排在 bot.py 之后注册 → 先跑并关掉 PG 池，bot.py 的 `flush_all()` 才跑，
于是 flush 里首次 DB 调用即 `AttributeError`，被 matcher 兜底吞成 `[echo] 用户原话`，
这批消息与回复都不落库/归档（每次重启必现）。

修法**不依赖钩子顺序**：两个钩子都调用本函数，函数内部固定顺序，并且幂等
（`PgMemoryStore.aclose` 二次调用是 no-op，空防抖窗口的 flush 也是 no-op）。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

logger = logging.getLogger(__name__)


async def shutdown_agent(
    *,
    debouncer: Any = None,
    memory: Any = None,
    scheduler: Any = None,
    extra_closers: Iterable[Callable[[], Awaitable[None]]] = (),
) -> None:
    """停机收尾：停调度 → flush 防抖 → 关 memory 池 → 关闭共享连接。

    每一步都单独容错：任一步失败都不能阻断后续步骤（否则会退回到"消息丢失"
    或"连接池泄漏"）。
    """
    _stop_scheduler(scheduler)
    await _flush_debouncer(debouncer)
    await _close_memory(memory)
    for closer in extra_closers:
        try:
            await closer()
        except Exception:
            logger.exception("shutdown closer failed: %r", closer)


def _stop_scheduler(scheduler: Any) -> None:
    if scheduler is None:
        return
    try:
        scheduler.shutdown(wait=False)
    except Exception:
        logger.exception("scheduler shutdown failed")


async def _flush_debouncer(debouncer: Any) -> None:
    """把防抖窗口中未到期的消息立即处理掉——必须早于 memory.aclose()。"""
    if debouncer is None:
        return
    flush_all = getattr(debouncer, "flush_all", None)
    if flush_all is None:
        return
    try:
        await flush_all()
    except Exception:
        logger.exception("debounce flush on shutdown failed")


async def _close_memory(memory: Any) -> None:
    if memory is None or not hasattr(memory, "aclose"):
        return
    try:
        await memory.aclose()
    except Exception:
        logger.exception("memory aclose failed")
