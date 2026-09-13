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
