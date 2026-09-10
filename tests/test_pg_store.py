"""P0-1/P0-2/P0-3 的 PG 集成测试。

仅在设置 TEST_DATABASE_URL 时运行（CI 无 PG 会自动跳过）；本地用
`TEST_DATABASE_URL=postgresql://... pytest tests/test_pg_store.py` 验证
内存实现与 PG 实现的行为一致（历史窗口、会话去重、索引存在性）。
"""
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL not set; PG integration tests skipped",
)

from agentcore.memory.store import PgMemoryStore  # noqa: E402


@pytest.fixture
async def store():
    s = PgMemoryStore(os.environ["TEST_DATABASE_URL"], dim=8)
    await s.init()
    yield s
    await s.aclose()


@pytest.fixture
async def clean(store):
    async with store.pool.acquire() as conn:
        await conn.execute("TRUNCATE messages, sessions, facts, kb_chunks CASCADE")
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
