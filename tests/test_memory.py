import pytest

from agentcore.memory.store import InMemoryMemoryStore


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
