"""agentcore/render/table：MD 表格提取与 PNG 渲染。

背景：QQ 聊天框不渲染 Markdown——表格以纯文本发出时竖线错位、观感极差
（用户实测：对比类回答整段不可读）。这里锁定「提取」与「渲染」两层行为：
提取错（漏提/误提）会直接体现在投递结果上；渲染必须有中文字体兜底与
降级路径（无字体 → None，调用方降级为原文本）。
"""

import io

from PIL import Image

from agentcore.render.table import render_table_png, split_tables

TABLE = """| 职业 | 圣聆初雪 | 结城理 |
|---|---|---|
| 定位 | 六星阵法术师 | 六星特种 |
| 机制 | 暖机型 | 替身切换 |"""

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class TestSplitTables:
    def test_standard_table_extracted(self):
        text = f"简单说：初雪是启动慢的控制炮。\n\n{TABLE}\n\n其他内容。"
        tables, rest = split_tables(text)
        assert len(tables) == 1
        assert tables[0].startswith("| 职业")
        assert "简单说" in rest
        assert "其他内容" in rest
        assert "|" not in rest

    def test_no_table_returns_text_as_is(self):
        text = "普通回复，没有表格。"
        tables, rest = split_tables(text)
        assert tables == []
        assert rest == text

    def test_single_pipe_line_not_a_table(self):
        """单行 | 不认：防误伤（正文里的竖线并不罕见）。"""
        tables, rest = split_tables("a | b | c")
        assert tables == []
        assert rest == "a | b | c"

    def test_table_without_separator_row_not_extracted(self):
        """缺分隔行（第二行不是 |---|）不认——那是普通的多竖线文本。"""
        text = "| a | b |\n| 1 | 2 |"
        tables, _ = split_tables(text)
        assert tables == []

    def test_multiple_tables(self):
        text = (
            "前言\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n中场\n\n"
            "| x | y |\n|---|---|\n| 3 | 4 |\n\n结语"
        )
        tables, rest = split_tables(text)
        assert len(tables) == 2
        assert "前言" in rest and "中场" in rest and "结语" in rest
        assert "|" not in rest

    def test_empty_text(self):
        assert split_tables("") == ([], "")


class TestRenderTablePng:
    def test_renders_png_bytes(self):
        png = render_table_png(TABLE)
        assert png is not None
        assert png[:8] == PNG_MAGIC

    def test_chinese_table_renders_with_font(self):
        png = render_table_png(TABLE)
        img = Image.open(io.BytesIO(png))
        assert img.format == "PNG"
        assert img.size[0] > 100 and img.size[1] > 50

    def test_font_missing_returns_none(self, tmp_path):
        """无中文字体 → None：调用方据此降级为纯文本（不发豆腐块图）。"""
        png = render_table_png(TABLE, font_path=tmp_path / "nonexistent.ttc")
        assert png is None

    def test_garbage_table_returns_none(self):
        assert render_table_png("") is None
