"""本机概览卡片渲染：竖屏简版（数据进、PNG 出）。

用途：QQ 里被「戳一戳」时回一张**当前机器概览图**。图是**本机渲染**的——
数据来自看板只读 API（``agentcore/dashboard.py``），绘制完全在 bot 进程内完成，
不依赖浏览器。

为什么是「简版竖屏」而不是照搬看板整页
--------------------------------------
看板概览页是给 1600x900 桌面视口设计的：侧栏导航、服务列表、页脚按钮在**一张
聊天图片**里全是噪音——手机上看不清，还占满屏幕。这里只保留"一眼要看的东西"：

    主机 + 实时状态
    CPU / 内存 / 功耗 / 网速 / 磁盘   各带一条迷你曲线
    此刻最忙的程序（前 5 条）
    两行健康摘要（服务 / 温度 / 负载 / 开机时长 / 网卡）

**风格**仍与看板一致：同一套 CSS 变量配色、同一套曲线口径（``chart.js`` 的量程
与渐变填充），只是重排成竖屏。改样式时两边要一起看。

- 画布宽度固定 900，**高度由内容决定**（进程条数会变）——保证永远是竖屏、
  且不留大片空白。
- 任何指标 ``available: false`` 显示「不可用」；缺字段不抛异常。
- 无可用中文字体时返回 None——调用方必须降级为文本。

边界：本模块是纯绘制，不碰网络、不 import nonebot。
"""

from __future__ import annotations

import io
import logging
import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 画布与排版
W = 900  # 固定宽度；高度按内容算（见 render_height）
PAD = 32
GAP = 14  # 卡片之间
CARD_RADIUS = 16

HOST_SIZE = 34
LIVE_SIZE = 14
LABEL_SIZE = 15
VALUE_SIZE = 34
UNIT_SIZE = 15
SUB_SIZE = 13
PROC_SIZE = 14
PROC_TITLE_SIZE = 15
PROC_NOTE_SIZE = 12
FOOT_SIZE = 13

HOST_LINE_H = 46
LIVE_LINE_H = 22
HEADER_GAP = 18

METRIC_COUNT = 5  # overview.js METRICS 条数（CPU/内存/功耗/网速/磁盘）
METRIC_H = 150  # 单个指标块高度
METRIC_PAD_X = 20
SPARK_H = 46  # 迷你曲线高度
VALUE_BASE = 62  # 值/标签基线相对块顶
SUB_BASE = 92  # 副标题基线相对块顶
SPARK_TOP = 98  # 曲线区顶部相对块顶
BAR_H = 10  # 磁盘进度条厚度

PROC_TITLE_H = 40
PROC_ROW_H = 34
MAX_PROCS = 5  # 只放前 5 条；再多就不"简"了

FOOT_LINES = 2
FOOT_LINE_H = 22

# ---------------------------------------------------------------- 调色板
# dashboard/static/style.css :root
BG = (0x17, 0x11, 0x1F)
BG_GLOW = (0x24, 0x1A, 0x33)
CARD = (0x22, 0x1B, 0x2E)
TEXT = (0xEC, 0xE8, 0xF5)
MUTED = (0x8D, 0x84, 0xA6)
DIM = (0x6B, 0x63, 0x83)
GREEN = (0x3D, 0xDC, 0x97)
PINK = (0xFF, 0x6B, 0x9D)
BLUE = (0x4F, 0xC3, 0xF7)
ORANGE = (0xFF, 0xB7, 0x4D)
YELLOW = (0xFF, 0xD5, 0x4F)
YELLOW_LIGHT = (0xFF, 0xE0, 0x82)

# 半透明色：Pillow 的 ImageDraw 不做 alpha 混合（直接覆写像素），所以按
# "已知背景色 + alpha"预混成实色。卡片底色是常量，预混结果与浏览器一致。
LINE_ON_CARD = (0x2C, 0x26, 0x38)  # rgba(255,255,255,.07) over --card
LINE_SOFT_ON_CARD = (0x2A, 0x23, 0x35)  # rgba(255,255,255,.04) over --card
BAR_TRACK = (0x35, 0x2E, 0x42)  # rgba(255,255,255,.09) over --card

# ---------------------------------------------------------------- 字体
# 界面风格是 PingFang / 微软雅黑那一路的现代无衬线：Noto Sans CJK（思源黑体）
# 观感最接近，且有真正的 Bold 字重（数值都是 600）。仓库自带的文泉驿正黑兜底
# ——editable 安装即到位，保证离线/精简机器也画得出来。
_REPO_FONT = Path(__file__).resolve().parents[2] / "data" / "fonts" / "wqy-zenhei.ttc"
_REGULAR_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    str(_REPO_FONT),
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/wqy-zenhei/wqy-zenhei.ttc",
    "/usr/local/share/fonts/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "C:/Windows/Fonts/msyh.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)
_BOLD_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
) + _REGULAR_CANDIDATES

_font_cache: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}
_font_probe_done = False
_font_ok = False

RGB = tuple[int, int, int]


def ensure_font_probed() -> bool:
    """首次调用探测字体链并打**一条**汇总 WARNING（之后缓存）。"""
    global _font_probe_done, _font_ok
    if _font_probe_done:
        return _font_ok
    _font_probe_done = True
    try:
        _font(14)
        _font_ok = True
    except Exception:
        _font_ok = False
        logger.warning(
            "overview render: 候选字体链全部不可用，概览图将降级为文本。候选：%s",
            ", ".join(_REGULAR_CANDIDATES),
        )
    return _font_ok


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    key = (size, bold)
    cached = _font_cache.get(key)
    if cached is not None:
        return cached
    last_err: Exception | None = None
    for path in _BOLD_CANDIDATES if bold else _REGULAR_CANDIDATES:
        try:
            font = ImageFont.truetype(str(path), size)
            _font_cache[key] = font
            return font
        except Exception as exc:  # 缺失/损坏都试下一个
            last_err = exc
    raise last_err or OSError("no font available")


def _text_w(font: ImageFont.FreeTypeFont, text: str) -> int:
    if not text:
        return 0
    return font.getbbox(text)[2]


def _ascent(font: ImageFont.FreeTypeFont) -> int:
    return font.getmetrics()[0]


def _baseline(top: float, line_h: float, font: ImageFont.FreeTypeFont) -> int:
    """CSS 行盒里首行文字的基线（半行距公式）。"""
    ascent, descent = font.getmetrics()
    return int(top + (line_h - (ascent + descent)) / 2 + ascent)


def _fit(font: ImageFont.FreeTypeFont, text: str, max_w: int) -> str:
    """把 text 截到 max_w 像素内（二分），末尾省略号——对应 CSS text-overflow。"""
    if max_w <= 0 or _text_w(font, text) <= max_w:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _text_w(font, text[:mid] + "…") <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return (text[:lo] + "…") if lo > 0 else "…"


# ---------------------------------------------------------------- 取数与格式化


def _m(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _jnum(value: Any) -> str:
    """尽力模仿 JS ``String(number)``：35.9 → "35.9"，35 → "35"。"""
    if isinstance(value, bool) or value is None:
        return "—"
    if isinstance(value, int):
        return str(value)
    out = _num(value)
    if out is None:
        return "—"
    return f"{out:g}" if out == int(out) else repr(out)


def fmt_rate(bps: Any) -> str:
    """等价 /static/app.js::fmtRate（B/s → KB/s → MB/s → GB/s）。"""
    value = _num(bps)
    if value is None:
        return "—"
    units = ("B/s", "KB/s", "MB/s", "GB/s")
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024.0
        idx += 1
    return (f"{value:.0f}" if value >= 100 else f"{value:.1f}") + " " + units[idx]


def fmt_mb(mb: Any) -> str:
    """等价 app.js::fmtMB。"""
    value = _num(mb)
    if value is None:
        return "—"
    return f"{value / 1024:.1f} GB" if value >= 1024 else f"{value:.0f} MB"


def fmt_uptime(seconds: Any) -> str:
    """等价 app.js::fmtUptime。"""
    value = _num(seconds)
    if value is None:
        return "—"
    days = int(value // 86400)
    hours = int(value % 86400 // 3600)
    mins = int(value % 3600 // 60)
    return f"{days} 天 {hours} 小时" if days > 0 else f"{hours} 小时 {mins} 分钟"


def fmt_clock(ts: float) -> str:
    """等价 app.js::fmtClock（9月22日 周二 02:02:38）。"""
    dt = datetime.fromtimestamp(ts)
    weeks = "日一二三四五六"
    return (
        f"{dt.month}月{dt.day}日 周{weeks[(dt.weekday() + 1) % 7]} "
        f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"
    )


def _lerp(a: RGB, b: RGB, t: float) -> RGB:
    return (
        int(a[0] + (b[0] - a[0]) * t + 0.5),
        int(a[1] + (b[1] - a[1]) * t + 0.5),
        int(a[2] + (b[2] - a[2]) * t + 0.5),
    )


def _blend(fg: RGB, alpha: float, bg: RGB) -> RGB:
    return _lerp(bg, fg, max(0.0, min(1.0, alpha)))


# ------------------------------------------------- 排版几何（绘制与测试共用）


def _header_bottom() -> int:
    return PAD + HOST_LINE_H + LIVE_LINE_H


def metrics_top() -> int:
    return _header_bottom() + HEADER_GAP


def metrics_bottom() -> int:
    return metrics_top() + METRIC_H * METRIC_COUNT


def metric_row(index: int) -> tuple[int, int]:
    """第 index 个指标块的 ``(y0, y1)``。"""
    top = metrics_top() + METRIC_H * index
    return top, top + METRIC_H


def spark_box(index: int) -> tuple[int, int, int, int]:
    """第 index 个指标块的迷你曲线区（磁盘块画成进度条，用的是同一块区域）。"""
    top, _ = metric_row(index)
    return (
        PAD + METRIC_PAD_X,
        top + SPARK_TOP,
        W - PAD - METRIC_PAD_X,
        top + SPARK_TOP + SPARK_H,
    )


def process_count_for(ov: Mapping[str, Any]) -> int:
    """实际会画几条进程（≤ MAX_PROCS），决定整图高度。"""
    items = [p for p in (ov.get("processes") or []) if isinstance(p, Mapping)]
    return min(len(items), MAX_PROCS)


def processes_top() -> int:
    return metrics_bottom() + GAP


def processes_height(n: int) -> int:
    return PROC_TITLE_H + PROC_ROW_H * n


def footer_top(n: int) -> int:
    return processes_top() + processes_height(n) + GAP


def render_height(ov: Mapping[str, Any]) -> int:
    """整图高度：由内容（进程条数）决定，保证竖屏且不留大片空白。"""
    return footer_top(process_count_for(ov)) + FOOT_LINE_H * FOOT_LINES + PAD


# ---------------------------------------------------------------- 背景 / 装饰


def _background(height: int) -> Image.Image:
    """body 背景：radial-gradient(1100px 560px at 18% -12%, --bg-glow, --bg 58%)。

    解析式在低分辨率上算椭圆距离再放大：渐变平滑，放大无可见失真，而逐像素在
    纯 Python 里要上百万次 sqrt（秒级）。
    """
    img = Image.new("RGB", (W, height), BG)
    rx, ry = 1100.0, 560.0
    cx, cy = W * 0.18, -height * 0.12
    sw, sh = 300, 160
    strip = Image.new("RGB", (sw, sh))
    pixels = strip.load()
    xs = [
        ((((cx - rx) + (ix + 0.5) * (2 * rx) / sw - cx) / rx) ** 2) for ix in range(sw)
    ]
    for iy in range(sh):
        dy = (cy - ry) + (iy + 0.5) * (2 * ry) / sh - cy
        ty = (dy / ry) ** 2
        for ix in range(sw):
            k = min(1.0, math.sqrt(xs[ix] + ty) / 0.58)
            pixels[ix, iy] = _lerp(BG_GLOW, BG, k)
    img.paste(
        strip.resize((int(rx * 2), int(ry * 2)), Image.BILINEAR),
        (int(cx - rx), int(cy - ry)),
    )
    return img


def _glow_dot(
    draw: ImageDraw.ImageDraw,
    cx: float,
    cy: float,
    radius: float,
    color: RGB,
    bg: RGB,
    strength: float,
) -> None:
    """``box-shadow: 0 0 Npx rgba(...)`` 的近似——一圈由内向外变淡。"""
    for step in range(4, 0, -1):
        r = radius + step * 2
        alpha = strength * (1 - step / 5.0) ** 2
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=_blend(color, alpha, bg))


def _card(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int]) -> None:
    draw.rounded_rectangle(
        box, radius=CARD_RADIUS, fill=CARD, outline=LINE_ON_CARD, width=1
    )


# ---------------------------------------------------------------- 指标定义与取值


_METRICS: tuple[dict[str, Any], ...] = (
    # overview.js METRICS（含 chart.js 的量程参数）
    {
        "key": "cpu",
        "label": "CPU 占用",
        "color": GREEN,
        "series": (("cpu", GREEN, True),),
        "fixed": "cpu",
    },
    {
        "key": "mem",
        "field": "memory",  # 卡片 key（mem）与 API 字段名（memory）不同名
        "label": "内存",
        "color": BLUE,
        "series": (("mem_used", BLUE, True),),
        "fixed": "mem",
    },
    {
        "key": "power",
        "label": "功耗",
        "color": ORANGE,
        "series": (("power", ORANGE, True),),
        "auto_min_top": 10.0,
    },
    {
        "key": "net",
        "label": "网速",
        "series": (("net_down", GREEN, False), ("net_up", PINK, False)),
        "auto_min_top": 4096.0,
    },
    {"key": "disk", "label": "磁盘剩余", "color": YELLOW, "bar": True},
)


def _field(spec: Mapping[str, Any]) -> str:
    """卡片 key → /api/overview 里的字段名（目前只有 mem → memory 不同名）。"""
    return str(spec.get("field") or spec["key"])


def _axis_range(
    spec: Mapping[str, Any], ov: Mapping[str, Any], peak: float
) -> tuple[float, float] | None:
    """chart.js redraw()：fixed 优先，否则 0 → max(peak*1.15, autoMinTop)。"""
    fixed = spec.get("fixed")
    if fixed == "cpu":
        return 0.0, 100.0
    if fixed == "mem":
        total = _num(_m(ov.get("memory")).get("total_gb"))
        return (0.0, total) if total and total > 0 else None
    if "auto_min_top" in spec:
        return 0.0, max(peak * 1.15, float(spec["auto_min_top"]))
    return None


def _metric_parts(
    spec: Mapping[str, Any], ov: Mapping[str, Any]
) -> tuple[list[tuple[str, RGB, int, bool]], str]:
    """返回 ``(值行片段, 副标题)``——数值口径对齐 overview.js::renderMetrics。"""
    key = spec["key"]
    if key == "cpu":
        cpu = _m(ov.get("cpu"))
        if not cpu.get("available"):
            return [("不可用", DIM, 22, True)], ""
        freq = _num(cpu.get("freq_mhz"))
        sub = f"{ov.get('cores', 0)} 核" + (f" · {freq:.0f} MHz" if freq else "")
        return [
            (f"{_num(cpu.get('percent')) or 0:.0f}", TEXT, VALUE_SIZE, True),
            ("%", MUTED, UNIT_SIZE, False),
        ], sub
    if key == "mem":
        mem = _m(ov.get("memory"))
        if not mem.get("available"):
            return [("不可用", DIM, 22, True)], ""
        total = _num(mem.get("total_gb")) or 0.0
        sub = (
            f"已用 {_jnum(mem.get('percent'))}% · "
            f"交换 {_num(mem.get('swap_used_gb')) or 0:.1f} GB"
        )
        return [
            (f"{_num(mem.get('used_gb')) or 0:.1f}", TEXT, VALUE_SIZE, True),
            (f"/ {total:.1f} GB", MUTED, UNIT_SIZE, False),
        ], sub
    if key == "power":
        power = _m(ov.get("power"))
        if not power.get("available"):
            reason = str(power.get("reason") or "")
            return [("不可用", DIM, 22, True)], (
                "需 root 或 udev 规则" if "root" in reason else ""
            )
        return [
            (f"{_num(power.get('watts')) or 0:.1f}", TEXT, VALUE_SIZE, True),
            ("W", MUTED, UNIT_SIZE, False),
        ], f"{power.get('source') or 'Intel RAPL'} · RAPL"
    if key == "disk":
        disk = _m(ov.get("disk"))
        if not disk.get("available"):
            return [("不可用", DIM, 22, True)], str(disk.get("reason") or "")
        free = _num(disk.get("free_gb")) or 0.0
        total = _num(disk.get("total_gb")) or 0.0
        sub = f"挂载 {disk.get('path')} · 已用 {_jnum(disk.get('used_percent'))}%"
        return [
            (f"{free:.0f}" if free >= 100 else f"{free:.1f}", TEXT, VALUE_SIZE, True),
            (f"/ {total:.0f} GB", MUTED, UNIT_SIZE, False),
        ], sub
    return [("不可用", DIM, 22, True)], ""


# ---------------------------------------------------------------- 迷你曲线


def _segments(
    points: Sequence[Any],
    t0: float,
    t1: float,
    y_min: float,
    y_max: float,
    max_gap: float,
    box: tuple[int, int, int, int],
) -> list[list[tuple[float, float]]]:
    """chart.js buildPaths：null 与时间断层处断开，0-100 坐标再映射到像素。"""
    x0, y0, x1, y1 = box
    cw, ch = x1 - x0, y1 - y0
    span = max(0.001, t1 - t0)
    y_span = max(0.0001, y_max - y_min)
    out: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    prev_ts: float | None = None
    for point in points or []:
        if not isinstance(point, list | tuple) or len(point) < 2:
            continue
        ts, value = _num(point[0]), _num(point[1])
        if ts is None or value is None:
            if current:
                out.append(current)
                current = []
            prev_ts = None
            continue
        if current and prev_ts is not None and ts - prev_ts > max_gap:
            out.append(current)
            current = []
        px = ((ts - t0) / span) * 100.0
        py = 100.0 - ((value - y_min) / y_span) * 96.0 - 2.0
        current.append(
            (
                x0 + max(0.0, min(100.0, px)) / 100.0 * cw,
                y0 + max(2.0, min(98.0, py)) / 100.0 * ch,
            )
        )
        prev_ts = ts
    if current:
        out.append(current)
    return out


def _draw_series(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    color: RGB,
    segments: list[list[tuple[float, float]]],
    fill: bool,
) -> None:
    x0, y0, x1, y1 = box
    cw, ch = x1 - x0, y1 - y0
    drawn = [seg for seg in segments if len(seg) >= 2]
    strip = None
    if fill and drawn:
        # linearGradient 默认 objectBoundingBox：渐变铺在**整条 area path** 的包围盒上，
        # 而不是图表框。path 的 d 里每段都带 `L x,100`，故包围盒底 = 图表底，
        # 顶 = 各段线点里最高的那个（所有段共用一条 path，包围盒也是全局的）。
        # 按图表框铺会让"线贴近顶部"的指标（内存）填充明显偏淡——对照参考截图实测过。
        top_px = min(py for seg in drawn for _, py in seg) - y0
        span = max(1.0, ch - top_px)
        strip = Image.new("RGB", (1, ch))
        spx = strip.load()
        for yy in range(ch):
            t = 0.0 if yy <= top_px else min(1.0, (yy - top_px) / span)
            spx[0, yy] = _blend(color, 0.22 * (1 - t), CARD)
        strip = strip.resize((cw, ch))
    for seg in segments:
        if len(seg) < 2:
            # 孤点：画一小段横线（chart.js single-point 分支，宽度 0.8/100）
            px, py = seg[0]
            draw.line(
                [(px - 0.004 * cw, py), (px + 0.004 * cw, py)], fill=color, width=2
            )
            continue
        if strip is not None:
            local = [(px - x0, py - y0) for px, py in seg]
            mask = Image.new("L", (cw, ch), 0)
            ImageDraw.Draw(mask).polygon(
                [*local, (local[-1][0], ch), (local[0][0], ch)], fill=255
            )
            img.paste(strip, (x0, y0), mask)
        draw.line(seg, fill=color, width=2, joint="curve")


def _draw_disk_bar(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    ov: Mapping[str, Any],
    box: tuple[int, int, int, int],
) -> None:
    disk = _m(ov.get("disk"))
    if not disk.get("available"):
        return
    x0, y0, x1, y1 = box
    ty = y0 + (y1 - y0 - BAR_H) // 2
    draw.rounded_rectangle((x0, ty, x1, ty + BAR_H), radius=BAR_H // 2, fill=BAR_TRACK)
    pct = _num(disk.get("used_percent")) or 0.0
    width = int((x1 - x0) * max(0.0, min(100.0, pct)) / 100.0)
    if width < 2:
        return
    # linear-gradient(90deg, #ffe082, --yellow)
    grad = Image.new("RGB", (width, 1))
    gpx = grad.load()
    for xx in range(width):
        gpx[xx, 0] = _lerp(YELLOW_LIGHT, YELLOW, xx / max(1, width - 1))
    mask = Image.new("L", (width, BAR_H), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, width - 1, BAR_H - 1), radius=BAR_H // 2, fill=255
    )
    img.paste(grad.resize((width, BAR_H)), (x0, ty), mask)


def _draw_spark(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    ov: Mapping[str, Any],
    snap: Mapping[str, Any],
    spec: Mapping[str, Any],
    box: tuple[int, int, int, int],
    t1: float,
) -> None:
    """画一条迷你曲线（磁盘块改画进度条）。数据/量程缺失就整块留空，不画半张图。"""
    # 指标报 available:false 时左侧写的是「不可用」，此时**绝不能**再画曲线——
    # /api/series 里对应的历史点仍在，照画就会出现"标着不可用却有曲线"的自相矛盾。
    if not _m(ov.get(_field(spec))).get("available"):
        return
    if spec.get("bar"):
        _draw_disk_bar(img, draw, ov, box)
        return
    window = _num(snap.get("window")) or _num(ov.get("window")) or 120.0
    interval = _num(snap.get("interval")) or _num(ov.get("interval")) or 1.0
    series_map = _m(snap.get("series"))
    points = {k: series_map.get(k) or [] for k, _, _ in spec["series"]}
    peak = max(
        (
            v
            for pts in points.values()
            for p in pts or []
            if isinstance(p, list | tuple)
            and len(p) >= 2
            and (v := _num(p[1])) is not None
        ),
        default=0.0,
    )
    axis = _axis_range(spec, ov, peak)
    if axis is None:
        return
    y_min, y_max = axis
    t0 = t1 - window
    max_gap = max(6.0, interval * 4)
    for key_name, color, fill in spec["series"]:
        segs = _segments(points.get(key_name) or [], t0, t1, y_min, y_max, max_gap, box)
        _draw_series(img, draw, box, color, segs, fill)


# ---------------------------------------------------------------- 各区块


def _draw_header(draw: ImageDraw.ImageDraw, ov: Mapping[str, Any], ts: float) -> None:
    f_host = _font(HOST_SIZE, bold=True)
    draw.text(
        (PAD, _baseline(PAD, HOST_LINE_H, f_host)),
        str(ov.get("host") or "总控台"),
        font=f_host,
        fill=TEXT,
        anchor="ls",
    )
    live_top = PAD + HOST_LINE_H
    f_live = _font(LIVE_SIZE)
    base = _baseline(live_top, LIVE_LINE_H, f_live)
    cy = live_top + LIVE_LINE_H / 2
    _glow_dot(draw, PAD + 4, cy, 3, GREEN, BG, 0.7)
    draw.ellipse((PAD + 1, cy - 3, PAD + 7, cy + 3), fill=GREEN)
    draw.text((PAD + 15, base), "实时更新中", font=f_live, fill=MUTED, anchor="ls")
    draw.text((W - PAD, base), fmt_clock(ts), font=f_live, fill=DIM, anchor="rs")


def _draw_metric(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    ov: Mapping[str, Any],
    snap: Mapping[str, Any],
    spec: Mapping[str, Any],
    index: int,
    t1: float,
) -> None:
    top, _ = metric_row(index)
    left, right = PAD + METRIC_PAD_X, W - PAD - METRIC_PAD_X
    parts, sub = _metric_parts(spec, ov)
    is_net = spec["key"] == "net"
    # 五行标签共用同一条基线；网速行右侧是两行数值，标签正好落在两块之间（左侧，
    # 不与右侧数值争位置）——比把标签单独上移更整齐。
    label_base = top + VALUE_BASE

    mark = spec.get("color")
    if mark is not None:
        draw.rounded_rectangle(
            (left, label_base - 15, left + 3, label_base + 1), radius=1, fill=mark
        )
        label_x = left + 3 + 8
    else:
        label_x = left
    f_label = _font(LABEL_SIZE)
    draw.text(
        (label_x, label_base), spec["label"], font=f_label, fill=MUTED, anchor="ls"
    )

    if is_net:
        net = _m(ov.get("net"))
        f_net = _font(16)
        for name, label, color, dy in (
            ("down", "下载", GREEN, -14),
            ("up", "上传", PINK, 14),
        ):
            available = bool(net.get("available"))
            value = fmt_rate(net.get(f"{name}_bps")) if available else "—"
            draw.text(
                (right, top + VALUE_BASE + dy),
                f"{label} {value}",
                font=f_net,
                fill=color if available else DIM,
                anchor="rs",
            )
    else:
        # 值 + 单位：从右往左摆，共享基线
        cursor = right
        for text, color, size, bold in reversed(parts):
            font = _font(size, bold=bold)
            draw.text(
                (cursor, top + VALUE_BASE), text, font=font, fill=color, anchor="rs"
            )
            cursor -= _text_w(font, text)
            if size == VALUE_SIZE:
                cursor -= 4  # .metric-value .unit margin-left

    if sub:
        f_sub = _font(SUB_SIZE)
        draw.text(
            (left, top + SUB_BASE),
            _fit(f_sub, sub, right - left),
            font=f_sub,
            fill=DIM,
            anchor="ls",
        )
    _draw_spark(img, draw, ov, snap, spec, spark_box(index), t1)


def _draw_processes(draw: ImageDraw.ImageDraw, ov: Mapping[str, Any], n: int) -> None:
    top = processes_top()
    left, right = PAD + METRIC_PAD_X, W - PAD - METRIC_PAD_X
    f_title = _font(PROC_TITLE_SIZE, bold=True)
    base = _baseline(top + 14, 22, f_title)
    draw.text((left, base), "此刻最忙的程序", font=f_title, fill=TEXT, anchor="ls")
    count = ov.get("process_count")
    if count:
        f_note = _font(PROC_NOTE_SIZE)
        draw.text(
            (right, base), f"共 {count} 个进程", font=f_note, fill=DIM, anchor="rs"
        )

    f_row = _font(PROC_SIZE)
    items = [p for p in (ov.get("processes") or []) if isinstance(p, Mapping)][:n]
    num_w, rss_w = 70, 86
    for idx, proc in enumerate(items):
        row_top = top + PROC_TITLE_H + idx * PROC_ROW_H
        base = _baseline(row_top, PROC_ROW_H, f_row)
        cpu = _num(proc.get("cpu"))
        name_w = right - left - num_w - rss_w - 28
        draw.text(
            (left, base),
            _fit(f_row, str(proc.get("name") or ""), name_w),
            font=f_row,
            fill=TEXT,
            anchor="ls",
        )
        draw.text(
            (right - rss_w - 14, base),
            f"{cpu:.1f}%" if cpu is not None else "—",
            font=f_row,
            fill=MUTED,
            anchor="rs",
        )
        draw.text(
            (right, base), fmt_mb(proc.get("rss_mb")), font=f_row, fill=DIM, anchor="rs"
        )
        if idx < len(items) - 1:
            y = row_top + PROC_ROW_H
            draw.line([(left, y), (right, y)], fill=LINE_SOFT_ON_CARD)


def _footer_lines(ov: Mapping[str, Any]) -> list[str]:
    """底部两行摘要：只放"整机是否健康"的标量。缺项就少一项，不补零不猜。"""
    first: list[str] = []
    docker = next(
        (s for s in (ov.get("services") or []) if _m(s).get("name") == "Docker"), None
    )
    detail = str(_m(docker).get("detail") or "").strip() if docker else ""
    if detail:
        first.append(f"服务 {detail}")
    temp = _m(ov.get("temp"))
    temp_c = _num(temp.get("celsius"))
    if temp.get("available") and temp_c is not None:
        first.append(f"温度 {temp_c:.0f}°C")
    load = _m(ov.get("load"))
    if load.get("available"):
        avg = [_num(load.get(k)) for k in ("avg1", "avg5", "avg15")]
        if all(v is not None for v in avg):
            first.append("负载 " + " / ".join(f"{v:.2f}" for v in avg))  # type: ignore[str-format]

    second: list[str] = [f"已开机 {fmt_uptime(ov.get('uptime_s'))}"]
    nic = str(_m(ov.get("net")).get("nic") or "").strip()
    if nic:
        second.append(f"网卡 {nic}")
    procs = ov.get("process_count")
    if procs:
        second.append(f"进程 {procs}")
    return [" · ".join(first), " · ".join(second)]


def _draw_footer(draw: ImageDraw.ImageDraw, ov: Mapping[str, Any], n: int) -> None:
    top = footer_top(n)
    f = _font(FOOT_SIZE)
    for idx, line in enumerate(_footer_lines(ov)[:FOOT_LINES]):
        if not line:
            continue
        draw.text(
            (PAD, _baseline(top + idx * FOOT_LINE_H, FOOT_LINE_H, f)),
            _fit(f, line, W - PAD * 2),
            font=f,
            fill=DIM,
            anchor="ls",
        )


# ---------------------------------------------------------------- 入口


def render_overview_png(
    snapshot: Mapping[str, Any], *, now: float | None = None
) -> bytes | None:
    """把一次看板采样画成竖屏简版概览 PNG；无字体/绘制失败返回 None。

    ``snapshot`` 形如 ``{"overview": {...}, "series": {...}, "ts": ..., "window": ...,
    "interval": ...}``（见 ``agentcore/dashboard.py``）。字段缺失/类型异常都不抛，
    缺的指标按「不可用」画。
    """
    if not ensure_font_probed():
        return None
    try:
        return _render(snapshot, now)
    except Exception:
        logger.warning("overview render failed", exc_info=True)
        return None


def _render(snapshot: Mapping[str, Any], now: float | None) -> bytes:
    ov = _m(snapshot.get("overview"))
    t1 = (
        now
        or _num(snapshot.get("ts"))
        or _num(ov.get("ts"))
        or datetime.now().timestamp()
    )
    n_proc = process_count_for(ov)
    height = render_height(ov)

    img = _background(height)
    draw = ImageDraw.Draw(img)
    _draw_header(draw, ov, t1)

    _card(draw, (PAD, metrics_top(), W - PAD, metrics_bottom()))
    for idx, spec in enumerate(_METRICS):
        if idx:
            y, _ = metric_row(idx)
            draw.line([(PAD, y), (W - PAD, y)], fill=LINE_SOFT_ON_CARD)
        _draw_metric(img, draw, ov, snapshot, spec, idx, t1)

    _card(
        draw,
        (PAD, processes_top(), W - PAD, processes_top() + processes_height(n_proc)),
    )
    _draw_processes(draw, ov, n_proc)
    _draw_footer(draw, ov, n_proc)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
