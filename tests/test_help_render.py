"""帮助菜单图片渲染：PNG 产出、开关与降级路径。"""

import pytest

from plugins.qq_agent_adapter import help_render


def test_renders_png_when_font_available():
    if help_render._load_font(25) is None:
        pytest.skip("环境无 CJK 字体")
    png = help_render.render_help_image()
    assert png is not None
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png) > 10_000


def test_env_toggle_disables(monkeypatch):
    monkeypatch.setenv("AGENT_HELP_IMAGE", "0")
    assert help_render.render_help_image() is None


def test_no_font_returns_none(monkeypatch):
    monkeypatch.setattr(help_render, "_FONT_CANDIDATES", ())
    assert help_render.render_help_image() is None


def test_missing_pillow_returns_none(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _no_pil(name, *args, **kwargs):
        if name == "PIL" or name.startswith("PIL."):
            raise ModuleNotFoundError("PIL")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_pil)
    monkeypatch.setenv("AGENT_HELP_IMAGE", "1")
    assert help_render.render_help_image() is None


# 来源: REVIEW-c472e56..733f57e L5/L7
class TestReviewC472HelpRender:
    def test_empty_env_value_means_disabled(self, monkeypatch):
        """L7：显式设置 AGENT_HELP_IMAGE=（空值）按「设了但无效」处理 → 关闭。
        旧实现 `or "1"` 会把空值变成开启。"""
        monkeypatch.setenv("AGENT_HELP_IMAGE", "")
        assert help_render.image_enabled() is False

    def test_unset_env_means_enabled(self, monkeypatch):
        monkeypatch.delenv("AGENT_HELP_IMAGE", raising=False)
        assert help_render.image_enabled() is True

    def test_render_body_exception_swallows_to_none(self, monkeypatch):
        """L5：渲染主体异常在 render_help_image 内吞掉返回 None——docstring
        声称的兜底必须真实存在，任何新调用点不必自带 try/except。"""
        pytest.importorskip("PIL")
        from PIL import ImageDraw

        def boom(*args, **kwargs):
            raise RuntimeError("draw failed")

        monkeypatch.setattr(help_render, "_load_font", lambda size: object())
        monkeypatch.setattr(ImageDraw, "Draw", boom)
        assert help_render.render_help_image() is None
