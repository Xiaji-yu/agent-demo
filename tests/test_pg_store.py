"""P0-1/P0-2/P0-3 的 PG 集成测试。

仅在设置 TEST_DATABASE_URL 时运行（CI 无 PG 会自动跳过）；本地用
`TEST_DATABASE_URL=postgresql://... pytest tests/test_pg_store.py` 验证
内存实现与 PG 实现的行为一致（历史窗口、会话去重、索引存在性）。
"""
import os
import time

import pytest
import pytest_asyncio

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL not set; PG integration tests skipped",
)

from agentcore.memory.store import PgMemoryStore  # noqa: E402


@pytest_asyncio.fixture
async def store():
    s = PgMemoryStore(os.environ["TEST_DATABASE_URL"], dim=8)
    await s.init()
    yield s
    await s.aclose()


@pytest_asyncio.fixture
async def clean(store):
    async with store.pool.acquire() as conn:
        await conn.execute("TRUNCATE messages, sessions, facts, kb_sources, kb_chunks CASCADE")
    yield


@pytest.mark.asyncio
async def test_resolve_session_idempotent_private(store, clean):
    # P0-3：私聊 group_id=None 必须复用同一行（NULL 安全 + 唯一约束）
    sid1 = await store.resolve_session("u1", None)
    sid2 = await store.resolve_session("u1", None)
    assert sid1 == sid2


@pytest.mark.asyncio
async def test_resolve_session_private_vs_group(store, clean):
    sid_p = await store.resolve_session("u1", None)
    sid_g = await store.resolve_session("u1", "g1")
    assert sid_p != sid_g
    assert sid_g == await store.resolve_session("u1", "g1")


@pytest.mark.asyncio
async def test_history_returns_latest_limited(store, clean):
    # P0-1：与内存实现一致——超过 limit 返回最近 limit 条且时间正序
    sid = await store.resolve_session("u1", None)
    for i in range(25):
        await store.append_message(sid, "user", f"msg-{i}")
    history = await store.get_history(sid, limit=20)
    assert [h["content"] for h in history] == [f"msg-{i}" for i in range(5, 25)]


@pytest.mark.asyncio
async def test_expected_indexes_exist(store, clean):
    # P0-2：常用查询路径的索引已建
    async with store.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE tablename IN ('messages','sessions','facts')"
        )
    names = {r["indexname"] for r in rows}
    assert "messages_session_id_idx" in names
    assert "facts_user_id_idx" in names
    assert "sessions_user_scope_key" in names


@pytest.mark.asyncio
async def test_facts_scoped_per_conversation(store, clean):
    # 长期记忆按会话隔离：群 A 的事实不得召回到群 B / 私聊
    sid_a = await store.resolve_session("u1", "groupA")
    sid_b = await store.resolve_session("u1", "groupB")
    sid_p = await store.resolve_session("u1", None)
    await store.save_fact("u1", "在群里说过喜欢围棋", [1.0] * 8, session_id=sid_a)

    assert await store.list_facts("u1", session_id=sid_a) == ["在群里说过喜欢围棋"]
    assert await store.list_facts("u1", session_id=sid_b) == []
    assert await store.list_facts("u1", session_id=sid_p) == []
    assert await store.recall_facts("u1", [1.0] * 8, session_id=sid_b) == []
    assert (await store.recall_facts("u1", [1.0] * 8, session_id=sid_a))[0]["content"] == "在群里说过喜欢围棋"


@pytest.mark.asyncio
async def test_same_fact_stored_once_per_scope(store, clean):
    # 去重按作用域：同一句话在两个群可各存一份
    sid_a = await store.resolve_session("u1", "groupA")
    sid_b = await store.resolve_session("u1", "groupB")
    assert await store.save_fact("u1", "喜欢 Python", [1.0] * 8, session_id=sid_a)
    assert await store.save_fact("u1", "喜欢 Python", [1.0] * 8, session_id=sid_b)
    assert not await store.save_fact("u1", "喜欢 Python", [1.0] * 8, session_id=sid_a)
    assert len(await store.list_facts("u1")) == 2


@pytest.mark.asyncio
async def test_unscoped_list_spans_all_sessions(store, clean):
    sid_a = await store.resolve_session("u1", "groupA")
    sid_b = await store.resolve_session("u1", "groupB")
    await store.save_fact("u1", "A", [1.0] * 8, session_id=sid_a)
    await store.save_fact("u1", "B", [1.0] * 8, session_id=sid_b)
    assert sorted(await store.list_facts("u1")) == ["A", "B"]


@pytest.mark.asyncio
async def test_init_repairs_legacy_duplicate_sessions(store, clean):
    """升级路径：旧库存在重复会话行时，init() 必须合并而不是抛错（否则机器人起不来）。

    复现历史 bug：私聊 group_id=NULL 用 `=` 比较恒不成立 → 每条消息建一个新 session。
    """
    async with store.pool.acquire() as conn:
        # 去掉唯一索引，模拟旧库；再灌入重复会话 + 各自 1 条消息 + 指向其中一行的 fact
        await conn.execute("DROP INDEX IF EXISTS sessions_user_scope_key")
        first = None
        for i in range(5):
            sid = await conn.fetchval(
                "INSERT INTO sessions(user_id, group_id, scope) VALUES($1,NULL,'private') RETURNING id",
                "u-legacy",
            )
            first = first or sid
            await conn.execute(
                "INSERT INTO messages(session_id, role, content) VALUES($1,'user',$2)", sid, f"m{i}"
            )
        await conn.execute(
            "INSERT INTO facts(user_id, session_id, content, embedding, source) "
            "VALUES($1,$2,'旧事实',$3::vector,'')",
            "u-legacy",
            first,
            "[" + ",".join(["0.1"] * 8) + "]",
        )

    # 重新 init：应自动合并重复会话并补建索引，不抛异常
    await store.init()

    async with store.pool.acquire() as conn:
        sessions = await conn.fetchval(
            "SELECT count(*) FROM sessions WHERE user_id='u-legacy' AND group_id IS NULL AND scope='private'"
        )
        messages = await conn.fetchval(
            "SELECT count(*) FROM messages m JOIN sessions s ON s.id=m.session_id WHERE s.user_id='u-legacy'"
        )
        idx = await conn.fetchval(
            "SELECT 1 FROM pg_indexes WHERE indexname='sessions_user_scope_key'"
        )
        fact_sid = await conn.fetchval("SELECT session_id FROM facts WHERE user_id='u-legacy'")
    assert sessions == 1, "重复会话应被合并为一行"
    assert messages == 5, "消息一条都不能丢"
    assert idx == 1, "合并后应能建出唯一索引"
    # fact 被重新指向存活的会话 → 作用域内仍可召回
    sid = await store.resolve_session("u-legacy", None)
    assert str(fact_sid) == sid
    assert await store.list_facts("u-legacy", session_id=sid) == ["旧事实"]

    # 幂等：再跑一次 init 不应改变任何东西
    await store.init()
    async with store.pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM sessions WHERE user_id='u-legacy'"
        ) == 1


@pytest.mark.asyncio
async def test_kb_add_search_delete(store, clean):
    # M5：公共知识库在 PG 上的读写与向量检索
    vec = [1.0] + [0.0] * 7
    sid = await store.kb_add_source("沙箱笔记", "manual", location="", meta={"chunks": 2})
    written = await store.kb_add_chunks(sid, ["命令白名单要逐参数校验", "find -exec 是执行入口"], [vec, vec])
    assert written == 2

    hits = await store.kb_search(vec, top_k=5, threshold=0.0)
    assert len(hits) == 2
    assert hits[0]["source_name"] == "沙箱笔记"
    assert hits[0]["kind"] == "manual"

    srcs = await store.kb_list_sources()
    assert len(srcs) == 1 and srcs[0]["chunks"] == 2
    assert srcs[0]["meta"] == {"chunks": 2}

    assert await store.kb_delete_source(sid) == 2
    assert (await store.kb_stats()) == {"sources": 0, "chunks": 0}
    assert await store.kb_search(vec, top_k=5) == []


@pytest.mark.asyncio
async def test_kb_watermark_roundtrip(store, clean):
    # 蒸馏水位线存在 distill 来源的 meta 里
    sid = await store.resolve_session("u1", None)
    for i in range(3):
        await store.append_message(sid, "user", f"m{i}")
    assert await store.kb_last_digest_watermark() == 0

    # TRUNCATE 不重置 SERIAL，id 不保证从 1 开始 → 用「当前最大 id」当水位线
    rows = await store.messages_after(0)
    assert [r["content"] for r in rows] == ["m0", "m1", "m2"]
    assert "user_id" not in rows[0]
    watermark = await store.latest_message_id()

    await store.kb_add_source("记忆蒸馏", "distill", meta={"last_message_id": watermark})
    assert await store.kb_last_digest_watermark() == watermark
    assert await store.messages_after(watermark) == []


@pytest.mark.asyncio
async def test_schedule_crud_and_due(store, clean):
    # M7 定时提醒：PG 侧增删查 + 到点筛选
    async with store.pool.acquire() as conn:
        await conn.execute("TRUNCATE schedules")
    now = time.time()
    sid = await store.schedule_add(
        kind="once", target="private:1", message="喝水", user_id="u1", next_run=now - 5
    )
    sid2 = await store.schedule_add(
        kind="cron", target="group:9", message="开会", user_id="u1",
        cron="0 9 * * *", next_run=now + 3600,
    )
    rows = await store.schedule_list("u1")
    assert [r["id"] for r in rows] == [sid, sid2]  # 按 next_run 升序
    assert rows[0]["kind"] == "once" and rows[1]["cron"] == "0 9 * * *"

    due = await store.schedule_due(now)
    assert [r["id"] for r in due] == [sid]
    assert due[0]["target"] == "private:1" and due[0]["message"] == "喝水"

    # 别人的提醒不能取消
    assert await store.schedule_cancel(sid, "other") is False
    assert await store.schedule_cancel(sid, "u1") is True
    assert [r["id"] for r in await store.schedule_list("u1")] == [sid2]

    # 标记触发：一次性 → 停用；周期 → 顺延
    await store.schedule_mark_fired(sid2, now + 7200)
    rows = await store.schedule_list("u1")
    assert rows[0]["next_run"] > now + 3600


@pytest.mark.asyncio
async def test_schedule_helpers_roundtrip_datetime(store, clean):
    """时间戳 ↔ timestamptz 往返不能串（提醒靠这个准时）。"""
    async with store.pool.acquire() as conn:
        await conn.execute("TRUNCATE schedules")
    ts = time.time() + 123.0
    sid = await store.schedule_add(
        kind="once", target="private:2", message="x", user_id="u2", next_run=ts
    )
    rows = await store.schedule_list("u2")
    assert rows[0]["id"] == sid
    assert abs(rows[0]["next_run"] - ts) < 1.0, (rows[0]["next_run"], ts)
