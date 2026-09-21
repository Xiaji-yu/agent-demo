"""current_model skill：让模型准确回答"你在用哪个模型"。

来源：用户需求「可以通过对话让他知道自己用的哪个模型」。
模型对自己型号的自我认知不可靠（会按训练知识自称），且主备切换是运行期动态的，
所以准信只能由 skill 在**被问到时**向 LLMClient 要。
"""

import pytest

from agentcore.skills.llm_info import register_llm_info_skills
from agentcore.skills.registry import SkillRegistry


class _FakeLLM:
    def __init__(self, status=None, boom=False):
        self._status = status or {}
        self._boom = boom

    def model_status(self):
        if self._boom:
            raise RuntimeError("status unavailable")
        return self._status


def _skill(llm) -> "object":
    reg = SkillRegistry()
    register_llm_info_skills(reg, llm)
    return reg.skills["current_model"]


_PRIMARY = {
    "active": "step-3.7-flash",
    "line": "primary",
    "primary": "step-3.7-flash",
    "fallback": "step-2-mini",
}
_FALLBACK = {
    "active": "step-2-mini",
    "line": "fallback",
    "primary": "step-3.7-flash",
    "fallback": "step-2-mini",
}


class TestRegistration:
    def test_registered_public_with_empty_params(self):
        sk = _skill(_FakeLLM(_PRIMARY))
        assert sk.permission == "public", "群里任何人都可能问，不能设权限门"
        assert sk.params_schema.get("properties") == {}
        assert sk.params_schema.get("required") == []

    def test_description_forbids_self_claim(self):
        """描述必须明确禁止模型按训练知识自称——这是本 skill 存在的理由。"""
        sk = _skill(_FakeLLM(_PRIMARY))
        desc = sk.description
        assert "不要" in desc and "猜测" in desc
        assert "current_model" in desc or "本 skill" in desc


class TestHandler:
    @pytest.mark.asyncio
    async def test_primary_line_text(self):
        text = await _skill(_FakeLLM(_PRIMARY)).handler()
        assert "step-3.7-flash" in text
        assert "主线路" in text
        assert "step-2-mini" in text, "配置了备用就该一并说明"

    @pytest.mark.asyncio
    async def test_fallback_line_text(self):
        text = await _skill(_FakeLLM(_FALLBACK)).handler()
        assert "step-2-mini" in text and "备用线路" in text
        assert "step-3.7-flash" in text, "要说明主模型是谁、当前不可用"

    @pytest.mark.asyncio
    async def test_no_fallback_configured(self):
        status = {"active": "m1", "line": "primary", "primary": "m1", "fallback": ""}
        text = await _skill(_FakeLLM(status)).handler()
        assert "未配置备用模型" in text

    @pytest.mark.asyncio
    async def test_status_failure_returns_error_not_crash(self):
        text = await _skill(_FakeLLM(boom=True)).handler()
        assert text.startswith("Error:"), "读不到状态要给模型可转述的错误，而不是抛异常"

    @pytest.mark.asyncio
    async def test_handler_takes_no_arguments(self):
        """registry.execute 会按签名注入 user_id/group_id；本 skill 不需要它们，
        签名必须能不传参调用（否则群/私聊上下文注入会变成 TypeError）。"""
        sk = _skill(_FakeLLM(_PRIMARY))
        import inspect

        params = inspect.signature(sk.handler).parameters
        assert list(params) == [], f"不应需要参数：{list(params)}"
