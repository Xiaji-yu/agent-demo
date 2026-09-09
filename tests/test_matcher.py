import re

from plugins.qq_agent_adapter.matcher import _qq_plain, _split_qq_message, _truncate


class TestQQPlain:
    def test_bold_stripped(self):
        assert _qq_plain("**年柱**：辛巳") == "年柱：辛巳"

    def test_heading_stripped(self):
        assert _qq_plain("### 标题\n正文") == "标题\n正文"

    def test_quote_stripped(self):
        assert _qq_plain("> 引用") == "引用"

    def test_list_dash_normalized(self):
        assert _qq_plain("- a\n* b") == "- a\n- b"

    def test_inline_code_stripped(self):
        assert _qq_plain("运行 `python bot.py`") == "运行 python bot.py"

    def test_link_kept(self):
        out = _qq_plain("[百度](https://www.baidu.com)")
        assert "百度" in out and "https://www.baidu.com" in out and "[" not in out

    def test_keeps_newlines(self):
        out = _qq_plain("第一行\n第二行\n第三行")
        assert out == "第一行\n第二行\n第三行"

    def test_empty(self):
        assert _qq_plain("") == ""
        assert _qq_plain(None) is None


class TestMatcherUtils:
    def test_truncate_short(self):
        assert _truncate("hello") == "hello"

    def test_truncate_exact(self):
        assert _truncate("hello", 5) == "hello"

    def test_truncate_long(self):
        text = "a" * 300
        result = _truncate(text, 200)
        assert len(result) == 203
        assert result.endswith("...")

    def test_split_short(self):
        assert _split_qq_message("hello") == ["hello"]

    def test_split_long_with_breaks(self):
        text = "第一句。第二句。第三句。"
        result = _split_qq_message(text, max_len=10)
        assert len(result) > 1

    def test_split_empty(self):
        assert _split_qq_message("") == []

    def test_split_skips_whitespace_only_chunks(self):
        text = "w" + " " * 500 + "w"
        chunks = _split_qq_message(text, max_len=100)
        assert chunks
        assert all(c.strip() for c in chunks)

    def test_qq_plain_fenced_code_untouched(self):
        code = "```python\n# 注释\ns = '**hi**'\n```"
        out = _qq_plain(code)
        assert "# 注释" in out
        assert "**hi**" in out  # 代码块内不做 md 改写

    def test_qq_plain_double_underscore_bold(self):
        assert _qq_plain("a __b__ c") == "a b c"

    def test_split_keeps_newlines_in_chunk(self):
        text = "第一行\n第二行\n第三行"
        result = _split_qq_message(text, max_len=50)
        assert result == [text]
        assert "\n" in result[0]

    def test_split_long_md_cuts_at_punct_not_mid_word(self):
        # 无空格无换行的超长 md 连续文本，应在中文标点后断行
        seg = "**年柱**：辛巳（白蜡金），**月柱**：丙申（山下火）。" * 8
        chunks = _split_qq_message(seg, max_len=40)
        assert len(chunks) > 1
        joined = "".join(chunks)
        # 切割不丢字符
        assert joined.replace(" ", "") == seg.replace(" ", "")
        # 断点尽量在标点/换行处，而不是任意截断
        for c in chunks[:-1]:
            assert c[-1] in "。，；、！？：\n" or c.endswith("）")
