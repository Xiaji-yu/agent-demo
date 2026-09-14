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
import math
import os
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


def _flush_timeout() -> float:
    """停机 flush 的等待上限（秒）。``<=0`` 表示不设限（退回无限等待的旧行为）。

    L3（REVIEW-679c9b3..c472e56）：flush 出去的每个窗口都要过全局 LLM 并发闸门
    （``matcher._answer``）；4 个在途回复各挂 60s 读超时时，flush 首个窗口就要排队
    数十秒——systemd 短超时下进程会被 SIGKILL，反而连已排到的 flush 都丢。
    给一个确定性的 deadline：到点放弃剩余窗口（记 ERROR），保证后续 aclose 干净执行。

    M3（REVIEW-c472e56..733f57e）：deadline 由 `Debouncer.flush_all(deadline=)`
    **原生**执行——此前用外层 `wait_for` 限时，会被 flush_all 逐窗口的
    `asyncio.shield` 吞掉取消而完全失效（已复现：0.5s 限时实等 2.10s）。
    L6（同报告）：nan/inf 等非有限值不再静默退化为 0（=不限时），按脏值回退 30s。
    """
    raw = (os.getenv("AGENT_SHUTDOWN_FLUSH_TIMEOUT") or "30").strip()
    try:
        value = float(raw)
    except ValueError:
        logger.warning("AGENT_SHUTDOWN_FLUSH_TIMEOUT=%r 不是数字，按 30s 处理", raw)
        return 30.0
    if not math.isfinite(value):
        logger.warning("AGENT_SHUTDOWN_FLUSH_TIMEOUT=%r 非有限数值，按 30s 处理", raw)
        return 30.0
    return max(0.0, value)


def _supports_deadline(func) -> bool:
    """flush_all 是否支持 deadline 形参（真实 Debouncer 支持；测试替身可能没有）。"""
    import inspect

    try:
        params = inspect.signature(func).parameters
    except (TypeError, ValueError):  # pragma: no cover - 内置/奇形签名
        return False
    if "deadline" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


async def _flush_debouncer(debouncer: Any) -> None:
    """把防抖窗口中未到期的消息立即处理掉——必须早于 memory.aclose()。"""
    if debouncer is None:
        return
    flush_all = getattr(debouncer, "flush_all", None)
    if flush_all is None:
        return
    timeout = _flush_timeout()
    try:
        if timeout > 0 and _supports_deadline(flush_all):
            result = await flush_all(deadline=timeout)
            # 兼容返回 None 的旧式替身：视为「无放弃计数」
            flushed, abandoned = result if isinstance(result, tuple) else (0, 0)
            if abandoned:
                logger.error(
                    "debounce flush on shutdown 超过 %.0fs，放弃 %d 个未完成窗口"
                    "（已执行 %d 个；在途回复占满 AGENT_MAX_CONCURRENT_TURNS 闸门时"
                    "会排队；确需等完可把 AGENT_SHUTDOWN_FLUSH_TIMEOUT 设为 0）",
                    timeout,
                    abandoned,
                    flushed,
                )
        else:
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
