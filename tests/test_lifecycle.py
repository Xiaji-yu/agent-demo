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

    async def flush_all(self):
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
        """flush 卡住（在途回复占满全局闸门的仿真）时：到点放弃并继续 aclose。

        原缺陷：flush_all 无界等待——4 个在途回复各挂 60s 读超时时，systemd
        短超时直接 SIGKILL，连已排到的 flush 都丢，后续 aclose 也执行不到。
        """
        import plugins.qq_agent_adapter.lifecycle as lc

        async def stuck_flush():
            await asyncio.sleep(60)

        closed: list[str] = []

        class FakeDebouncer:
            async def flush_all(self):
                await stuck_flush()

        class FakeMemory:
            async def aclose(self):
                closed.append("memory")

        monkeypatch.setenv("AGENT_SHUTDOWN_FLUSH_TIMEOUT", "0.1")
        with caplog.at_level(logging.ERROR, logger=lc.logger.name):
            await asyncio.wait_for(
                shutdown_agent(
                    debouncer=FakeDebouncer(), memory=FakeMemory(), scheduler=None
                ),
                timeout=5,
            )

        assert closed == ["memory"], "超时后必须继续关 memory（每步独立容错）"
        assert any("AGENT_SHUTDOWN_FLUSH_TIMEOUT" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_flush_completes_within_deadline(self, monkeypatch):
        """正常路径不受影响：deadline 内 flush 完成、消息不丢、memory 照常关闭。"""
        flushed: list[str] = []
        closed: list[str] = []

        class FakeDebouncer:
            async def flush_all(self):
                flushed.append("ok")

        class FakeMemory:
            async def aclose(self):
                closed.append("memory")

        monkeypatch.setenv("AGENT_SHUTDOWN_FLUSH_TIMEOUT", "5")
        await shutdown_agent(
            debouncer=FakeDebouncer(), memory=FakeMemory(), scheduler=None
        )
        assert flushed == ["ok"] and closed == ["memory"]

    @pytest.mark.asyncio
    async def test_zero_timeout_means_unlimited_legacy(self, monkeypatch):
        """<=0 显式退回旧行为：不设 deadline，慢 flush 也不被打断。"""
        flushed: list[str] = []
        closed: list[str] = []

        class FakeDebouncer:
            async def flush_all(self):
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


# ---------------------------------------------------------------- L5
