import pytest

from agentcore.memory.store import InMemoryMemoryStore
from agentcore.memory.store import _deserialize_tool_calls, _vector_dim_of


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
        assert history[0]["tool_call_id"] is None
        assert history[1]["tool_call_id"] == "call_1"

    @pytest.mark.asyncio
    async def test_empty_tool_call_id(self, store):
        sid = await store.resolve_session("u1", None)
        await store.append_message(sid, "tool", "ok", tool_call_id=None)
        history = await store.get_history(sid)
        assert history[0]["tool_call_id"] is None


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


class TestVectorDimOf:
    def test_parse(self):
        assert _vector_dim_of("vector(1024)") == 1024
        assert _vector_dim_of("vector(2048)") == 2048

    def test_unparseable(self):
        assert _vector_dim_of(None) is None
        assert _vector_dim_of("text") is None
        assert _vector_dim_of("") is None
