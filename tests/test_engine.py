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
    async def test_extra_images_append_image_url_content(self, engine):
        engine.llm.responses.append(
            {"choices": [{"message": {"content": "这是一只猫"}}]}
        )
        data_url = "data:image/jpeg;base64,AAAA"
        reply = await engine.run(
            {"user_id": "111"}, "图里是什么", extra_images=[data_url]
        )
        assert reply == "这是一只猫"
        user_msg = engine.llm.calls[0]["messages"][-1]
        assert isinstance(user_msg["content"], list)
        kinds = [c["type"] for c in user_msg["content"]]
        assert kinds == ["text", "image_url"]
        assert user_msg["content"][1]["image_url"]["url"] == data_url

    @pytest.mark.asyncio
    async def test_extra_images_https_url_accepted(self, engine):
        engine.llm.responses.append(
            {"choices": [{"message": {"content": "ok"}}]}
        )
        await engine.run(
            {"user_id": "111"}, "hi", extra_images=["https://gchat.qpic.cn/a.jpg"]
        )
        user_msg = engine.llm.calls[0]["messages"][-1]
        kinds = [c["type"] for c in user_msg["content"]]
        assert "image_url" in kinds

    @pytest.mark.asyncio
    async def test_extra_images_non_uri_ignored(self, engine):
        engine.llm.responses.append(
            {"choices": [{"message": {"content": "ok"}}]}
        )
        await engine.run({"user_id": "111"}, "hi", extra_images=["http://x/y.jpg", "ftp://x"])
        user_msg = engine.llm.calls[0]["messages"][-1]
        assert user_msg["content"] == "hi"

    @pytest.mark.asyncio
    async def test_no_extra_images_keeps_plain_text(self, engine):
        engine.llm.responses.append(
            {"choices": [{"message": {"content": "hi"}}]}
        )
        await engine.run({"user_id": "111"}, "hi")
        user_msg = engine.llm.calls[0]["messages"][-1]
        assert user_msg["content"] == "hi"

    @pytest.mark.asyncio
    async def test_images_not_resent_in_tool_loop(self):
        # M16：tool-loop 后续步骤不再重发图片载荷
        import copy

        class SnapshotLLM(FakeLLM):
            async def chat(self, messages, tools=None):
                self.calls.append({"messages": copy.deepcopy(messages), "tools": tools})
                return self.responses.pop(0)

        llm = SnapshotLLM([])
        skills = SkillRegistry()
        memory = InMemoryMemoryStore()
        engine = AgentEngine(llm, skills, memory)

        async def calc(expr=""):
            return "2"

        skills.register("calc", "计算", {"type": "object"}, permission="public")(calc)
        llm.responses.extend(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "tool_calls": [
                                    {"id": "c1", "function": {"name": "calc", "arguments": "{}"}}
                                ]
                            }
                        }
                    ]
                },
                {"choices": [{"message": {"content": "识别完成"}}]},
            ]
        )
        data_url = "data:image/jpeg;base64," + "A" * 64
        reply = await engine.run({"user_id": "1"}, "图里是什么", extra_images=[data_url])
        assert reply == "识别完成"
        first_user = llm.calls[0]["messages"][-1]
        second_user = next(
            m for m in llm.calls[1]["messages"] if m["role"] == "user" and m is not llm.calls[1]["messages"][0]
        )
        assert isinstance(first_user["content"], list)
        assert any(c["type"] == "image_url" for c in first_user["content"])
        # 第二次调用时图片已降级为纯文本
        assert isinstance(second_user["content"], str)
        assert "A" * 64 not in str(llm.calls[1]["messages"])

    @pytest.mark.asyncio
    async def test_strict_data_uri_validation(self, engine):
        # L15：裸 data: 前缀 / 非 base64 内容不透传
        engine.llm.responses.append({"choices": [{"message": {"content": "ok"}}]})
        await engine.run(
            {"user_id": "1"},
            "hi",
            extra_images=["data:text/html;base64,PGI+", "data:image/jpeg;base64,!!!bad!!!"],
        )
        user_msg = engine.llm.calls[0]["messages"][-1]
        assert user_msg["content"] == "hi"  # 全部非法 → 纯文本

    @pytest.mark.asyncio
    async def test_empty_text_skips_facts_pipeline(self):
        # L4：纯图空文本不应触发 facts 抽取（省 LLM/embedding 调用）
        llm = FakeLLM([{"choices": [{"message": {"content": "ok"}}]}])

        class CountingEmbedding:
            def __init__(self):
                self.embed_calls = 0

            async def embed(self, text):
                self.embed_calls += 1
                return [1.0]

            async def embed_many(self, texts):
                self.embed_calls += 1
                return [[1.0] for _ in texts]

        emb = CountingEmbedding()
        engine = AgentEngine(llm, SkillRegistry(), InMemoryMemoryStore(), embedding=emb)
        await engine.run({"user_id": "1"}, "   ")
        assert emb.embed_calls == 0

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


class TestM2PermissionDenied:
    """M2：「无权限的工具不重试」必须是代码约束，而不是只写在 prompt 里。"""

    @pytest.fixture
    def engine(self):
        llm = FakeLLM([])
        skills = SkillRegistry()
        memory = InMemoryMemoryStore()
        return AgentEngine(llm, skills, memory)

    @staticmethod
    def _tool_call(step: int):
        return {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "id": f"c{step}",
                                "function": {"name": "admin_only", "arguments": "{}"},
                            }
                        ]
                    }
                }
            ]
        }

    @pytest.mark.asyncio
    async def test_denied_skill_not_reexecuted_and_loop_stops(self, engine):
        executed = {"n": 0}
        real_execute = engine.skills.execute

        async def spy(name, **kw):
            executed["n"] += 1
            return await real_execute(name, **kw)

        engine.skills.execute = spy

        async def admin_only():
            return "secret"

        # permission != public 且无 permission_checker → registry 返回 permission denied
        engine.skills.register("admin_only", "仅管理员", {"type": "object"}, permission="superuser")(
            admin_only
        )

        # LLM 连续 4 次重试同一个无权限工具
        engine.llm.responses.extend([self._tool_call(i) for i in range(1, 5)])

        reply = await engine.run({"user_id": "111"}, "帮我执行")

        assert executed["n"] == 1, "无权限的技能只应真正进入权限检查一次，之后必须被短路"
        assert "权限" in reply

    @pytest.mark.asyncio
    async def test_denied_tool_result_tells_model_not_to_retry(self, engine):
        seen_results = []
        real_execute = engine.skills.execute

        async def spy(name, **kw):
            seen_results.append(name)
            return await real_execute(name, **kw)

        engine.skills.execute = spy

        async def admin_only():
            return "secret"

        engine.skills.register("admin_only", "仅管理员", {"type": "object"}, permission="superuser")(
            admin_only
        )
        engine.llm.responses.extend([self._tool_call(1), self._tool_call(2)])
        await engine.run({"user_id": "111"}, "执行")

        # 第二次的工具回包必须是「已确认无权限、请勿重试」，而不是再次真实执行
        tool_msgs = [
            m
            for call in engine.llm.calls
            for m in call["messages"]
            if m.get("role") == "tool"
        ]
        assert any("请勿重试" in m["content"] for m in tool_msgs)
