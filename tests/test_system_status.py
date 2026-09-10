import pytest

from agentcore.skills.registry import SkillRegistry
from agentcore.skills.system_status import (
    _resolve_items,
    register_system_skills,
    run_system_status,
)


class TestSystemStatus:
    def test_register(self):
        reg = SkillRegistry()
        register_system_skills(reg)
        assert "system_status" in reg.skills
        schema = reg.skills["system_status"].to_openai_schema()
        assert schema["function"]["name"] == "system_status"

    def test_resolve_items(self):
        all_items = ["os", "cpu", "mem", "disk", "proc", "gpu", "docker"]
        assert _resolve_items("") == all_items
        assert _resolve_items("all") == all_items
        assert _resolve_items("cpu,mem") == ["cpu", "mem"]
        assert _resolve_items("bogus") == []

    @pytest.mark.asyncio
    async def test_run_os_item(self):
        out = await run_system_status("os")
        assert "系统" in out
        assert len(out) > 5

    @pytest.mark.asyncio
    async def test_run_invalid_item(self):
        out = await run_system_status("bogus")
        assert "支持的查询项" in out

    @pytest.mark.asyncio
    async def test_run_all_fast(self):
        # 全量查询需在超时内完成（各命令 5s 上限），输出非空
        out = await run_system_status("cpu,mem")
        assert "CPU" in out or "内存" in out
