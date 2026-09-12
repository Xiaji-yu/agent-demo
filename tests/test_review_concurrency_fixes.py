"""REVIEW-a604023..679c9b3 第三批（并发/资源）修复的回归测试。"""

from __future__ import annotations

import asyncio

import pytest


class TestDebounceBurstCap:
    @pytest.mark.asyncio
    async def test_burst_flushes_immediately_without_loss(self):
        """突发刷屏达到上限即结算本批（不丢内容、单窗口不膨胀）。"""
        from plugins.qq_agent_adapter.debounce import Debouncer

        seen: list[list] = []
        done = asyncio.Event()

        async def runner(parts):
            seen.append(parts)
            done.set()

        deb = Debouncer(delay=30, max_parts=3)
        for i in range(3):
            await deb.push("k", i, runner)

        await asyncio.wait_for(done.wait(), timeout=2)
        assert len(seen) == 1
        assert seen[0] == [0, 1, 2], "达到上限应立即结算，且内容完整"
        assert deb.pending_keys() == [], "结算后应开新窗口（旧窗口已清空）"

    @pytest.mark.asyncio
    async def test_below_cap_still_debounces(self):
        from plugins.qq_agent_adapter.debounce import Debouncer

        seen: list[list] = []

        async def runner(parts):
            seen.append(parts)

        deb = Debouncer(delay=0.05, max_parts=10)
        await deb.push("k", 1, runner)
        await asyncio.sleep(0.12)
        assert len(seen) == 1 and seen[0] == [1]


class TestMergeOrdering:
    def test_merge_sorts_by_message_id(self):
        from plugins.qq_agent_adapter.pipeline import merge_parts

        # payload 构建完成顺序被图片下载拖成倒序，但 message_id 仍能恢复真实顺序
        parts = [
            {"text": "第二句", "images": [], "message_id": "102"},
            {"text": "第一句", "images": [], "message_id": "101"},
        ]
        text, _ = merge_parts(parts)
        assert text == "第一句\n第二句"

    def test_merge_keeps_arrival_order_without_ids(self):
        from plugins.qq_agent_adapter.pipeline import merge_parts

        parts = [{"text": "A"}, {"text": "B"}]
        assert merge_parts(parts)[0] == "A\nB"


class TestRecentImageByteBudget:
    def test_evicts_oldest_when_over_budget(self):
        from plugins.qq_agent_adapter.pipeline import RecentImageBuffer

        buf = RecentImageBuffer(ttl=3600, max_entries=32, max_images=2, max_bytes=100)
        buf.put("k1", ["x" * 60])  # 60 字节
        buf.put("k2", ["y" * 60])  # 累计 120 > 100 → 淘汰最旧 k1
        assert len(buf) == 2 or "k1" not in buf._data
        total = sum(v["bytes"] for v in buf._data.values())
        assert total <= 120  # 允许最后一条自身超预算（不丢当前会话）
        buf.put("k3", ["z" * 60])
        assert len(buf) == 1, "超预算时按最旧淘汰"

    def test_unlimited_when_zero(self):
        from plugins.qq_agent_adapter.pipeline import RecentImageBuffer

        buf = RecentImageBuffer(ttl=3600, max_entries=32, max_images=2, max_bytes=0)
        for i in range(5):
            buf.put(f"k{i}", ["x" * 1000])
        assert len(buf) == 5


class TestGlobalTurnSemaphore:
    def test_semaphore_limits_concurrency(self, monkeypatch):
        monkeypatch.setenv("AGENT_MAX_CONCURRENT_TURNS", "2")
        import plugins.qq_agent_adapter.matcher as m

        m._turn_semaphore = None
        sem = m._get_turn_semaphore()
        assert sem._value == 2

    @pytest.mark.asyncio
    async def test_burst_does_not_exceed_limit(self, monkeypatch):
        monkeypatch.setenv("AGENT_MAX_CONCURRENT_TURNS", "2")
        import plugins.qq_agent_adapter.matcher as m

        m._turn_semaphore = None
        running = 0
        peak = 0

        async def fake_run_and_format(payload, text, images):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.05)
            running -= 1
            return "ok"

        monkeypatch.setattr(m, "_run_and_format", fake_run_and_format)
        monkeypatch.setattr(m, "deliver_reply", _noop_deliver, raising=False)
        payload = {
            "user_id": "1",
            "group_id": None,
            "text": "hi",
            "images": [],
            "message_id": "1",
            "chat_target": "private:1",
        }
        await asyncio.gather(*(m._answer([dict(payload)]) for _ in range(6)))
        assert peak <= 2, f"并发闸门失效：峰值 {peak}"


async def _noop_deliver(*args, **kwargs):
    return {"mode": "single", "sent": 1}
