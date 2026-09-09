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


class FakeEmbedding:
    async def embed(self, text):
        return [1.0, float(len(text))]

    async def embed_many(self, texts):
        return [[1.0, float(len(t))] for t in texts]


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
    async def test_empty_content_retry_then_reply(self, engine):
        # 第一次空输出后应重试，第二次给出内容则成功返回
        engine.llm.responses.extend(
            [
                {"choices": [{"message": {"content": ""}}]},
                {"choices": [{"message": {"content": "好的，我明白了"}}]},
            ]
        )
        reply = await engine.run({"user_id": "111"}, "hi")
        assert reply == "好的，我明白了"
        assert len(engine.llm.calls) == 2

    @pytest.mark.asyncio
    async def test_empty_content_fallback(self, engine):
        # 连续多次空输出（超过重试上限）才返回占位提示；
        # 断言确实重试了 3 次，且重试前注入了 nudge 提示
        engine.llm.responses.extend(
            [
                {"choices": [{"message": {"content": ""}}]},
                {"choices": [{"message": {"content": ""}}]},
                {"choices": [{"message": {"content": ""}}]},
            ]
        )
        reply = await engine.run({"user_id": "111"}, "hi")
        assert "空内容" in reply
        assert len(engine.llm.calls) == 3
        nudge = [
            m
            for m in engine.llm.calls[1]["messages"]
            if m.get("role") == "user" and "没有输出任何内容" in m.get("content", "")
        ]
        assert nudge, "retry should inject an empty-output nudge prompt"

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

    @pytest.mark.asyncio
    async def test_safe_text_keeps_newlines(self, engine):
        raw = "第一行\n第二行\r\n**加粗**\t结尾"
        cleaned = engine._safe_text(raw)
        assert "\n" in cleaned
        assert "\t" in cleaned
        assert "第一行\n第二行" in cleaned

    @pytest.mark.asyncio
    async def test_safe_text_strips_real_control_chars(self, engine):
        cleaned = engine._safe_text("a\x00b\x07c\x1b d\x7fe")
        assert cleaned == "abc de"
        assert "\x00" not in cleaned and "\x07" not in cleaned and "\x7f" not in cleaned

    @pytest.mark.asyncio
    async def test_no_embedding_no_facts(self, engine):
        engine.llm.responses.append(
            {"choices": [{"message": {"content": "hi"}}]}
        )
        await engine.run({"user_id": "111"}, "我叫小明，住在北京")
        assert await engine.memory.list_facts("111") == []
        assert len(engine.llm.calls) == 1

    @pytest.mark.asyncio
    async def test_remember_and_recall_facts(self):
        llm = FakeLLM(
            [
                {"choices": [{"message": {"content": '["用户住在北京"]'}}]},
                {"choices": [{"message": {"content": "记住了"}}]},
            ]
        )
        skills = SkillRegistry()
        memory = InMemoryMemoryStore()
        engine = AgentEngine(
            llm,
            skills,
            memory,
            config={"memory_facts_threshold": 0.0},
            embedding=FakeEmbedding(),
        )
        reply = await engine.run({"user_id": "111"}, "我叫小明，住在北京")
        assert reply == "记住了"
        facts = await memory.list_facts("111")
        assert any("北京" in f for f in facts)
        # 第二次 LLM 调用的 system prompt 应包含召回的长期记忆
        second_prompt = llm.calls[1]["messages"][0]["content"]
        assert "用户住在北京" in second_prompt

    @pytest.mark.asyncio
    async def test_remember_dedup(self):
        llm = FakeLLM(
            [
                {"choices": [{"message": {"content": '["用户住在北京"]'}}]},
                {"choices": [{"message": {"content": "ok1"}}]},
                {"choices": [{"message": {"content": '["用户住在北京"]'}}]},
                {"choices": [{"message": {"content": "ok2"}}]},
            ]
        )
        skills = SkillRegistry()
        memory = InMemoryMemoryStore()
        engine = AgentEngine(
            llm,
            skills,
            memory,
            config={"memory_facts_threshold": 0.0},
            embedding=FakeEmbedding(),
        )
        await engine.run({"user_id": "111"}, "我住北京")
        await engine.run({"user_id": "111"}, "我住北京")
        facts = await memory.list_facts("111")
        assert len(facts) == 1

    @pytest.mark.asyncio
    async def test_persona_injected_into_system_prompt(self, tmp_path):
        from agentcore.personas import PersonaManager

        (tmp_path / "a.md").write_text(
            "---\nname: default_p\ndefault: true\n---\nDEFAULT_BODY", encoding="utf-8"
        )
        (tmp_path / "f.md").write_text(
            "---\nname: fortune\n---\nFORTUNE_BODY", encoding="utf-8"
        )
        pm = PersonaManager(tmp_path)

        llm = FakeLLM([{"choices": [{"message": {"content": "好的"}}]}])
        skills = SkillRegistry()
        memory = InMemoryMemoryStore()
        engine = AgentEngine(llm, skills, memory, persona_manager=pm)
        # 未设置 → 用默认人格
        await engine.run({"user_id": "111"}, "hi")
        assert "DEFAULT_BODY" in llm.calls[0]["messages"][0]["content"]
        assert "FORTUNE_BODY" not in llm.calls[0]["messages"][0]["content"]

        # 切到 fortune → 注入 fortune body
        llm.responses.append({"choices": [{"message": {"content": "好的"}}]})
        await memory.set_user_persona("111", "fortune")
        await engine.run({"user_id": "111"}, "hi")
        prompt = llm.calls[1]["messages"][0]["content"]
        assert "FORTUNE_BODY" in prompt
        assert "DEFAULT_BODY" not in prompt

        # 存的 persona 名已失效 → 回退默认人格而非空白
        llm.responses.append({"choices": [{"message": {"content": "好的"}}]})
        await memory.set_user_persona("111", "gone_persona")
        await engine.run({"user_id": "111"}, "hi")
        prompt = llm.calls[2]["messages"][0]["content"]
        assert "DEFAULT_BODY" in prompt

        # 新加的 md 在 refresh 后可被看到（TTL 由 manager 处理，这里直接验证 get 能取到新文件）
        (tmp_path / "g.md").write_text(
            "---\nname: newbie\n---\nNEW_BODY", encoding="utf-8"
        )
        pm.refresh()
        assert pm.get("newbie") is not None
