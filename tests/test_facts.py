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
        llm = FakeLLM(
            {"choices": [{"message": {"content": '["1","2","3","4","5","6","7","8"]'}}]}
        )
        facts = await extract_facts_from_message(llm, "x", max_facts=3)
        assert len(facts) == 3


class TestFilterNewFacts:
    def test_exact_dup_removed(self):
        out = filter_new_facts(["用户住在北京", "用户喜欢 Python"], ["用户住在北京"])
        assert out == ["用户喜欢 Python"]

    def test_substring_dup_removed(self):
        out = filter_new_facts(["北京", "上海"], ["用户住在北京"])
        assert out == ["上海"]


class TestTransientFactFilter:
    def test_image_and_file_facts_dropped(self):
        from agentcore.memory.facts import is_transient_fact

        for junk in [
            "用户询问该图片上的文字内容",
            "用户上传的图片已保存到工作区media/35ab8ba112ed.jpg路径",
            "用户上传的图片已保存至工作区路径media/5daf2293d3cf.jpg",
            "用户发送了一张截图",
            "用户提供了文件路径 /tmp/a.png",
        ]:
            assert is_transient_fact(junk), junk

    def test_normal_facts_kept(self):
        from agentcore.memory.facts import is_transient_fact

        for good in [
            "用户住在北京",
            "用户喜欢喝茉莉奶绿",
            "用户明确禁止涉及色色相关内容",
            "用户调用小维",
            "用户的出生时间为农历2001年6月21日晚十点",
        ]:
            assert not is_transient_fact(good), good

    @pytest.mark.asyncio
    async def test_extract_filters_transient(self):
        llm = FakeLLM(
            {
                "choices": [
                    {
                        "message": {
                            "content": '["用户住在北京", "用户询问该图片上的文字内容"]'
                        }
                    }
                ]
            }
        )
        facts = await extract_facts_from_message(llm, "我在北京，这图上写的啥")
        assert facts == ["用户住在北京"]

    def test_prompt_mentions_image_exclusion(self):
        from agentcore.memory.facts import EXTRACT_PROMPT

        assert "图片" in EXTRACT_PROMPT and "不要" in EXTRACT_PROMPT
