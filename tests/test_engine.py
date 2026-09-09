import pytest

from agentcore.loop.engine import AgentEngine
from agentcore.skills.registry import SkillRegistry
from agentcore.memory.store import InMemoryMemoryStore


class FakeLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def chat(self, messages, tools=None):
        self.calls.append({"messages": messages, "tools": tools})
        return self.responses.pop(0)


class TestAgentEngine:
    @pytest.fixture
    def engine(self):
        llm = FakeLLM([])
        skills = SkillRegistry()
        memory = InMemoryMemoryStore()
        return AgentEngine(llm, skills, memory)

    @pytest.mark.asyncio
    async def test_simple_reply(self, engine):
        engine.llm.responses.append(
            {"choices": [{"message": {"content": "hello"}}]}
        )
        reply = await engine.run({"user_id": "111"}, "hi")
        assert reply == "hello"
        assert len(engine.llm.calls) == 1

    @pytest.mark.asyncio
    async def test_tool_call_loop(self, engine):
        engine.llm.responses.extend(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "function": {
                                            "name": "calc",
                                            "arguments": '{"expr": "1+1"}',
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                },
                {"choices": [{"message": {"content": "结果是 2"}}]},
            ]
        )

        async def calc(expr=""):
            return "2"

        engine.skills.register("calc", "计算", {"type": "object"}, permission="public")(
            calc
        )

        reply = await engine.run({"user_id": "111"}, "算一下 1+1")
        assert reply == "结果是 2"
        assert len(engine.llm.calls) == 2

    @pytest.mark.asyncio
    async def test_empty_content_fallback(self, engine):
        engine.llm.responses.append(
            {"choices": [{"message": {"content": ""}}]}
        )
        reply = await engine.run({"user_id": "111"}, "hi")
        assert "空内容" in reply

    @pytest.mark.asyncio
    async def test_llm_failure(self, engine):
        engine.llm.responses.append(None)

        async def bad_chat(messages, tools=None):
            raise RuntimeError("boom")

        engine.llm.chat = bad_chat
        reply = await engine.run({"user_id": "111"}, "hi")
        assert "失败" in reply

    @pytest.mark.asyncio
    async def test_user_id_sanitized_in_prompt(self, engine):
        engine.llm.responses.append(
            {"choices": [{"message": {"content": "ok"}}]}
        )
        await engine.run({"user_id": "111\nbad", "group_id": "g1"}, "hi")
        prompt = engine.llm.calls[0]["messages"][0]["content"]
        assert "111\nbad" not in prompt
        assert "111bad" in prompt
