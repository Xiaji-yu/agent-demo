"""REVIEW-679c9b3..c472e56（第五批）修复的回归测试。

每条对应报告的一个编号：M1 debounce 崩溃 / L3 停机 flush 无界等待 /
L4 后台任务无引用 / L5 zip 等号误伤。写法沿用本仓库约定：
先在修复前复现过缺陷，再锁定修复后的行为（变异复核记录见 FIX 文档）。
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from plugins.qq_agent_adapter.debounce import Debouncer
from plugins.qq_agent_adapter.lifecycle import shutdown_agent


# ---------------------------------------------------------------- M1
class TestDebounceMaxPartsOne:
    @pytest.mark.asyncio
    async def test_max_parts_one_does_not_crash_and_delivers(self):
        """max_parts=1 是文档化合法配置（每条消息独立请求）。

        原缺陷：burst 分支对新建 entry 的 task=None 调 cancel() 抛 AttributeError，
        且抛在 pop 之前 → entry 残留 _pending → 该会话此后每条消息都炸（runner 0 次）。
        """
        deb = Debouncer(delay=30, max_parts=1)
        seen: list[list] = []
        first = asyncio.Event()
        second = asyncio.Event()

        async def runner(parts):
            seen.append(list(parts))
            (first if len(seen) == 1 else second).set()

        await deb.push("k", "消息1", runner)  # 修复前：AttributeError 就在这
        await asyncio.wait_for(first.wait(), timeout=2)
        await deb.push("k", "消息2", runner)  # 修复前：残留 entry 让第二条同样炸
        await asyncio.wait_for(second.wait(), timeout=2)

        assert seen == [["消息1"], ["消息2"]], "每条消息独立成批送达"
        assert deb.pending_keys() == [], "结算后不得残留窗口"

    @pytest.mark.asyncio
    async def test_max_parts_one_sequential_messages_all_delivered(self):
        """连发 5 条、每条都达到上限：5 批全部送达（不丢、不并）。"""
        deb = Debouncer(delay=30, max_parts=1)
        seen: list[list] = []

        async def runner(parts):
            seen.append(list(parts))

        for i in range(5):
            await deb.push("k", i, runner)
        await asyncio.sleep(0.05)  # burst 结算在后台跑，让出事件循环
        assert seen == [[0], [1], [2], [3], [4]]
        assert deb.pending_keys() == []


# ---------------------------------------------------------------- L4
class TestBurstTaskKeepsReference:
    @pytest.mark.asyncio
    async def test_background_tasks_tracked_and_released(self):
        """突发结算任务必须被强引用（asyncio 只持弱引用），完成后自动移除。"""
        deb = Debouncer(delay=30, max_parts=2)
        done = asyncio.Event()

        async def runner(parts):
            done.set()

        await deb.push("k", 1, runner)
        await deb.push("k", 2, runner)  # 达到上限 → burst 后台结算
        # 精确断言 **burst 位点**（_run_parts）的任务被持引用——窗口任务
        # entry["task"] 本来就有引用，若只查集合非空会把它掩盖掉
        assert any(t.get_name().startswith("debounce-burst:") for t in deb._bg_tasks), (
            f"burst 结算任务应被持引用，实际：{[t.get_name() for t in deb._bg_tasks]}"
        )
        await asyncio.wait_for(done.wait(), timeout=2)
        await asyncio.sleep(0)  # 让 done_callback 跑完
        assert deb._bg_tasks == set(), "任务完成后应从引用集合移除"


# ---------------------------------------------------------------- L3
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
class TestZipEqualsSignScope:
    def test_operand_filename_with_equals_allowed(self):
        """L5：`=` 只在开关上禁止；文件名操作数含 `=` 是合法的。

        原缺陷：无差别拒 `=` 误伤 `report_v=2.zip`——而带 `=` 的开关
        （`--unzip-command=cmd`）本来就活不过白名单，这条检查只剩误伤。
        """
        from agentcore.workspace.runner import permitted

        ok, reason = permitted("zip", ["report_v=2.zip", "a=v.txt", "b.txt"])
        assert ok, reason

    def test_flag_with_equals_still_rejected(self):
        from agentcore.workspace.runner import permitted

        for bad in ("--unzip-command=touch x", "-T=x", "-r=1"):
            ok, reason = permitted("zip", ["o.zip", "a.txt", bad])
            assert not ok, bad
            assert "zip" in reason
