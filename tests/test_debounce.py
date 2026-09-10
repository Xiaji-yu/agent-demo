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


@pytest.mark.asyncio
async def test_runner_serialized_per_key():
    # M10：LLM 长延迟下，同 key 新窗口不会与在途 runner 并发执行
    d = Debouncer(delay=0.02)
    order = []
    in_flight = False

    async def slow_runner(parts):
        nonlocal in_flight
        assert not in_flight, "同 key 不得并发执行 runner"
        in_flight = True
        order.append(("start", parts[0]["t"]))
        await asyncio.sleep(0.08)
        order.append(("end", parts[0]["t"]))
        in_flight = False

    await d.push("k", {"t": "第一段"}, slow_runner)
    await asyncio.sleep(0.05)  # 第一段已进入 runner 执行
    await d.push("k", {"t": "第二段"}, slow_runner)  # 在途时到达 → 新窗口
    await asyncio.sleep(0.2)

    assert [o[0] for o in order] == ["start", "end", "start", "end"]


@pytest.mark.asyncio
async def test_push_during_runner_not_split():
    # M10：窗口边界（runner 在途）到达的消息不把「半句+补充」拆成两次并发调用
    d = Debouncer(delay=0.02)
    calls = []

    async def runner(parts):
        calls.append([p["t"] for p in parts])
        await asyncio.sleep(0.05)

    await d.push("k", {"t": "半句"}, runner)
    await asyncio.sleep(0.05)   # 第一个窗口到期，runner 开始执行
    await d.push("k", {"t": "补充"}, runner)
    await asyncio.sleep(0.2)

    assert len(calls) == 2
    assert calls[0] == ["半句"]
    assert calls[1] == ["补充"]


@pytest.mark.asyncio
async def test_flush_all_runs_pending_windows():
    # L1：停机前 flush，未到期消息不静默丢失
    d = Debouncer(delay=60)  # 窗口极长，正常永远不会触发
    calls = []

    async def runner(parts):
        calls.append([p["t"] for p in parts])

    await d.push("k1", {"t": "a"}, runner)
    await d.push("k2", {"t": "b"}, runner)
    assert d.pending_keys() == ["k1", "k2"]

    await d.flush_all()
    assert sorted(calls) == [["a"], ["b"]]
    assert d.pending_keys() == []
