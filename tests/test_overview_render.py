"""本机概览图渲染：产出的像素/结构化断言、降级路径与格式化口径。

重点是**能抓住实现被改坏**：
- 内存行的字段名是 ``memory`` 而卡片 key 是 ``mem``——写错就去画「不可用」，
  所以直接断言内存曲线区真的有蓝色线像素（不是只看"没抛异常"）。
- ``fmt_*`` 必须与 dashboard/static/app.js 的口径一致（数值格式对齐截图）。
"""

from __future__ import annotations

import pytest

from agentcore.render import overview as ov

pytest.importorskip("PIL")

BLUE = ov.BLUE


def _snapshot(now: float = 1790026700.0) -> dict:
    """一份最小可渲染快照（曲线两点即可成段）。"""
    points = [[now - 2, 40.0], [now - 1, 60.0], [now, 55.0]]
    return {
        "overview": {
            "ts": now,
            "ready": True,
            "host": "unit-host",
            "cores": 4,
            "uptime_s": 3600.0,
            "process_count": 12,
            "window": 120.0,
            "interval": 1.0,
            "cpu": {"available": True, "percent": 55.0, "freq_mhz": 3000},
            "memory": {
                "available": True,
                "used_gb": 4.0,
                "total_gb": 16.0,
                "percent": 25.0,
                "swap_used_gb": 0.0,
            },
            "power": {"available": True, "watts": 9.0, "source": "平均值"},
            "net": {
                "available": True,
                "nic": "eth0",
                "down_bps": 1024.0,
                "up_bps": 2048.0,
            },
            "disk": {
                "available": True,
                "path": "/",
                "free_gb": 100.0,
                "total_gb": 233.0,
                "used_percent": 57.0,
            },
            "temp": {"available": True, "celsius": 70.0},
            "load": {"available": True, "avg1": 1.0, "avg5": 1.0, "avg15": 1.0},
            "processes": [
                {"pid": 1, "name": "chrome", "cpu": 24.3, "rss_mb": 218.0},
                {"pid": 2, "name": "python3", "cpu": 2.2, "rss_mb": 24.0},
            ],
            "services": [
                {
                    "group": "容器",
                    "name": "Docker",
                    "status": "ok",
                    "detail": "6/6 运行中",
                },
                {
                    "group": "本机",
                    "name": "SSH (22)",
                    "status": "ok",
                    "detail": "监听中",
                },
            ],
        },
        "series": {
            "cpu": points,
            "mem_used": points,
            "power": points,
            "net_down": points,
            "net_up": points,
        },
        "ts": now,
        "window": 120.0,
        "interval": 1.0,
    }


def _has_color_near(img, box, color, tol: int = 40) -> bool:
    """box 内是否存在接近 color 的像素（用于"曲线真的画出来了"）。"""
    data = img.crop(box).convert("RGB").tobytes()
    return any(
        all(abs(data[i + k] - color[k]) <= tol for k in range(3))
        for i in range(0, len(data), 3)
    )


# ---------------------------------------------------------------- 格式化口径


class TestFormatters:
    def test_rate_matches_js_fmtRate(self):
        """app.js：>=1024 进一位；>=100 取整、否则保留 1 位小数。"""
        assert ov.fmt_rate(512) == "512 B/s"
        assert ov.fmt_rate(1024) == "1.0 KB/s"
        assert ov.fmt_rate(65228.8) == "63.7 KB/s"
        assert ov.fmt_rate(1363148.8) == "1.3 MB/s"
        assert ov.fmt_rate(None) == "—"
        assert ov.fmt_rate(float("nan")) == "—"

    def test_mb_matches_js_fmtMB(self):
        assert ov.fmt_mb(512) == "512 MB"
        assert ov.fmt_mb(2048) == "2.0 GB"
        assert ov.fmt_mb(None) == "—"

    def test_uptime_matches_js_fmtUptime(self):
        assert ov.fmt_uptime(3600) == "1 小时 0 分钟"
        assert ov.fmt_uptime(14705) == "4 小时 5 分钟"
        assert ov.fmt_uptime(90000) == "1 天 1 小时"
        assert ov.fmt_uptime(None) == "—"

    def test_clock_matches_js_fmtClock(self):
        # 2026-09-22 是周二（与参考截图同款格式）
        text = ov.fmt_clock(1790023352.0)
        assert text.startswith("9月22日 周")
        assert len(text.split(" ")) == 3

    def test_jnum_trims_integral_floats(self):
        assert ov._jnum(35.9) == "35.9"
        assert ov._jnum(35.0) == "35"
        assert ov._jnum(57) == "57"
        assert ov._jnum(None) == "—"


# ---------------------------------------------------------------- 渲染


class TestRender:
    def test_renders_portrait_canvas(self):
        """宽度固定 900，高度按内容算，且**必须比宽高**（竖屏）。"""
        if not ov.ensure_font_probed():
            pytest.skip("环境无 CJK 字体")
        import io

        from PIL import Image

        snap = _snapshot()
        png = ov.render_overview_png(snap)
        assert png is not None
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        with Image.open(io.BytesIO(png)) as img:
            assert img.size == (ov.W, ov.render_height(snap["overview"]))
            assert img.height > img.width, "必须是竖屏（高 > 宽）"

    def test_height_follows_process_count(self):
        """进程条数决定整图高度（而不是留一片空白），并受 MAX_PROCS 封顶。"""
        few = _snapshot()["overview"]
        few["processes"] = [{"name": "a", "cpu": 1.0, "rss_mb": 1.0}]
        many = _snapshot()["overview"]
        many["processes"] = [
            {"name": str(i), "cpu": 1.0, "rss_mb": 1.0} for i in range(ov.MAX_PROCS + 4)
        ]
        assert ov.render_height(few) < ov.render_height(many)
        assert ov.process_count_for(many) == ov.MAX_PROCS

    def test_spark_boxes_stay_inside_their_rows(self):
        """5 条曲线区必须落在各自指标块内且互不重叠（排版回归的硬约束）。"""
        boxes = [ov.spark_box(i) for i in range(ov.METRIC_COUNT)]
        for idx, (x0, y0, x1, y1) in enumerate(boxes):
            row = ov.metric_row(idx)
            assert row[0] <= y0 < y1 <= row[1], (idx, row, (y0, y1))
            assert x0 < x1
        for prev, cur in zip(boxes, boxes[1:], strict=False):
            assert prev[3] <= cur[1], (prev, cur)

    def test_memory_row_uses_memory_field(self):
        """回归：卡片 key 是 mem，API 字段是 memory——写错就画成「不可用」。

        改坏方式：把 _METRICS 里 mem 的 field 去掉（或写成 "mem"），此用例必须失败。
        """
        if not ov.ensure_font_probed():
            pytest.skip("环境无 CJK 字体")
        import io

        from PIL import Image

        png = ov.render_overview_png(_snapshot())
        with Image.open(io.BytesIO(png)) as img:
            img = img.convert("RGB")
            # 第 2 个指标块（内存）的曲线区
            assert _has_color_near(img, ov.spark_box(1), BLUE), (
                "内存曲线区没有蓝色线像素（字段名映射错了？）"
            )

    def test_mem_spec_field_mapping(self):
        mem = next(s for s in ov._METRICS if s["key"] == "mem")
        assert ov._field(mem) == "memory"
        assert ov._field({"key": "cpu"}) == "cpu"

    def test_unavailable_metric_draws_no_curve(self):
        """报 available:false 的指标左侧写「不可用」，曲线区就必须是空的。

        /api/series 里对应的历史点仍在——少这一层判断就会画出"标着不可用却有曲线"
        的自相矛盾图（对照 dashboard 的 markUnavailable 行为）。
        改坏方式：删掉 _draw_spark 里的 available 判断，此用例必须失败。
        """
        if not ov.ensure_font_probed():
            pytest.skip("环境无 CJK 字体")
        import io

        from PIL import Image

        snap = _snapshot()
        # 功耗不可用（缺 RAPL 权限的常见情形），但曲线数据照旧存在
        snap["overview"]["power"] = {"available": False, "reason": "需要 root 权限"}
        with Image.open(io.BytesIO(ov.render_overview_png(snap))) as raw:
            img = raw.convert("RGB")
        assert not _has_color_near(img, ov.spark_box(2), ov.ORANGE), (
            "功耗报「不可用」却仍画出了橙色曲线"
        )
        # 对照组：可用时同一位置必须有曲线——证明上面的断言不是"整块都没画"的假阳性
        snap["overview"]["power"] = {"available": True, "watts": 9.0, "source": "x"}
        with Image.open(io.BytesIO(ov.render_overview_png(snap))) as raw:
            ok = raw.convert("RGB")
        assert _has_color_near(ok, ov.spark_box(2), ov.ORANGE)

    def test_handles_missing_and_null_fields(self):
        """数据缺字段/为 None 时必须照画（看板采样未就绪时前端就是全 "—"）。"""
        if not ov.ensure_font_probed():
            pytest.skip("环境无 CJK 字体")
        png = ov.render_overview_png({"overview": {}, "series": {}})
        assert png is not None and png[:8] == b"\x89PNG\r\n\x1a\n"

    def test_unavailable_metrics_do_not_crash(self):
        if not ov.ensure_font_probed():
            pytest.skip("环境无 CJK 字体")
        snap = _snapshot()
        for key in ("cpu", "memory", "power", "disk", "net"):
            snap["overview"][key] = {"available": False, "reason": "缺传感器"}
        snap["series"] = {}
        assert ov.render_overview_png(snap) is not None

    def test_explicit_now_overrides_ts(self):
        """now 显式传入时按它定位曲线窗口（否则测试/离线渲染会画出空图）。"""
        if not ov.ensure_font_probed():
            pytest.skip("环境无 CJK 字体")
        snap = _snapshot()
        snap["ts"] = 1.0  # 陈旧时间戳：曲线会全部落在窗口外
        assert ov.render_overview_png(snap, now=1790026700.0) is not None

    def test_no_font_returns_none(self, monkeypatch):
        monkeypatch.setattr(ov, "_font_probe_done", True)
        monkeypatch.setattr(ov, "_font_ok", False)
        assert ov.render_overview_png(_snapshot()) is None

    def test_render_exception_swallowed_to_none(self, monkeypatch):
        """绘制主体异常必须吞成 None（调用方据此降级文本），不能冒泡。"""
        monkeypatch.setattr(ov, "_font_probe_done", True)
        monkeypatch.setattr(ov, "_font_ok", True)

        def boom(*args, **kwargs):
            raise RuntimeError("draw failed")

        monkeypatch.setattr(ov, "_render", boom)
        assert ov.render_overview_png(_snapshot()) is None


class TestSegments:
    """chart.js buildPaths 的行为对齐：断层断开、超窗裁剪、量程映射。"""

    def test_axis_range_matches_chart_js(self):
        """量程口径（chart.js redraw）：cpu 固定 0-100、内存固定 0-总量，
        其余 0 → max(峰值×1.15, autoMinTop)。改坏任一条这里都会红。"""
        assert ov._axis_range({"key": "cpu", "fixed": "cpu"}, {}, 50.0) == (0.0, 100.0)
        assert ov._axis_range(
            {"key": "mem", "fixed": "mem"}, {"memory": {"total_gb": 16.0}}, 0.0
        ) == (0.0, 16.0)
        # 内存总量拿不到时不下发量程（否则会画出 0 高度的空图）
        assert (
            ov._axis_range({"key": "mem", "fixed": "mem"}, {"memory": {}}, 0.0) is None
        )
        # autoMinTop 是"自适量程的最小上限"，防止小数值把噪声放大
        assert ov._axis_range({"key": "power", "auto_min_top": 10.0}, {}, 3.0) == (
            0.0,
            10.0,
        )
        assert ov._axis_range(
            {"key": "power", "auto_min_top": 10.0}, {}, 100.0
        ) == pytest.approx((0.0, 115.0))
        assert ov._axis_range(
            {"key": "net", "auto_min_top": 4096.0}, {}, 8192.0
        ) == pytest.approx((0.0, 9420.8))
        assert ov._axis_range({"key": "disk", "bar": True}, {}, 0.0) is None

    def test_fill_gradient_uses_path_bbox(self):
        """填充渐变铺在 **area path 的包围盒** 上（chart.js 的 objectBoundingBox），
        不是图表框——按图表框铺会让"线贴顶"的内存行填充明显偏淡（对照参考截图实测）。

        内存行放一条横贯窗口的平线：线正下方 2px 处的 alpha 应≈0.22。
        改坏方式：把 ``top_px`` 写成 0（=按图表框铺），此用例必须失败。
        """
        if not ov.ensure_font_probed():
            pytest.skip("环境无 CJK 字体")
        from io import BytesIO

        from PIL import Image

        snap = _snapshot()
        now = snap["ts"]
        # 内存用 GB（固定量程 0..total_gb）：4.0/16.0 → 线画在曲线区的 63% 高度处
        snap["series"]["mem_used"] = [[now - 120 + i, 4.0] for i in range(121)]
        png = ov.render_overview_png(snap)
        with Image.open(BytesIO(png)) as raw:
            img = raw.convert("RGB")

        x0, y0, x1, y1 = ov.spark_box(1)  # 内存块的曲线区
        x = (x0 + x1) // 2
        column = [(y, img.getpixel((x, y))) for y in range(y0, y1)]
        line_y = next(
            y for y, c in column if all(abs(c[i] - ov.BLUE[i]) <= 25 for i in range(3))
        )
        fill = img.getpixel((x, line_y + 2))

        # 两种锚点下的预期：包围盒顶（=线所在处，alpha 最浓）/ 图表框顶（按高度线性衰减）
        bbox_anchor = ov._blend(ov.BLUE, 0.22, ov.CARD)
        local = (line_y + 2 - y0) / (y1 - y0)
        chart_anchor = ov._blend(ov.BLUE, 0.22 * (1 - local), ov.CARD)
        assert fill != ov.CARD and fill != bbox_anchor, fill
        assert abs(fill[1] - bbox_anchor[1]) < abs(fill[1] - chart_anchor[1]), (
            f"填充色 {fill} 更接近「图表框锚点」{chart_anchor}，说明渐变锚错了位置"
            f"（应为包围盒锚点 {bbox_anchor}）"
        )

    def test_break_on_null_and_gap(self):
        pts = [[0.0, 10.0], [1.0, 20.0], [2.0, None], [3.0, 30.0], [50.0, 40.0]]
        segs = ov._segments(
            pts, 0.0, 51.0, 0.0, 100.0, max_gap=5.0, box=(0, 0, 100, 100)
        )
        # null 处断开 → 3 段；50.0 与 3.0 间隔 47 > 5 也要断开
        assert len(segs) == 3
        assert [len(s) for s in segs] == [2, 1, 1]

    def test_points_outside_window_clamp_to_edges(self):
        segs = ov._segments(
            [[-100.0, 50.0], [200.0, 50.0]],
            0.0,
            10.0,
            0.0,
            100.0,
            99.0,
            (0, 0, 100, 100),
        )
        xs = [x for seg in segs for x, _ in seg]
        assert min(xs) == 0.0 and max(xs) == 100.0
