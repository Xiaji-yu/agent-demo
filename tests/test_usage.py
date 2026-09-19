"""用量统计命令（/usage）：表格生成 + 边界。"""

import pytest


@pytest.fixture(scope="module")
def nb_driver():
    import nonebot

    # admin.py 的 on_command 构造需要 NoneBot 已初始化（同 test_admin_import）
    nonebot.init(_env_file=None, superusers={"10000"})
    from nonebot import get_driver

    return get_driver()


@pytest.fixture(scope="module")
def render_table(nb_driver):
    """NoneBot 初始化后才能 import admin（on_command 构造）。"""
    from plugins.qq_agent_adapter.admin import _render_usage_table

    return _render_usage_table


from agentcore.budget import CostBudget  # noqa: E402


def _seeded(tmp_path):
    b = CostBudget(root=tmp_path)
    b.record("chat", 3000, 2000, model="step-3.7-flash", route="group:123")
    b.record("chat", 1000, 500, model="step-3.7-flash", route="private:456")
    b.record("chat", 100, 50, model="fallback-model", route="group:123")
    b.record("embedding", 500)
    return b


@pytest.mark.usefixtures("nb_driver")
class TestRenderUsageTable:
    def test_contains_today_and_total(self, tmp_path, render_table):
        table = render_table(_seeded(tmp_path))
        assert "# 用量统计" in table
        assert "| 对话 token | 6,650 | 6,650 |" in table
        assert "| embedding token | 500 | 500 |" in table

    def test_route_and_model_sections(self, tmp_path, render_table):
        table = render_table(_seeded(tmp_path))
        assert "## 今日按路由" in table
        assert "group:123" in table
        assert "private:456" in table
        assert "## 今日按模型" in table
        assert "step-3.7-flash" in table
        assert "fallback-model" in table  # fallback 模型名自然区分

    def test_empty_budget_renders_without_sections(self, tmp_path, render_table):
        table = render_table(CostBudget(root=tmp_path))
        assert "# 用量统计" in table
        assert "| 对话 token | 0 | 0 |" in table
        assert "## 今日按路由" not in table  # 空明细不渲染章节

    def test_cost_shown_when_priced(self, tmp_path, render_table):
        b = CostBudget(
            root=tmp_path, price_prompt_per_m=1.0, price_completion_per_m=3.0
        )
        b.record("chat", 1_000_000, 0)
        table = render_table(b)
        assert "估算成本" in table
        assert "≈ 1.0000 元" in table
