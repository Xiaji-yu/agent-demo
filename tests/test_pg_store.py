"""PG 集成测试——只放 **PG 独有面**（DDL/索引/连接池/损坏数据/PG 类型往返）。

两套实现的**行为断言**已统一由 `tests/test_store_contract.py` 参数化锁死
（同一批断言同时打内存与 PG）；原先与之重复的 13 条已并入契约套件，
勿再把通用行为断言加回本文件。
"""

import os
import time

import asyncpg
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
        await conn.execute(
            "TRUNCATE messages, sessions, facts, kb_sources, kb_chunks CASCADE"
        )
    yield


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
                "INSERT INTO messages(session_id, role, content) VALUES($1,'user',$2)",
                sid,
                f"m{i}",
            )
        await conn.execute(
            "INSERT INTO facts(user_id, session_id, content, embedding, source) "
            "VALUES($1,$2,'旧事实',$3::vector,'')",
            "u-legacy",
            first,
            "[" + ",".join(["0.1"] * 8) + "]",
        )

    # 重新 init：应自动合并重复会话并补建索引，不抛异常
    # （L2 之后 init 对已建池的 store 是幂等空操作；这里要验证真实重跑，先释放旧池）
    await store.aclose()
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
        fact_sid = await conn.fetchval(
            "SELECT session_id FROM facts WHERE user_id='u-legacy'"
        )
    assert sessions == 1, "重复会话应被合并为一行"
    assert messages == 5, "消息一条都不能丢"
    assert idx == 1, "合并后应能建出唯一索引"
    # fact 被重新指向存活的会话 → 作用域内仍可召回
    sid = await store.resolve_session("u-legacy", None)
    assert str(fact_sid) == sid
    assert await store.list_facts("u-legacy", session_id=sid) == ["旧事实"]

    # 幂等：真实重跑一次 init 不应改变任何东西
    await store.aclose()
    await store.init()
    async with store.pool.acquire() as conn:
        assert (
            await conn.fetchval(
                "SELECT count(*) FROM sessions WHERE user_id='u-legacy'"
            )
            == 1
        )


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


# ---------- 评审修复回归（L1/L2/L3/L4/L5/L7/L8/L10/M1 存储侧） ----------


@pytest.mark.asyncio
async def test_init_repairs_null_and_empty_group_id_mix(store, clean):
    """L1 回归：group_id=NULL 与 '' 并存的历史脏数据必须按 COALESCE 口径合并。

    修复前按原值分组计数判「无重复」→ 不合并 → 唯一索引建不起来 → 每次启动
    报错不收敛；且 '' 行若作为存活行保留，_find_session 按 IS NULL 查私聊会查不到。
    """
    async with store.pool.acquire() as conn:
        await conn.execute("DROP INDEX IF EXISTS sessions_user_scope_key")
        # 故意让 '' 行 id 更小（MIN(id) 口径下它会存活）——验证规范化确实生效
        empty_sid = await conn.fetchval(
            "INSERT INTO sessions(user_id, group_id, scope) VALUES('u-l1','','private') RETURNING id"
        )
        null_sid = await conn.fetchval(
            "INSERT INTO sessions(user_id, group_id, scope) VALUES('u-l1',NULL,'private') RETURNING id"
        )
        await conn.execute(
            "INSERT INTO messages(session_id, role, content) VALUES($1,'user','from-empty')",
            empty_sid,
        )
        await conn.execute(
            "INSERT INTO messages(session_id, role, content) VALUES($1,'user','from-null')",
            null_sid,
        )

    await store.aclose()
    await store.init()  # 不应抛错

    async with store.pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, group_id FROM sessions WHERE user_id='u-l1'"
        )
        idx = await conn.fetchval(
            "SELECT 1 FROM pg_indexes WHERE indexname='sessions_user_scope_key'"
        )
    assert len(rows) == 1, "NULL 与 '' 同属一组，应合并为一行"
    assert rows[0]["group_id"] is None, "存活行必须是规范形态（group_id=NULL）"
    assert idx == 1, "合并后应能建出唯一索引"
    # 私聊会话解析正常（存活行可被 NULL 安全查找命中）
    assert await store.resolve_session("u-l1", None) == str(rows[0]["id"])
    # 消息一条不丢
    history = await store.get_history(str(rows[0]["id"]))
    assert [h["content"] for h in history] == ["from-empty", "from-null"]


@pytest.mark.asyncio
async def test_repeated_init_reuses_existing_pool(store, monkeypatch):
    """L2 回归：重复 init 不得新建/替换连接池（旧池不 close 会泄漏）。"""
    created = []
    real_create_pool = asyncpg.create_pool

    async def spy_create_pool(*args, **kwargs):
        pool = await real_create_pool(*args, **kwargs)
        created.append(pool)
        return pool

    monkeypatch.setattr(asyncpg, "create_pool", spy_create_pool)
    before = store.pool
    await store.init()
    assert store.pool is before, "第二次 init 不应替换现有池"
    assert created == [], "第二次 init 不应再创建新池"


@pytest.mark.asyncio
async def test_get_history_tolerates_corrupt_tool_calls_jsonb(store, clean):
    """L5 回归：tool_calls 列被写坏成 JSON object / 标量时，get_history 不抛异常，
    按缺失处理（不带 tool_calls 下发）。"""
    sid = await store.resolve_session("u1", None)
    async with store.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO messages(session_id, role, content, tool_calls) "
            "VALUES($1,'assistant','x',$2::jsonb)",
            int(sid),
            '{"id": "c1"}',
        )
        await conn.execute(
            "INSERT INTO messages(session_id, role, content, tool_calls) "
            "VALUES($1,'assistant','y','\"just-a-string\"')",
            int(sid),
        )
        await conn.execute(
            "INSERT INTO messages(session_id, role, content, tool_calls) "
            "VALUES($1,'assistant','z','[1,2]'::jsonb)",
            int(sid),
        )
    history = await store.get_history(sid)
    assert [h["content"] for h in history] == ["x", "y", "z"]
    assert all("tool_calls" not in h for h in history)


@pytest.mark.asyncio
async def test_kb_delete_source_atomic_on_failure(store, clean):
    """L7 回归：来源删除失败时块删除必须整体回滚（不留 0-chunk 空壳）。"""
    vec = [0.1] * 8
    sid = await store.kb_add_source("待删来源", "manual")
    await store.kb_add_chunks(sid, ["c1", "c2"], [vec, vec])

    real_pool = store.pool

    class FailingConn:
        """第二条 DELETE（kb_sources）注入故障的连接代理。"""

        def __init__(self, conn):
            self._conn = conn

        async def fetchval(self, *a, **k):
            return await self._conn.fetchval(*a, **k)

        async def execute(self, sql, *a, **k):
            if "DELETE FROM kb_sources" in sql:
                raise RuntimeError("simulated crash between deletes")
            return await self._conn.execute(sql, *a, **k)

        def transaction(self, *a, **k):
            return self._conn.transaction(*a, **k)

        def __getattr__(self, item):
            return getattr(self._conn, item)

    class FailingPool:
        def __init__(self, pool):
            self._pool = pool

        def acquire(self):
            outer = self._pool.acquire()

            class _CM:
                async def __aenter__(self):
                    return FailingConn(await outer.__aenter__())

                async def __aexit__(self, *exc):
                    return await outer.__aexit__(*exc)

            return _CM()

        def __getattr__(self, item):
            return getattr(self._pool, item)

    store.pool = FailingPool(real_pool)
    try:
        with pytest.raises(RuntimeError):
            await store.kb_delete_source(sid)
    finally:
        store.pool = real_pool

    # 回滚：块还在、来源还在（无事务时块已被删、来源残留 → 0-chunk 空壳）
    assert await store.kb_delete_source(sid) == 2
    assert (await store.kb_stats()) == {"sources": 0, "chunks": 0}


@pytest.mark.asyncio
async def test_messages_after_null_session_id(store, clean):
    """L8 回归：session_id 为 NULL 的历史行不得产出字面量 \"None\"。"""
    async with store.pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO messages(session_id, role, content) VALUES(NULL,'user','孤儿')"
        )
    rows = await store.messages_after(0, include_private=True)
    assert [r["content"] for r in rows] == ["孤儿"]
    assert [r["session_id"] for r in rows] == [""]
