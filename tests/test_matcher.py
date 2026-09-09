import re

from plugins.qq_agent_adapter.matcher import _split_qq_message, _truncate


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
        assert _split_qq_message("") == [""]

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
