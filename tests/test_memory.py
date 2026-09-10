import pytest

from agentcore.memory.store import (
    InMemoryMemoryStore,
    _deserialize_tool_calls,
    _vector_dim_of,
    _vector_migration_enabled,
)


class TestInMemoryMemoryStore:
    @pytest.fixture
    def store(self):
        s = InMemoryMemoryStore()
        return s

    @pytest.mark.asyncio
    async def test_resolve_session(self, store):
        sid = await store.resolve_session("u1", None)
        assert sid == await store.resolve_session("u1", None)
        sid2 = await store.resolve_session("u2", "g1")
        assert sid != sid2

    @pytest.mark.asyncio
    async def test_append_and_get_history(self, store):
        sid = await store.resolve_session("u1", None)
        await store.append_message(sid, "user", "hello")
        await store.append_message(sid, "assistant", "hi")
        history = await store.get_history(sid)
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert history[0]["content"] == "hello"

    @pytest.mark.asyncio
    async def test_tool_calls_round_trip(self, store):
        sid = await store.resolve_session("u1", None)
        await store.append_message(
            sid,
            "assistant",
            "",
            tool_calls=[{"id": "call_1", "function": {"name": "calc"}}],
        )
        await store.append_message(sid, "tool", "42", tool_call_id="call_1")
        history = await store.get_history(sid)
        assert history[0]["tool_calls"] == [{"id": "call_1", "function": {"name": "calc"}}]
        # 与 PG 实现一致：空字段不下发（显式 null 会被严格 provider 拒绝）
        assert "tool_call_id" not in history[0]
        assert history[1]["tool_call_id"] == "call_1"

    @pytest.mark.asyncio
    async def test_empty_tool_call_id(self, store):
        sid = await store.resolve_session("u1", None)
        await store.append_message(sid, "tool", "ok", tool_call_id=None)
        history = await store.get_history(sid)
        assert "tool_call_id" not in history[0]

    @pytest.mark.asyncio
    async def test_history_returns_latest_when_over_limit(self, store):
        # P0-1 契约：超过 limit 时返回「最近的 limit 条」，且保持时间正序
        sid = await store.resolve_session("u1", None)
        for i in range(25):
            await store.append_message(sid, "user", f"msg-{i}")
        history = await store.get_history(sid, limit=20)
        assert len(history) == 20
        assert history[0]["content"] == "msg-5"
        assert history[-1]["content"] == "msg-24"

    @pytest.mark.asyncio
    async def test_get_history_limit_floor(self, store):
        # L4 契约：limit<=0 统一按 1 处理（不再出现「内存返回全部 / PG 返回空」的分歧）
        sid = await store.resolve_session("u1", None)
        for i in range(3):
            await store.append_message(sid, "user", f"m{i}")
        assert [m["content"] for m in await store.get_history(sid, limit=0)] == ["m2"]
        assert [m["content"] for m in await store.get_history(sid, limit=-5)] == ["m2"]
        assert [m["content"] for m in await store.get_history(sid, limit=1)] == ["m2"]


class TestInMemoryFacts:
    @pytest.fixture
    def store(self):
        return InMemoryMemoryStore()

    @pytest.mark.asyncio
    async def test_save_and_list(self, store):
        assert await store.save_fact("u1", "住在北京", [1.0, 0.0])
        assert await store.save_fact("u1", "喜欢 Python", [0.0, 1.0])
        assert await store.list_facts("u1") == ["住在北京", "喜欢 Python"]

    @pytest.mark.asyncio
    async def test_save_dedup(self, store):
        assert await store.save_fact("u1", "住在北京", [1.0, 0.0])
        assert not await store.save_fact("u1", "住在北京", [1.0, 0.0])
        assert len(await store.list_facts("u1")) == 1

    @pytest.mark.asyncio
    async def test_recall_similarity_ranking(self, store):
        await store.save_fact("u1", "喜欢围棋", [1.0, 0.1])
        await store.save_fact("u1", "讨厌下雨", [0.0, 1.0])
        hits = await store.recall_facts("u1", [1.0, 0.0], top_k=1, threshold=0.0)
        assert hits[0]["content"] == "喜欢围棋"

    @pytest.mark.asyncio
    async def test_recall_per_user(self, store):
        await store.save_fact("u1", "住在北京", [1.0, 0.0])
        await store.save_fact("u2", "住在上海", [1.0, 0.0])
        assert await store.list_facts("u2") == ["住在上海"]


class TestFactsSessionScope:
    """长期记忆按会话（用户 + 群/私聊）隔离：不同群聊的记忆不互串。"""

    @pytest.fixture
    def store(self):
        return InMemoryMemoryStore()

    @pytest.mark.asyncio
    async def test_facts_do_not_leak_across_groups(self, store):
        sid_a = await store.resolve_session("u1", "groupA")
        sid_b = await store.resolve_session("u1", "groupB")
        await store.save_fact("u1", "在群里说过喜欢围棋", [1.0, 0.0], session_id=sid_a)

        assert await store.list_facts("u1", session_id=sid_a) == ["在群里说过喜欢围棋"]
        assert await store.list_facts("u1", session_id=sid_b) == []
        assert await store.recall_facts("u1", [1.0, 0.0], session_id=sid_b) == []

    @pytest.mark.asyncio
    async def test_facts_do_not_leak_private_to_group(self, store):
        sid_p = await store.resolve_session("u1", None)
        sid_g = await store.resolve_session("u1", "groupA")
        await store.save_fact("u1", "私聊里说了生日", [1.0, 0.0], session_id=sid_p)

        assert await store.recall_facts("u1", [1.0, 0.0], session_id=sid_g) == []
        assert await store.recall_facts("u1", [1.0, 0.0], session_id=sid_p)

    @pytest.mark.asyncio
    async def test_same_fact_can_exist_per_scope(self, store):
        # 去重也按作用域：同一句话在两个群各记一份，互不吞并
        sid_a = await store.resolve_session("u1", "groupA")
        sid_b = await store.resolve_session("u1", "groupB")
        assert await store.save_fact("u1", "喜欢 Python", [1.0, 0.0], session_id=sid_a)
        assert await store.save_fact("u1", "喜欢 Python", [1.0, 0.0], session_id=sid_b)
        assert not await store.save_fact("u1", "喜欢 Python", [1.0, 0.0], session_id=sid_a)
        assert len(await store.list_facts("u1")) == 2

    @pytest.mark.asyncio
    async def test_session_isolation_holds_across_users(self, store):
        sid_a = await store.resolve_session("u1", "groupA")
        sid_b = await store.resolve_session("u2", "groupA")
        await store.save_fact("u1", "u1 的事实", [1.0, 0.0], session_id=sid_a)
        assert await store.list_facts("u2", session_id=sid_b) == []

    @pytest.mark.asyncio
    async def test_unscoped_query_still_spans_all(self, store):
        # 不传 session_id 时保持旧行为（管理/工具场景）
        sid_a = await store.resolve_session("u1", "groupA")
        sid_b = await store.resolve_session("u1", "groupB")
        await store.save_fact("u1", "A的事实", [1.0, 0.0], session_id=sid_a)
        await store.save_fact("u1", "B的事实", [1.0, 0.0], session_id=sid_b)
        assert sorted(await store.list_facts("u1")) == ["A的事实", "B的事实"]

    @pytest.mark.asyncio
    async def test_reset_keeps_session_identity(self, store):
        # /reset 只清消息、保留 session 身份 → 作用域内的 facts 仍可召回
        sid = await store.resolve_session("u1", "groupA")
        await store.save_fact("u1", "事实", [1.0, 0.0], session_id=sid)
        store.messages.pop(sid, None)
        assert await store.resolve_session("u1", "groupA") == sid
        assert await store.list_facts("u1", session_id=sid) == ["事实"]


class TestDeserializeToolCalls:
    def test_jsonb_string_to_list(self):
        raw = '[{"id": "c1", "function": {"name": "calc", "arguments": "{}"}}]'
        out = _deserialize_tool_calls(raw)
        assert isinstance(out, list)
        assert out[0]["id"] == "c1"

    def test_already_list_passthrough(self):
        data = [{"id": "c1"}]
        assert _deserialize_tool_calls(data) is data

    def test_none_passthrough(self):
        assert _deserialize_tool_calls(None) is None

    def test_invalid_json_returns_none(self):
        assert _deserialize_tool_calls("not-json{{{") is None

    def test_json_object_shape_rejected(self):
        # L5：tool_calls 被写坏成 JSON object（而非数组）→ 按解析失败返回 None
        assert _deserialize_tool_calls('{"id": "c1", "function": {"name": "calc"}}') is None

    def test_non_dict_elements_rejected(self):
        # L5：数组元素非对象（标量/嵌套数组）同样视为坏数据
        assert _deserialize_tool_calls('["not-a-dict"]') is None
        assert _deserialize_tool_calls("[[1, 2]]") is None
        assert _deserialize_tool_calls([{"id": "ok"}, "bad"]) is None

    def test_empty_list_kept(self):
        assert _deserialize_tool_calls("[]") == []


class TestVectorDimOf:
    def test_parse(self):
        assert _vector_dim_of("vector(1024)") == 1024
        assert _vector_dim_of("vector(2048)") == 2048

    def test_unparseable(self):
        assert _vector_dim_of(None) is None
        assert _vector_dim_of("text") is None
        assert _vector_dim_of("") is None


class TestVectorMigrationFlag:
    def test_enabled_variants(self, monkeypatch):
        for v in ("1", "true", "yes", "on", "TRUE"):
            monkeypatch.setenv("AGENT_MIGRATE_VECTOR", v)
            assert _vector_migration_enabled(), v

    def test_disabled_variants(self, monkeypatch):
        for v in ("0", "", "false", "off", "no"):
            monkeypatch.setenv("AGENT_MIGRATE_VECTOR", v)
            assert not _vector_migration_enabled(), repr(v)

    def test_unset_disabled(self, monkeypatch):
        monkeypatch.delenv("AGENT_MIGRATE_VECTOR", raising=False)
        assert not _vector_migration_enabled()


class TestMessagesAfter:
    """M1 存储侧：messages_after 默认排除私聊，供蒸馏取公共（群聊）消息。"""

    @pytest.fixture
    def store(self):
        return InMemoryMemoryStore()

    @pytest.mark.asyncio
    async def test_excludes_private_by_default(self, store):
        sid_g = await store.resolve_session("u1", "g1")
        sid_p = await store.resolve_session("u1", None)
        await store.append_message(sid_g, "user", "群消息")
        await store.append_message(sid_p, "user", "私聊消息")

        rows = await store.messages_after(0)
        assert [r["content"] for r in rows] == ["群消息"]
        assert rows[0]["session_id"] == sid_g
        # 蒸馏输入不带身份信息
        assert "user_id" not in rows[0]

        rows = await store.messages_after(0, include_private=True)
        assert [r["content"] for r in rows] == ["群消息", "私聊消息"]

    @pytest.mark.asyncio
    async def test_incremental_window_and_types(self, store):
        sid_g = await store.resolve_session("u1", "g1")
        for i in range(3):
            await store.append_message(sid_g, "user", f"m{i}")
        latest = await store.latest_message_id()
        assert isinstance(latest, int)
        rows = await store.messages_after(1, limit=2)
        assert [r["id"] for r in rows] == [2, 3]
        assert await store.messages_after(latest) == []
