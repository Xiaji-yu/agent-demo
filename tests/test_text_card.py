"""文字卡片渲染：产出尺寸、门禁截断与降级路径。

重点抓「改坏实现必然失败」：
- 正常内容必须真的画出墨色像素（不是空白图）
- 超过软上限/高度上限必须 truncated 且高度不越界
- 无字体 / 空内容必须返回明确 reason，而不是抛异常
"""

from __future__ import annotations

import io

import pytest

from agentcore.render import text_card as tc

pytest.importorskip("PIL")

from PIL import Image, ImageDraw  # noqa: E402


def _open(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png))


def _has_ink(img: Image.Image, threshold: int = 0x40) -> bool:
    return img.convert("L").getextrema()[0] < threshold


class TestRenderTextCard:
    def test_basic_render_has_ink_and_expected_size(self):
        result = tc.render_text_card_png(
            "第一行\n第二行", title="测试标题", now=1790026700.0
        )
        assert result.png is not None
        assert result.truncated is False
        assert result.reason == ""
        img = _open(result.png)
        assert img.width == tc.W
        assert img.height <= tc.MAX_IMAGE_HEIGHT
        assert img.width * img.height <= tc.MAX_PIXELS
        assert _has_ink(img), "标题/正文没有画出墨色像素，等于空白图"

    def test_empty_content_returns_reason_not_exception(self):
        result = tc.render_text_card_png("   \n\t  ")
        assert result.png is None
        assert result.reason == "empty"

    def test_no_font_returns_reason(self, monkeypatch):
        monkeypatch.setattr(tc, "ensure_font_probed", lambda: False)
        result = tc.render_text_card_png("正文")
        assert result.png is None
        assert result.reason == "no_font"

    def test_soft_limit_truncates_and_marks(self):
        body = "字" * (tc.MAX_TEXT_CHARS_SOFT + 500)
        result = tc.render_text_card_png(body, now=1790026700.0)
        assert result.png is not None
        assert result.truncated is True
        assert result.reason == "too_long"
        assert result.chars == len(body)
        assert _open(result.png).height <= tc.MAX_IMAGE_HEIGHT

    def test_height_limit_truncates(self):
        # 行数远超画布高度，但总字数低于软上限 → 必须由高度门禁截断
        body = "\n".join("行" for _ in range(1000))
        result = tc.render_text_card_png(body, now=1790026700.0)
        assert result.png is not None
        assert result.truncated is True
        assert result.reason == "too_large"
        assert _open(result.png).height <= tc.MAX_IMAGE_HEIGHT

    def test_control_chars_are_cleaned(self):
        result = tc.render_text_card_png("a\x00b\x07c\x7f", now=1790026700.0)
        assert result.png is not None, "控制字符不得让渲染失败"

    def test_overlong_title_is_fitted(self):
        result = tc.render_text_card_png("正文", title="标" * 200, now=1790026700.0)
        assert result.png is not None
        assert _open(result.png).width == tc.W


class TestWrap:
    def test_cjk_lines_never_exceed_max_width(self):
        f = tc._font(tc.BODY_SIZE)
        draw = ImageDraw.Draw(Image.new("RGB", (8, 8)))
        max_w = 400
        lines = tc._wrap(draw, "中文测试" * 100, f, max_w)
        assert len(lines) > 1
        assert all(draw.textlength(line, font=f) <= max_w for line in lines)

    def test_explicit_newlines_are_preserved(self):
        f = tc._font(tc.BODY_SIZE)
        draw = ImageDraw.Draw(Image.new("RGB", (8, 8)))
        lines = tc._wrap(draw, "a\n\nb", f, 400)
        assert lines == ["a", "", "b"]


class TestTruncateChars:
    def test_prefers_line_break(self):
        # 换行落在截断点后 20% 内时才回退到换行处（避免为了断行丢掉大半内容）
        text = "长" * 45 + "\n" + "长" * 100
        cut = tc._truncate_chars(text, 50)
        assert cut == "长" * 45

    def test_no_line_break_falls_back_to_exact_limit(self):
        text = "长" * 100
        cut = tc._truncate_chars(text, 50)
        assert len(cut) == 50


class TestFooterNote:
    """M17（REVIEW-1b16fa7..9c2d3c1）：页脚「已截断」标注必须覆盖软上限路径。

    半角输入 7000 字 → 软上限截到 6000 → 仅 82 行 < 119 行高度上限 →
    cut_by_height=False。旧条件只看 cut_by_height，图内无任何截断标注、
    页脚还显示截断后的「6000 字」——与「超出截断并在图内标注」声称不符。
    PNG 不可 OCR，故钉纯函数；渲染侧截断行为由 TestRenderTextCard 锁。
    """

    def test_soft_limit_alone_annotates(self):
        assert tc._footer_note(True, False, 6000) == "已截断"

    def test_height_cut_alone_annotates(self):
        assert tc._footer_note(False, True, 5000) == "已截断"

    def test_both_annotate(self):
        assert tc._footer_note(True, True, 6000) == "已截断"

    def test_no_truncation_shows_count(self):
        assert tc._footer_note(False, False, 4321) == "4321 字"

    def test_half_width_7000_truncates_without_height_cut(self):
        """端到端确认该路径存在：truncated=True 且没有高度截断（无标注漏网）。"""
        r = tc.render_text_card_png("a" * 7000, "t")
        assert r.truncated is True
        assert r.reason == "too_long"
