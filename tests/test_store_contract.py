"""内存 / PostgreSQL 双实现的**共享契约测试**（REVIEW-a604023..679c9b3 第四批）。

为什么需要：两套实现此前各测各的，语义漂移只能靠人肉评审发现——历史事故
`list_facts` 排序相反、`kb_add_chunks` 内存完全不去重、会话键撞车（`"private"`）、
`latest_message_id` 口径不同。本文件用**同一批断言**同时打两个实现：
`store` fixture 参数化为 `memory` 与 `pg`，PG 参数在无 `TEST_DATABASE_URL` 时跳过
（CI 已注入 pgvector service，所以它不是"永久跳过"）。

覆盖不到的部分：`messages_after` 的**孤儿会话**（dirty data）在 PG 侧无法构造——
`messages.session_id` 有外键且无 ON DELETE CASCADE，删掉 session 行会被拒。
该契约由 `tests/test_review_m_fixes.py::TestSessionScopeContract` 在内存侧锁定。
"""

import os

import pytest
import pytest_asyncio

from agentcore.memory.store import InMemoryMemoryStore, PgMemoryStore

PG_URL = (os.getenv("TEST_DATABASE_URL") or "").strip()
_PG_MARK = pytest.mark.skipif(
    not PG_URL, reason="TEST_DATABASE_URL not set; PG contract param skipped"
)

# facts.embedding / kb_chunks.embedding 在 PG 侧是 vector(8)（与 test_pg_store 同口径）
_DIM = 8


def onehot(index: int) -> list[float]:
    """正交单位向量：相同 → 余弦 1.0，不同 → 余弦 0.0（阈值断言的稳定基础）。"""
    return [1.0 if i == index else 0.0 for i in range(_DIM)]


@pytest_asyncio.fixture(params=["memory", pytest.param("pg", marks=_PG_MARK)])
async def store(request):
    if request.param == "memory":
        impl = InMemoryMemoryStore()
    else:
        impl = PgMemoryStore(PG_URL, dim=_DIM)
    await impl.init()
    if request.param == "pg":
        async with impl.pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE messages, sessions, facts, kb_sources, kb_chunks, "
                "schedules, user_state CASCADE"
            )
    yield impl
    aclose = getattr(impl, "aclose", None)
    if aclose is not None:
        await aclose()


# --------------------------------------------------------------------------- #
# 会话作用域与身份
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_session_scope_and_identity_contract(store):
    private = await store.resolve_session("u1", None)
    group = await store.resolve_session("u1", "g1")
    # 群号字面量 "private" 不得与私聊撞键（PG 用 scope 列、内存用命名空间键）
    tricky = await store.resolve_session("u1", "private")

    assert len({private, group, tricky}) == 3, "私聊 / 群聊 / 群号=private 必须三行"
    assert await store.resolve_session("u1", None) == private, "同键必须复用同一行"
    assert await store.resolve_session("u1", "g1") == group

    assert await store.get_session_identity(private) == ("u1", None)
    assert await store.get_session_identity(group) == ("u1", "g1")
    assert await store.get_session_identity(tricky) == ("u1", "private")
    assert await store.get_session_identity("99999999") == ("", None), "未知会话返回空"


@pytest.mark.asyncio
async def test_history_contract(store):
    empty = await store.resolve_session("nobody", None)
    assert await store.get_history(empty) == []

    sid = await store.resolve_session("u1", "g1")
    for i in range(5):
        await store.append_message(sid, "user", f"m{i}")

    history = await store.get_history(sid, 2)
    assert [m["content"] for m in history] == ["m3", "m4"], "取最近 N 条且时间正序"
    # L4：limit<=0 统一按 1 处理（内存 [-0:] 曾返回全部、PG LIMIT 0 返回空）
    assert [m["content"] for m in await store.get_history(sid, 0)] == ["m4"]
    # 内部 id 不得出现在发给 LLM 的消息里
    assert set(history[0]) == {"role", "content"}


@pytest.mark.asyncio
async def test_tool_calls_roundtrip_contract(store):
    """JSONB/内存两边的 tool_calls 必须都能还原成结构化列表（历史事故：PG 返回字符串）。"""
    sid = await store.resolve_session("u1", None)
    calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "x", "arguments": "{}"},
        }
    ]
    await store.append_message(sid, "assistant", "", tool_calls=calls)
    await store.append_message(sid, "tool", "结果", tool_call_id="call_1")

    history = await store.get_history(sid, 10)
    assert history[0]["tool_calls"] == calls
    assert history[1]["tool_call_id"] == "call_1"
    # 无工具调用时不得凭空出现空字段
    await store.append_message(sid, "user", "普通消息")
    assert set((await store.get_history(sid, 1))[0]) == {"role", "content"}


@pytest.mark.asyncio
async def test_latest_message_id_and_messages_after_contract(store):
    assert await store.latest_message_id() == 0, "空库最新消息 id 必须是 0"

    group_sid = await store.resolve_session("u1", "g1")
    private_sid = await store.resolve_session("u1", None)
    g1 = await store.append_message(group_sid, "user", "群消息1")
    p1 = await store.append_message(private_sid, "user", "私聊消息")
    g2 = await store.append_message(group_sid, "user", "群消息2")

    assert await store.latest_message_id() == g2

    default = await store.messages_after(0)
    assert [r["content"] for r in default] == ["群消息1", "群消息2"], (
        "默认只出群聊（私聊不进公共蒸馏）且正序"
    )
    assert all(set(r) == {"id", "session_id", "role", "content"} for r in default), (
        "不得带出用户/群身份字段"
    )

    with_private = await store.messages_after(0, include_private=True)
    assert [r["content"] for r in with_private] == ["群消息1", "私聊消息", "群消息2"]
    assert [r["id"] for r in with_private] == [g1, p1, g2]

    assert [r["content"] for r in await store.messages_after(g1)] == ["群消息2"]
    assert [r["content"] for r in await store.messages_after(0, limit=1)] == ["群消息1"]
    # 非正 limit 一律空结果（原先内存"去掉最后 N 条"、PG 直接抛 LIMIT 负值）
    assert await store.messages_after(0, limit=0) == []
    assert await store.messages_after(0, limit=-1) == []


# --------------------------------------------------------------------------- #
# 长期记忆（facts）
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_facts_scope_dedupe_and_ordering_contract(store):
    sid_g = await store.resolve_session("u1", "g1")
    sid_p = await store.resolve_session("u1", None)

    assert await store.save_fact("u1", "喜欢猫", onehot(0), session_id=sid_g) is True
    assert (
        await store.save_fact("u1", "喜欢猫", onehot(0), session_id=sid_g) is False
    ), "同会话同内容必须去重"
    assert await store.save_fact("u1", "喜欢猫", onehot(0), session_id=sid_p) is True, (
        "不同会话可各存一份（作用域隔离）"
    )
    assert (
        await store.save_fact("u1", "最近在学 Rust", onehot(1), session_id=sid_g)
        is True
    )

    assert await store.list_facts("u1", session_id=sid_g) == [
        "最近在学 Rust",
        "喜欢猫",
    ], "最新优先（对齐 PG 的 ORDER BY id DESC）"
    assert await store.list_facts("u1", session_id=sid_p) == ["喜欢猫"]
    assert await store.list_facts("u1") == ["最近在学 Rust", "喜欢猫", "喜欢猫"], (
        "不限会话时按插入序倒序返回全部作用域"
    )

    assert await store.list_facts("u1", limit=1, session_id=sid_g) == ["最近在学 Rust"]
    assert await store.list_facts("u1", limit=0, session_id=sid_g) == []
    assert await store.list_facts("u1", limit=-3, session_id=sid_g) == []
    assert await store.list_facts("nobody") == []


@pytest.mark.asyncio
async def test_recall_facts_contract(store):
    sid = await store.resolve_session("u1", "g1")
    await store.save_fact("u1", "喜欢猫", onehot(0), source="chat", session_id=sid)
    await store.save_fact("u1", "喜欢狗", onehot(1), source="chat", session_id=sid)

    assert await store.recall_facts("u1", onehot(0), top_k=0) == []
    assert await store.recall_facts("u1", onehot(0), top_k=-1) == []

    hits = await store.recall_facts("u1", onehot(0), top_k=1, threshold=0.5)
    assert [h["content"] for h in hits] == ["喜欢猫"], "最相似者优先，低于阈值被过滤"
    assert hits[0]["score"] > 0.99
    assert hits[0]["source"] == "chat"

    both = await store.recall_facts("u1", onehot(0), top_k=5, threshold=0.0)
    assert [h["content"] for h in both] == ["喜欢猫", "喜欢狗"], "按相似度降序"
    assert await store.recall_facts("nobody", onehot(0), top_k=5) == []


# --------------------------------------------------------------------------- #
# 知识库（KB）
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_kb_chunks_dedupe_search_and_delete_contract(store):
    source = await store.kb_add_source(
        "手册", "manual", location="data/x.md", meta={"v": 1}
    )
    assert await store.kb_add_source("另一本", "distill") != source

    # 批内重复同样要去重（内存实现原先是完全不去重）
    assert (
        await store.kb_add_chunks(
            source, ["A", "B", "A"], [onehot(0), onehot(1), onehot(0)]
        )
        == 2
    )
    assert (
        await store.kb_add_chunks(
            source, ["A", "B", "C"], [onehot(0), onehot(1), onehot(2)]
        )
        == 1
    )

    assert await store.kb_stats() == {"sources": 2, "chunks": 3}

    listed = await store.kb_list_sources()
    assert [s["name"] for s in listed] == ["另一本", "手册"], "按时间倒序"
    manual = next(s for s in listed if s["name"] == "手册")
    assert manual["kind"] == "manual"
    assert manual["location"] == "data/x.md"
    assert manual["chunks"] == 3
    assert manual["meta"] == {"v": 1}

    hits = await store.kb_search(onehot(2), top_k=2, threshold=0.5)
    assert [h["chunk"] for h in hits] == ["C"]
    assert hits[0]["source_name"] == "手册"
    assert hits[0]["kind"] == "manual"
    assert hits[0]["source_id"] == source

    assert await store.kb_search(onehot(2), top_k=0) == []
    assert await store.kb_search(onehot(2), top_k=-2) == []
    assert await store.kb_list_sources(limit=0) == []
    assert await store.kb_list_sources(limit=-1) == []

    assert await store.kb_delete_source(source) == 3
    assert await store.kb_stats() == {"sources": 1, "chunks": 0}
    assert await store.kb_search(onehot(2), top_k=2) == []
    assert await store.kb_delete_source(source) == 0, "重复删除返回 0 而不是报错"


@pytest.mark.asyncio
async def test_kb_last_digest_watermark_contract(store):
    """水位线 = **最新一条** distill 来源记录的进度（PG: ORDER BY id DESC LIMIT 1）。

    内存实现原先取所有 distill 来源的 max——两次实现口径不同：若较新的蒸馏批次
    记录的水位更小（回退/重跑），max 会永久跳过中间消息，而 PG 允许重新处理
    （chunk 去重保证幂等）。这里以 PG 为准锁死。
    """
    assert await store.kb_last_digest_watermark() == 0, "从未蒸馏 → 0"

    await store.kb_add_source("digest-1", "distill", meta={"last_message_id": 5})
    assert await store.kb_last_digest_watermark() == 5

    await store.kb_add_source("manual-1", "manual", meta={"last_message_id": 42})
    assert await store.kb_last_digest_watermark() == 5, "非 distill 来源不参与"

    await store.kb_add_source("digest-2", "distill", meta={"last_message_id": 9})
    assert await store.kb_last_digest_watermark() == 9

    await store.kb_add_source("digest-3", "distill", meta={"last_message_id": 3})
    assert await store.kb_last_digest_watermark() == 3, "以最新 distill 来源为准"


# --------------------------------------------------------------------------- #
# 用户偏好与定时提醒
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_user_persona_contract(store):
    assert await store.get_user_persona("u1") is None
    await store.set_user_persona("u1", "温柔")
    assert await store.get_user_persona("u1") == "温柔"
    await store.set_user_persona("u1", "严肃")
    assert await store.get_user_persona("u1") == "严肃"
    await store.set_user_persona("u1", None)
    assert await store.get_user_persona("u1") is None
    assert await store.get_user_persona("nobody") is None


@pytest.mark.asyncio
async def test_schedule_lifecycle_contract(store):
    a_late = await store.schedule_add(
        kind="once", target="group:1", message="晚", user_id="a", next_run=200.0
    )
    a_soon = await store.schedule_add(
        kind="once", target="group:1", message="早", user_id="a", next_run=100.0
    )
    b = await store.schedule_add(
        kind="cron",
        target="private:2",
        message="周期",
        user_id="b",
        cron="0 9 * * *",
        next_run=50.0,
    )

    assert [r["id"] for r in await store.schedule_list("a")] == [a_soon, a_late], (
        "按 next_run 升序"
    )
    assert [r["id"] for r in await store.schedule_list()] == [b, a_soon, a_late]
    row = (await store.schedule_list("a"))[0]
    assert row["message"] == "早" and row["enabled"] is True and row["kind"] == "once"

    assert [r["id"] for r in await store.schedule_due(150.0)] == [b, a_soon]
    assert await store.schedule_due(0.0) == []
    assert await store.schedule_due(1000.0, limit=0) == []
    assert await store.schedule_due(1000.0, limit=-1) == []
    assert [r["id"] for r in await store.schedule_due(1000.0, limit=2)] == [b, a_soon]

    assert await store.schedule_cancel(a_soon, user_id="b") is False, "不能取消别人的"
    assert await store.schedule_cancel(a_soon, user_id="a") is True
    assert await store.schedule_cancel(a_soon, user_id="a") is False, (
        "重复取消返回 False"
    )
    assert [r["id"] for r in await store.schedule_list("a")] == [a_late]
    assert {r["id"] for r in await store.schedule_list("a", include_disabled=True)} == {
        a_soon,
        a_late,
    }

    await store.schedule_mark_fired(a_late, None)
    assert [r["id"] for r in await store.schedule_list("a")] == [], "一次性已送达即停用"
    disabled = {
        r["id"]: r["enabled"]
        for r in await store.schedule_list("a", include_disabled=True)
    }
    assert disabled == {a_soon: False, a_late: False}, "两条都已停用（取消 / 已送达）"

    await store.schedule_mark_fired(b, 300.0)
    assert [r["id"] for r in await store.schedule_due(350.0)] == [b], (
        "cron 顺延到下一次"
    )
    assert await store.schedule_cancel(b) is True, "user_id=None 表示管理员操作"
