"""中文字体加载：候选链 + 探测 + 缓存（所有 Pillow 渲染图共用）。

从 ``overview.py`` 抽出：概览图（overview）、发言榜卡片（rank）、词云（cloud）
都需要中文字体，三处各维护一份候选链必然漂移——某个环境装了 Noto 而另一个
只认 wqy 时，会出现"有的图能画有的不能"的灵异故障。

探测语义（照搬 overview 原实现）：
- 首次调用打**一条**汇总 WARNING，之后缓存（不逐条刷屏，评审 L-4）
- 候选全部不可用时 :func:`ensure_font_probed` 返回 False，
  **调用方必须降级为文本**而不是抛异常

``table.py`` / ``help_render.py`` 各有一份更早的候选链（测试直接 import 它们的
私有名），不在本期合并范围内——合并是纯重构，留给单独的提交做。
"""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import ImageFont

logger = logging.getLogger(__name__)

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


def ensure_font_probed() -> bool:
    """首次调用探测字体链并打**一条**汇总 WARNING（之后缓存）。"""
    global _font_probe_done, _font_ok
    if _font_probe_done:
        return _font_ok
    _font_probe_done = True
    try:
        font(14)
        _font_ok = True
    except Exception:
        _font_ok = False
        logger.warning(
            "render: 候选字体链全部不可用，相关图片将降级为文本。候选：%s",
            ", ".join(_REGULAR_CANDIDATES),
        )
    return _font_ok


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """按候选链加载字体（带 (size, bold) 缓存）；全部失败抛异常。"""
    key = (size, bold)
    cached = _font_cache.get(key)
    if cached is not None:
        return cached
    last_err: Exception | None = None
    for path in _BOLD_CANDIDATES if bold else _REGULAR_CANDIDATES:
        try:
            loaded = ImageFont.truetype(str(path), size)
            _font_cache[key] = loaded
            return loaded
        except Exception as exc:  # 缺失/损坏都试下一个
            last_err = exc
    raise last_err or OSError("no font available")
