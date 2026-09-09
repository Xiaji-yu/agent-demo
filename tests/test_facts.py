import pytest

from agentcore.memory.facts import (
    _parse_json_list,
    extract_facts_from_message,
    filter_new_facts,
)


class FakeLLM:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def chat(self, messages, tools=None):
        self.calls.append(messages)
        return self.response


class TestParseJsonList:
    def test_plain_array(self):
        assert _parse_json_list('["a", "b"]') == ["a", "b"]

    def test_code_fence(self):
        raw = '```json\n["用户住在北京", "用户喜欢 Python"]\n```'
        assert _parse_json_list(raw) == ["用户住在北京", "用户喜欢 Python"]

    def test_noise_around(self):
        raw = '好的，抽取结果如下： ["x"] 完'
        assert _parse_json_list(raw) == ["x"]

    def test_invalid_returns_empty(self):
        assert _parse_json_list("not json") == []
        assert _parse_json_list("") == []


class TestExtractFacts:
    @pytest.mark.asyncio
    async def test_extract_ok(self):
        llm = FakeLLM({"choices": [{"message": {"content": '["用户住在北京"]'}}]})
        facts = await extract_facts_from_message(llm, "我在北京工作")
        assert facts == ["用户住在北京"]

    @pytest.mark.asyncio
    async def test_extract_empty(self):
        llm = FakeLLM({"choices": [{"message": {"content": "[]"}}]})
        facts = await extract_facts_from_message(llm, "你好呀")
        assert facts == []

    @pytest.mark.asyncio
    async def test_extract_failure_silent(self):
        async def boom(messages, tools=None):
            raise RuntimeError("x")

        llm = FakeLLM(None)
        llm.chat = boom
        assert await extract_facts_from_message(llm, "hi") == []

    @pytest.mark.asyncio
    async def test_max_facts_limit(self):
        llm = FakeLLM({"choices": [{"message": {"content": '["1","2","3","4","5","6","7","8"]'}}]})
        facts = await extract_facts_from_message(llm, "x", max_facts=3)
        assert len(facts) == 3


class TestFilterNewFacts:
    def test_exact_dup_removed(self):
        out = filter_new_facts(["用户住在北京", "用户喜欢 Python"], ["用户住在北京"])
        assert out == ["用户喜欢 Python"]

    def test_substring_dup_removed(self):
        out = filter_new_facts(["北京", "上海"], ["用户住在北京"])
        assert out == ["上海"]
