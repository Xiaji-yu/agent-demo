import asyncio

import pytest

from plugins.qq_agent_adapter.debounce import Debouncer


@pytest.mark.asyncio
async def test_merge_quick_messages_then_single_run():
    d = Debouncer(delay=0.05)
    calls = []

    async def runner(parts):
        calls.append(parts)

    await d.push("k", {"t": "你好"}, runner)
    await asyncio.sleep(0.01)
    await d.push("k", {"t": "我补充一下"}, runner)
    await asyncio.sleep(0.15)  # 超过窗口

    assert len(calls) == 1
    assert len(calls[0]) == 2  # 两条合并
    assert calls[0][0]["t"] == "你好"
    assert calls[0][1]["t"] == "我补充一下"
    assert d.pending_keys() == []


@pytest.mark.asyncio
async def test_separate_keys_do_not_merge():
    d = Debouncer(delay=0.05)
    calls = []

    async def runner(parts):
        calls.append(parts)

    await d.push("k1", {"t": "a"}, runner)
    await asyncio.sleep(0.01)
    await d.push("k2", {"t": "b"}, runner)
    await asyncio.sleep(0.15)

    assert len(calls) == 2
    assert d.pending_keys() == []


@pytest.mark.asyncio
async def test_runner_exception_does_not_break():
    d = Debouncer(delay=0.02)
    calls = []

    async def runner(parts):
        raise RuntimeError("boom")

    await d.push("k", {"t": "a"}, runner)
    await asyncio.sleep(0.1)
    assert d.pending_keys() == []
    assert calls == []


@pytest.mark.asyncio
async def test_zero_delay_runs_immediately():
    d = Debouncer(delay=0)
    calls = []

    async def runner(parts):
        calls.append(parts)

    await d.push("k", {"t": "a"}, runner)
    await asyncio.sleep(0.01)
    assert len(calls) == 1
