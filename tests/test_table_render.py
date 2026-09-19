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


def _png_height(png: bytes) -> int:
    """从 IHDR 直接读 PNG 高度（避免为了断言引入额外解码）。"""
    import struct

    return struct.unpack(">I", png[20:24])[0]


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


class TestRenderGuards:
    """评审 M-1：规模护栏（列/行/单元格上限）——零护栏时 1601 列崩溃 ValueError、
    单格 1000 字渲染 24.6s（O(n²) 逐字符截断）。"""

    def test_too_many_columns_returns_none(self):
        n = 25  # > _MAX_COLS (20)
        tbl = (
            "|" + "|".join(f"c{i}" for i in range(n)) + "|\n"
            "|" + "|".join("---" for _ in range(n)) + "|\n"
            "|" + "|".join("v" for _ in range(n)) + "|\n"
        )
        assert render_table_png(tbl) is None

    def test_too_many_rows_truncated_with_note(self):
        """L23（REVIEW-6ec3f7c..a36ea1d）：原断言（png is not None + magic）在
        **删掉行截断后照样通过**，等于没有护栏。改为断言"截断后高度显著更小"，
        并直接检查注释文本被真正绘制。"""
        from agentcore.render.table import _MAX_ROWS

        def build(n):
            rows = ["| 列A | 列B |", "|---|---|"]
            for i in range(n):
                rows.append(f"| r{i} | v{i} |")
            return "\n".join(rows)

        capped = render_table_png(build(_MAX_ROWS + 30))
        assert capped is not None  # 截断而非拒绝
        assert capped[:8] == PNG_MAGIC

        # 高度必须由"实际渲染行数"决定：放开护栏时高度应明显更大
        import agentcore.render.table as tbl_mod

        orig = tbl_mod._MAX_ROWS
        try:
            tbl_mod._MAX_ROWS = _MAX_ROWS + 30
            uncapped = render_table_png(build(_MAX_ROWS + 30))
        finally:
            tbl_mod._MAX_ROWS = orig
        assert uncapped is not None
        assert _png_height(uncapped) > _png_height(capped), (
            "行护栏失效时高度不变 → 本用例应当失败"
        )

    def test_row_truncation_note_is_drawn(self, monkeypatch):
        """注释行必须真的画出来（截断要"注明"，不能静默丢行）。"""
        from agentcore.render.table import _MAX_ROWS

        rows = ["| 列A | 列B |", "|---|---|"]
        for i in range(_MAX_ROWS + 30):
            rows.append(f"| r{i} | v{i} |")

        drawn: list[str] = []
        import PIL.ImageDraw as _ImageDraw

        real_text = _ImageDraw.ImageDraw.text

        def spy_text(self, xy, text, *a, **kw):
            drawn.append(str(text))
            return real_text(self, xy, text, *a, **kw)

        monkeypatch.setattr(_ImageDraw.ImageDraw, "text", spy_text)
        assert render_table_png("\n".join(rows)) is not None
        assert any("仅显示前" in t for t in drawn), f"未绘制截断注释：{drawn[-5:]}"

    def test_overlong_cell_truncated(self):
        from agentcore.render.table import _MAX_CELL_CHARS

        long_cell = "长" * (_MAX_CELL_CHARS + 500)
        tbl = f"| 表头 |\n|---|\n| {long_cell} |"
        png = render_table_png(tbl)
        assert png is not None  # 渲染成功（截断加省略号），不再 O(n²) 挂住

    def test_fit_text_binary_search(self):
        from agentcore.render.table import _fit_text, _load_font

        font = _load_font(16)
        short = "你好"
        assert _fit_text(font, short, 1000) == short  # 不超宽原样
        fitted = _fit_text(font, "长" * 100, 200)
        assert fitted.endswith("…")
        assert len(fitted) < 100  # 确实截了

    def test_ensure_font_probed_caches(self):
        """探测只做一次（评审 L-4：不逐条刷 warning）。"""
        import agentcore.render.table as t

        t._font_probe_done = False
        first = t.ensure_font_probed()
        second = t.ensure_font_probed()
        assert first == second
        assert t._font_probe_done is True


class TestTitleRows:
    """`#` 标题行：解析为独立标题（不混进表格行），渲染为标题条。

    背景：/usage 统计表带 `#`/`##` 小节标题，此前被当成单格行渲染成
    孤立的错位行；顺带给渲染加层次（深色表头/斑马纹），回应"画面太素"。
    """

    def test_parse_separates_titles_from_rows(self):
        from agentcore.render.table import _parse_rows

        titles, rows = _parse_rows(
            "# 用量统计\n| a | b |\n|---|---|\n| 1 | 2 |\n## 按路由\n| r | n |\n|---|---|\n| g1 | 3 |"
        )
        assert titles == ["用量统计", "按路由"]
        assert rows == [["a", "b"], ["1", "2"], ["r", "n"], ["g1", "3"]]

    def test_renders_with_titles(self):
        png = render_table_png("# 标题\n" + TABLE)
        assert png is not None
        assert png[:8] == PNG_MAGIC

    def test_title_increases_height(self):
        """带标题的图比纯表格高（标题条占位）——结构断言，非恒真。"""
        base = Image.open(io.BytesIO(render_table_png(TABLE)))
        with_title = Image.open(io.BytesIO(render_table_png("# 标题\n" + TABLE)))
        assert with_title.size[1] > base.size[1]
        assert with_title.size[0] == base.size[0]  # 标题不改变列宽

    def test_titles_only_returns_none(self):
        """只有标题没有表格：不算表格，返回 None（调用方降级纯文本）。"""
        assert render_table_png("# 只是一个标题\n## 没有表格") is None

    def test_visual_styling_pixels(self):
        """像素断言守卫视觉元素。

        采样**表头行内部**（无标题时行 0..32，y=16 行中、x 避开左右外框与
        网格线）——整图取色会被"兜底色"骗过：深底与外框同色、白字与白色
        背景同色，断言恒真（变异复核实测）。斑马纹底色无兜底源，整图断言即可。
        """
        img = Image.open(io.BytesIO(render_table_png(TABLE))).convert("RGB")
        w = img.size[0]
        mid = {img.getpixel((x, 16)) for x in range(10, w - 10, 5)}
        assert (44, 62, 80) in mid  # #2C3E50 表头深底
        assert (255, 255, 255) in mid  # 表头白字笔画
        assert (245, 246, 250) in set(img.getdata())  # #F5F6FA 斑马纹


# ==========================================================================
# L19（REVIEW-6ec3f7c..a36ea1d）：护栏必须限**墙钟**，而不只是输入规模
#
# 上轮 M-1 的三个护栏（行/列/单元格）兜住了旧崩溃，但三者同时顶满时
# 100×20×200 全中文仍需 24.0s（≈0.055 ms/表字符，Font.getsize 为主）。
# 新增总量护栏后最坏 ~0.7s。
# ==========================================================================


class TestTotalCharsGuard:
    def _build(self, rows, cols, cell):
        hdr = "| " + " | ".join(f"列{i}" for i in range(cols)) + " |"
        sep = "|" + "---|" * cols
        body = [
            "| " + " | ".join("汉" * cell for _ in range(cols)) + " |"
            for _ in range(rows)
        ]
        return "\n".join([hdr, sep, *body])

    def test_ceiling_render_is_seconds_not_tens_of_seconds(self):
        """三护栏同时顶满：耗时必须落在秒级（旧实现 24s）。"""
        import time

        table = self._build(100, 20, 200)
        t0 = time.perf_counter()
        png = render_table_png(table)
        elapsed = time.perf_counter() - t0
        assert png is not None
        assert elapsed < 5.0, f"最坏渲染 {elapsed:.1f}s，总量护栏未生效"

    def test_guard_actually_truncates_rows(self):
        """截断必须真的发生（否则断言会变成恒真）。"""
        import agentcore.render.table as tbl_mod

        table = self._build(100, 20, 200)
        captured: list[int] = []
        real_warn = tbl_mod.logger.warning

        def spy(msg, *a, **kw):
            if "总字符数" in str(msg):
                captured.append(1)
            return real_warn(msg, *a, **kw)

        tbl_mod.logger.warning = spy
        try:
            assert render_table_png(table) is not None
        finally:
            tbl_mod.logger.warning = real_warn
        assert captured, "超过 _MAX_TOTAL_CHARS 时必须有截断告警"

    def test_small_table_not_truncated(self):
        """正常规模的表不得被新护栏误伤。"""
        table = self._build(5, 3, 10)
        png = render_table_png(table)
        assert png is not None
        assert _png_height(png) > 0

    def test_total_chars_limit_is_documented_constant(self):
        from agentcore.render.table import _MAX_TOTAL_CHARS

        assert _MAX_TOTAL_CHARS > 0
        # 与"秒级"目标相称：按 0.055 ms/字 ≈ 1.1s
        assert _MAX_TOTAL_CHARS <= 100_000
