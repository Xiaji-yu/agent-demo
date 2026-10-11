"""群聊词云渲染：词频表进、PNG 出（WebUI 黑白点阵风格 + 墨阶词色）。

布局是确定性螺旋（阿基米德螺旋 + 矩形碰撞），**不用随机数**——同一份词频必须
画出同一张图（测试可断言几何性质；用户重开命令看到跳动的布局也会以为统计
变了）。放置失败的词（画布塞不下）记 DEBUG 后丢弃，绝不裁切半拉字。

配色沿墨阶三档（INK / INK_SOFT / MUTED）循环 + 少量 90° 旋转表达层次，
不引彩色板：与 style.py 的「状态一律黑白表达」同源（词云不是状态指示，
但一套绘制语言内部自洽更重要；要彩色是产品决策，见 BACKLOG）。

入口 :func:`render_cloud_png` 与 rank/overview 同约定：无字体/异常返回 None，
调用方降级文本。
"""

from __future__ import annotations

import io
import logging
import math

from PIL import Image, ImageDraw

from agentcore.render.fonts import ensure_font_probed
from agentcore.render.fonts import font as _font
from agentcore.render.style import INK, INK_SOFT, MUTED, PAPER, hatch

logger = logging.getLogger(__name__)

W, H = 800, 600
PAD = 36
TITLE_SIZE = 24
TITLE_H = 44

MIN_SIZE = 22
MAX_SIZE = 92
# 旋转角循环：中文竖排（90°）每 5 词出现一次，避免整张图一个方向太死
_ANGLES = (0, 0, 0, 0, 90)

# 螺旋参数。等环间距设计：径向步长 ∝ 1/r（K = 环间距×弧步长/2π），
# 这样内圈外圈的螺线疏密一致——早期实现用恒定径向步长，外圈环间距涨到
# 100+px，大词永远卡不进去（实测 40 词只放下 12 个）。
_SPIRAL_ARC = 5.0  # 每步弧长（px）
_SPIRAL_RING = 30.0  # 每圈径向进给（px）
_SPIRAL_K = _SPIRAL_RING * _SPIRAL_ARC / (2 * math.pi)
_SPIRAL_SQUASH = 0.72  # 纵向压扁系数：中文词横长竖短，椭圆轨道比正圆填充率高
_BOX_MARGIN = 10  # 碰撞盒在墨迹盒外的呼吸边距（贴太近的词在人眼里成一坨）
_MAX_PLACEMENTS = 5000  # 单词最大尝试次数（等环间距下 ≈ 覆盖到画布四角）

_COLORS = (INK, INK_SOFT, MUTED)


_PROBE = ImageDraw.Draw(Image.new("RGB", (8, 8)))
# 螺旋半径上限：超过中心到Canvas最远角的距离后不可能再落回画布内，直接停
_MAX_RADIUS = math.hypot(W / 2, H / 2)


def _font_size(count: int, max_count: int) -> int:
    """字号 ∝ sqrt(词频/最高词频)——平方根比线性更适合词云（长尾不被压成点）。"""
    if max_count <= 0:
        return MIN_SIZE
    ratio = math.sqrt(max(0, count) / max_count)
    return max(MIN_SIZE, min(MAX_SIZE, int(MIN_SIZE + (MAX_SIZE - MIN_SIZE) * ratio)))


def _text_layer(word: str, size: int, angle: int):
    """把词画到透明图层，裁剪到**墨迹紧包围盒**后（按需）旋转。

    不能用 ``font.getmetrics()`` 的 ascent+descent 当高度：wqy-zenhei 的
    ascent+descent ≈ 2em（行高留白），碰撞盒会比可见字形大一倍——实测 40 个词
    只放下 11 个，其余全被误判"放不下"（本函数初版踩过）。
    """
    f = _font(size, bold=True)
    tw = _PROBE.textlength(word, font=f)
    ascent, descent = f.getmetrics()
    pad = 6
    tmp = Image.new(
        "RGBA", (int(tw) + 2 * pad, ascent + descent + 2 * pad), (0, 0, 0, 0)
    )
    d = ImageDraw.Draw(tmp)
    d.text((pad, pad), word, font=f, fill=INK, anchor="la")
    ink = tmp.getbbox()
    if ink:
        tmp = tmp.crop(ink)
    if angle:
        tmp = tmp.rotate(-angle, expand=True)
    return tmp, tmp.width, tmp.height


def _place(
    boxes: list[tuple[int, int, int, int]],
    w: int,
    h: int,
    seed_index: int,
) -> tuple[int, int] | None:
    """螺旋试放：返回左上角坐标或 None（试爆仍放不下）。

    碰撞盒在墨迹盒外再扩 ``_BOX_MARGIN`` 呼吸边距（贴太近的词在人眼里成一坨）。
    螺旋半径单调扩张，超过中心到画布最远角的距离后不可能回到画布内——直接
    收工（否则满画布时要空转上千次碰撞检测）。
    """
    cx, cy = W // 2, (TITLE_H + H) // 2
    pw, ph = w + 2 * _BOX_MARGIN, h + 2 * _BOX_MARGIN
    radius = 0.0
    theta = seed_index * 0.7  # 每个词一个起始角，避免全部沿同一射线排队
    for _ in range(_MAX_PLACEMENTS):
        if radius > _MAX_RADIUS:
            return None
        x = int(cx + radius * math.cos(theta) * _SPIRAL_SQUASH) - pw // 2
        y = (
            int(cy + radius * math.sin(theta) * _SPIRAL_SQUASH) - ph // 2
        )  # 纵向压扁：行比列密
        if (
            PAD - _BOX_MARGIN <= x
            and x + pw <= W - PAD + _BOX_MARGIN
            and TITLE_H + 8 - _BOX_MARGIN <= y
            and y + ph <= H - PAD + _BOX_MARGIN
        ):
            if not any(
                x < bx1 and x + pw > bx and y < by1 and y + ph > by
                for bx, by, bx1, by1 in boxes
            ):
                return x + _BOX_MARGIN, y + _BOX_MARGIN
        theta += _SPIRAL_ARC / max(radius, 10.0)
        radius += _SPIRAL_K / max(radius, 10.0)
    return None


def render_cloud_png(
    title: str,
    words: list[tuple[str, int]],
    *,
    max_words: int = 80,
) -> bytes | None:
    """渲染词云 PNG；无字体/空词表/异常返回 None。"""
    if not ensure_font_probed():
        return None
    if not words:
        return None
    try:
        return _render(title, words[: max(0, max_words)])
    except Exception:
        logger.warning("cloud render failed", exc_info=True)
        return None


def _render(title: str, words: list[tuple[str, int]]) -> bytes:
    img = Image.new("RGB", (W, H), PAPER)
    draw = ImageDraw.Draw(img)

    f_title = _font(TITLE_SIZE, bold=True)
    draw.text((PAD, PAD - 8), title, font=f_title, fill=INK, anchor="la")
    hatch(img, (PAD, TITLE_H - 12, W - PAD, TITLE_H - 6), step=5)

    max_count = max((c for _, c in words), default=0)
    boxes: list[tuple[int, int, int, int]] = []
    dropped = 0
    for idx, (word, count) in enumerate(words):
        size = _font_size(count, max_count)
        angle = _ANGLES[idx % len(_ANGLES)]
        layer, w, h = _text_layer(word, size, angle)
        pos = _place(boxes, w, h, idx)
        if pos is None:
            dropped += 1
            logger.debug("cloud: 词 %r 放置失败（画布已满），丢弃", word)
            continue
        x, y = pos
        # 墨阶上色：按词序在三档间循环（高频词先放=更大更靠中心）
        tinted = Image.new("RGBA", layer.size, (0, 0, 0, 0))
        solid = Image.new("RGBA", layer.size, _COLORS[idx % len(_COLORS)] + (255,))
        tinted.paste(solid, (0, 0), layer)
        img.paste(tinted, (x, y), tinted)
        boxes.append((x, y, x + w, y + h))
    if dropped:
        logger.info("cloud: %d 个词因画布不足被丢弃", dropped)
    if not boxes:
        return None  # 一个词都没放下：调用方走文案分支

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
