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
    """runner 抛错后：防抖器必须仍然可用（后续窗口照常结算）。

    原用例两条断言恒真（pending 在执行前已 pop、calls 从未 append），
    去掉 _run_parts 的异常吞没也不会失败 —— 这里改成真正的行为断言。
    """
    d = Debouncer(delay=0.02)
    invoked = []

    async def bad_runner(parts):
        invoked.append(list(parts))
        raise RuntimeError("boom")

    async def good_runner(parts):
        invoked.append(list(parts))

    await d.push("k", {"t": "a"}, bad_runner)
    await asyncio.sleep(0.08)
    assert invoked == [[{"t": "a"}]], "失败的 runner 必须真的被调用过"

    await d.push("k", {"t": "b"}, good_runner)
    await asyncio.sleep(0.08)
    assert invoked[-1] == [{"t": "b"}], "异常之后防抖器必须仍能正常工作"
    assert d.pending_keys() == []


@pytest.mark.asyncio
async def test_zero_delay_runs_immediately():
    """delay=0 必须在 push 返回**之前**同步跑完 runner（不走窗口任务）。

    原断言在 sleep(0.01) 之后计数——把实现改成 create_task 一样通过，
    「立即」这个行为没有被验证。
    """
    d = Debouncer(delay=0)
    calls = []

    async def runner(parts):
        calls.append(parts)

    await d.push("k", {"t": "a"}, runner)
    assert calls == [[{"t": "a"}]], "push 返回时 runner 必须已执行（不是排任务）"
    assert d.pending_keys() == []


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
async def test_key_lock_persist_no_eviction():
    """L23：per-key 锁创建后不再淘汰。

    旧实现 runner 结束后 pop 锁：release 后、等待者唤醒前 pop 会把等待者留在
    孤儿锁上，新窗口另建新锁并发执行。改为不清理后，同 key 的锁对象跨多次
    执行保持稳定，串行不变量不依赖清理时机。
    """
    d = Debouncer(delay=0.01)
    in_flight = False

    async def runner(parts):
        nonlocal in_flight
        assert not in_flight, "同 key 不得并发执行 runner"
        in_flight = True
        await asyncio.sleep(0.02)
        in_flight = False

    for i in range(3):
        await d.push("k", {"t": i}, runner)
        await asyncio.sleep(0.05)  # 窗口到期 + runner 执行完（旧实现此刻已 pop 锁）

    assert "k" in d._key_locks, "锁不应被清理淘汰"
    lock = d._key_locks["k"]
    for i in range(3, 5):
        await d.push("k", {"t": i}, runner)
        await asyncio.sleep(0.05)
    assert d._key_locks["k"] is lock, "后续窗口必须复用同一把锁（杜绝孤儿锁竞态）"


@pytest.mark.asyncio
async def test_push_during_runner_not_split():
    # M10：窗口边界（runner 在途）到达的消息不把「半句+补充」拆成两次并发调用
    d = Debouncer(delay=0.02)
    calls = []

    async def runner(parts):
        calls.append([p["t"] for p in parts])
        await asyncio.sleep(0.05)

    await d.push("k", {"t": "半句"}, runner)
    await asyncio.sleep(0.05)  # 第一个窗口到期，runner 开始执行
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


@pytest.mark.asyncio
async def test_flush_all_survives_cancelled_window():
    """M5：单个窗口被取消（CancelledError 属 BaseException）不能中断整批 flush。

    旧实现 `_run_parts` 只捕 Exception，CancelledError 会穿透 flush_all 的循环，
    导致同一批里排在后面的窗口消息仍然静默丢失——恰好是 L1 想防的那件事。
    """
    d = Debouncer(delay=60)
    flushed = []

    async def cancelling_runner(parts):
        flushed.append(parts[0]["t"])
        raise asyncio.CancelledError()

    async def normal_runner(parts):
        flushed.append(parts[0]["t"])

    await d.push("k_cancel", {"t": "被取消"}, cancelling_runner)
    await d.push("k_ok", {"t": "应仍被处理"}, normal_runner)

    await d.flush_all()

    assert "应仍被处理" in flushed, "前一个窗口被取消后，后续窗口仍必须被 flush"
    assert d.pending_keys() == []


@pytest.mark.asyncio
async def test_flush_all_isolates_runner_exception():
    """M5：某个窗口抛异常也不能影响同批其它窗口。"""
    d = Debouncer(delay=60)
    flushed = []

    async def boom(parts):
        raise RuntimeError("boom")

    async def ok(parts):
        flushed.append(parts[0]["t"])

    await d.push("k1", {"t": "坏"}, boom)
    await d.push("k2", {"t": "好"}, ok)

    await d.flush_all()
    assert flushed == ["好"]
    assert d.pending_keys() == []


# ==========================================================================
# REVIEW 批次回归（原 test_review_concurrency_fixes / test_review_5_fixes）
# ==========================================================================


# 来源: REVIEW-a604023..679c9b3 第三批 TestDebounceBurstCap
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


# 来源: REVIEW-679c9b3..c472e56 第五批 TestDebounceMaxPartsOne / TestBurstTaskKeepsReference
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
