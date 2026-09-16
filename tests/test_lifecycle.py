"""停机生命周期：顺序固定（scheduler → flush → aclose）、幂等、flush 有 deadline。

回归来源：REVIEW-a604023..679c9b3 H5（test_review_h_fixes，已并入）与
REVIEW-679c9b3..c472e56 L3（test_review_5_fixes，已并入）。
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from plugins.qq_agent_adapter.lifecycle import shutdown_agent


# 来源: REVIEW-a604023..679c9b3 H5 TestShutdownOrder（含 _Recorder）
class _Recorder:
    def __init__(self, name, order, fail=False):
        self.name = name
        self.order = order
        self.fail = fail

    def shutdown(self, wait=False):
        self.order.append(f"{self.name}:stop")
        if self.fail:
            raise RuntimeError(f"{self.name} stop failed")

    async def flush_all(self, deadline=None):
        self.order.append(f"{self.name}:flush")
        if self.fail:
            raise RuntimeError(f"{self.name} flush failed")

    async def aclose(self):
        self.order.append(f"{self.name}:aclose")
        if self.fail:
            raise RuntimeError(f"{self.name} aclose failed")


class TestShutdownOrder:
    @pytest.mark.asyncio
    async def test_flush_before_aclose(self):
        from plugins.qq_agent_adapter.lifecycle import shutdown_agent

        order: list[str] = []
        await shutdown_agent(
            debouncer=_Recorder("deb", order),
            memory=_Recorder("mem", order),
            scheduler=_Recorder("sched", order),
        )
        assert order == ["sched:stop", "deb:flush", "mem:aclose"]

    @pytest.mark.asyncio
    async def test_later_steps_survive_earlier_failure(self):
        from plugins.qq_agent_adapter.lifecycle import shutdown_agent

        order: list[str] = []
        closed: list[str] = []

        async def closer():
            closed.append("extra")

        await shutdown_agent(
            debouncer=_Recorder("deb", order, fail=True),  # flush 抛错
            memory=_Recorder("mem", order, fail=True),  # aclose 也抛错
            scheduler=_Recorder("sched", order, fail=True),
            extra_closers=(closer,),
        )
        assert order == ["sched:stop", "deb:flush", "mem:aclose"]
        assert closed == ["extra"], "单步失败不得阻断后续收尾"

    @pytest.mark.asyncio
    async def test_idempotent_second_call(self):
        from plugins.qq_agent_adapter.lifecycle import shutdown_agent

        order: list[str] = []
        deb, mem = _Recorder("deb", order), _Recorder("mem", order)
        await shutdown_agent(debouncer=deb, memory=mem)
        await shutdown_agent(debouncer=deb, memory=mem)
        assert order == ["deb:flush", "mem:aclose", "deb:flush", "mem:aclose"]

    @pytest.mark.asyncio
    async def test_none_objects_are_noop(self):
        from plugins.qq_agent_adapter.lifecycle import shutdown_agent

        await shutdown_agent()  # 不应抛错


# 来源: REVIEW-679c9b3..c472e56 L3 TestShutdownFlushDeadline
class TestShutdownFlushDeadline:
    @pytest.mark.asyncio
    async def test_flush_timeout_is_bounded_and_shutdown_continues(
        self, monkeypatch, caplog
    ):
        """到点放弃（native deadline）并继续 aclose。

        原缺陷：flush_all 无界等待——在途回复占满全局闸门时 systemd 短超时
        直接 SIGKILL。M3（REVIEW-c472e56..733f57e）起 deadline 由 flush_all
        原生执行并返回放弃计数，lifecycle 对放弃的窗口记 ERROR。
        """
        import plugins.qq_agent_adapter.lifecycle as lc

        closed: list[str] = []

        class FakeDebouncer:
            async def flush_all(self, deadline=None):
                assert deadline is not None, "deadline 必须原生传入 flush_all"
                return 1, 2  # 执行 1 个窗口、到点放弃 2 个

        class FakeMemory:
            async def aclose(self):
                closed.append("memory")

        monkeypatch.setenv("AGENT_SHUTDOWN_FLUSH_TIMEOUT", "0.1")
        with caplog.at_level(logging.ERROR, logger=lc.logger.name):
            await shutdown_agent(
                debouncer=FakeDebouncer(), memory=FakeMemory(), scheduler=None
            )

        assert closed == ["memory"], "到点放弃后必须继续关 memory（每步独立容错）"
        assert any("放弃 2 个未完成窗口" in r.message for r in caplog.records)
        assert any("AGENT_SHUTDOWN_FLUSH_TIMEOUT" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_flush_completes_within_deadline(self, monkeypatch):
        """正常路径不受影响：deadline 内 flush 完成、消息不丢、memory 照常关闭。"""
        flushed: list[str] = []
        closed: list[str] = []

        class FakeDebouncer:
            async def flush_all(self, deadline=None):
                flushed.append(deadline)

        class FakeMemory:
            async def aclose(self):
                closed.append("memory")

        monkeypatch.setenv("AGENT_SHUTDOWN_FLUSH_TIMEOUT", "5")
        await shutdown_agent(
            debouncer=FakeDebouncer(), memory=FakeMemory(), scheduler=None
        )
        assert flushed == [5.0] and closed == ["memory"]

    @pytest.mark.asyncio
    async def test_zero_timeout_means_unlimited_legacy(self, monkeypatch):
        """<=0 显式退回旧行为：不设 deadline，慢 flush 也不被打断。"""
        flushed: list[str] = []
        closed: list[str] = []

        class FakeDebouncer:
            async def flush_all(self, deadline=None):
                assert deadline is None
                await asyncio.sleep(0.2)
                flushed.append("ok")

        class FakeMemory:
            async def aclose(self):
                closed.append("memory")

        monkeypatch.setenv("AGENT_SHUTDOWN_FLUSH_TIMEOUT", "0")
        await shutdown_agent(
            debouncer=FakeDebouncer(), memory=FakeMemory(), scheduler=None
        )
        assert flushed == ["ok"] and closed == ["memory"]

    def test_invalid_timeout_falls_back_to_30(self, monkeypatch):
        from plugins.qq_agent_adapter.lifecycle import _flush_timeout

        monkeypatch.setenv("AGENT_SHUTDOWN_FLUSH_TIMEOUT", "abc")
        assert _flush_timeout() == 30.0
        monkeypatch.setenv("AGENT_SHUTDOWN_FLUSH_TIMEOUT", "-3")
        assert _flush_timeout() == 0.0, "负值 = 不设限"

    @pytest.mark.parametrize("dirty", ["nan", "inf", "-inf"])
    def test_non_finite_timeout_falls_back_to_30(self, monkeypatch, dirty):
        """L6（来源: REVIEW-c472e56..733f57e）：nan/inf 不再静默退化为 0（不限时）。"""
        from plugins.qq_agent_adapter.lifecycle import _flush_timeout

        monkeypatch.setenv("AGENT_SHUTDOWN_FLUSH_TIMEOUT", dirty)
        assert _flush_timeout() == 30.0

    @pytest.mark.asyncio
    async def test_shutdown_deadline_bounds_real_debouncer_flush(
        self, monkeypatch, caplog
    ):
        """M3 回归（**真实 Debouncer**）：deadline 必须真正约束 flush 总时长。

        旧实现用外层 wait_for 限时，被 flush_all 逐窗口的 asyncio.shield 吞掉
        取消——0.5s 限时实等全部窗口（已复现 2.10s），TimeoutError 分支不可达、
        旧 FakeDebouncer（裸 sleep 可被 cancel）测试假通过。
        """
        import time

        from plugins.qq_agent_adapter.debounce import Debouncer

        done: list[int] = []

        async def runner(parts):
            await asyncio.sleep(0.4)
            done.append(len(parts))

        d = Debouncer(delay=60, max_parts=20)
        for i in range(6):
            await d.push(f"sess-{i}", f"m{i}", runner)

        monkeypatch.setenv("AGENT_SHUTDOWN_FLUSH_TIMEOUT", "0.5")
        import plugins.qq_agent_adapter.lifecycle as lc

        t0 = time.monotonic()
        with caplog.at_level(logging.ERROR, logger=lc.logger.name):
            await shutdown_agent(debouncer=d, memory=None, scheduler=None)
        elapsed = time.monotonic() - t0

        assert elapsed < 2.0, (
            f"deadline 必须约束总等待（旧实现实等 {elapsed:.2f}s 跑完全部窗口）"
        )
        assert len(done) <= 2, "deadline 到点后不得继续执行后续窗口"
        assert any("放弃" in r.message for r in caplog.records)
        assert d.pending_keys() == [], "窗口已出队（放弃即明确丢弃，不再滞留）"


# ---------------------------------------------------------------- L5


# ------------------------------------------------- 评审 M3：PG 回退池关闭
class TestInitMemoryFallback:
    """_init_memory：PG init 失败回退 InMemory 前必须关闭已建成的池。

    评审 M3（REVIEW-46c85d1..6ec3f7c）：旧实现直接丢弃失败的 PgMemoryStore，
    其 asyncpg 池从未 aclose → 泄漏连接伴随进程终生。
    """

    @pytest.mark.asyncio
    async def test_pg_failure_closes_pool_before_fallback(self, monkeypatch):
        from agentcore.memory.store import InMemoryMemoryStore
        from plugins.qq_agent_adapter import _init_memory

        closed = []

        class FakePg:
            def __init__(self, *a, **kw):
                self.pool = object()  # init 前或中途已建成池

            async def init(self):
                raise RuntimeError("ddl boom")

            async def aclose(self):
                closed.append(1)
                self.pool = None

        monkeypatch.setattr("agentcore.memory.store.PgMemoryStore", FakePg)
        memory = await _init_memory("postgresql://x", dim=64)
        assert isinstance(memory, InMemoryMemoryStore)
        assert closed == [1], "回退前必须 aclose 失败的 PG 存储"

    @pytest.mark.asyncio
    async def test_pg_init_success_returns_pg(self, monkeypatch):
        from plugins.qq_agent_adapter import _init_memory

        class FakePg:
            def __init__(self, *a, **kw):
                pass

            async def init(self):
                pass

        monkeypatch.setattr("agentcore.memory.store.PgMemoryStore", FakePg)
        memory = await _init_memory("postgresql://x", dim=64)
        assert isinstance(memory, FakePg)

    @pytest.mark.asyncio
    async def test_no_db_url_returns_inmemory(self):
        from agentcore.memory.store import InMemoryMemoryStore
        from plugins.qq_agent_adapter import _init_memory

        memory = await _init_memory("", dim=64)
        assert isinstance(memory, InMemoryMemoryStore)

    @pytest.mark.asyncio
    async def test_aclose_failure_does_not_block_fallback(self, monkeypatch):
        """aclose 自身失败也不能挡住回退（降级路径永不抛）。"""
        from agentcore.memory.store import InMemoryMemoryStore
        from plugins.qq_agent_adapter import _init_memory

        class FakePg:
            def __init__(self, *a, **kw):
                self.pool = object()

            async def init(self):
                raise RuntimeError("ddl boom")

            async def aclose(self):
                raise RuntimeError("aclose boom")

        monkeypatch.setattr("agentcore.memory.store.PgMemoryStore", FakePg)
        memory = await _init_memory("postgresql://x", dim=64)
        assert isinstance(memory, InMemoryMemoryStore)
