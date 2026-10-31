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

_FONT_DIR = Path(__file__).resolve().parents[2] / "data" / "fonts"
_DISPLAY_FONT = _FONT_DIR / "屏显臻宋-瘦金风格.ttf"
_REPO_FONT = _FONT_DIR / "wqy-zenhei.ttc"

# 项目默认字体：屏显臻宋（瘦金风格展示字体，与 xiaji-home 同源）。字形面是
# GB2312 量级（≈8k），缺 ▲▼°、假名等符号——Pillow 没有逐字形回落，缺字形
# 渲染为**零墨迹空白**（不是豆腐块），动态文本（昵称/词云词/LLM 输出）必须
# 走 font_for() 整段回落，不要直接用 font()。
_REGULAR_CANDIDATES = (
    str(_DISPLAY_FONT),
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    str(_REPO_FONT),
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/wqy-zenhei/wqy-zenhei.ttc",
    "/usr/local/share/fonts/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "C:/Windows/Fonts/msyh.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)
# 屏显臻宋没有粗体字面：粗体与常规同链（层级靠字号/斜纹表达，不靠字重），
# Noto Bold 殿后——排在前面会让标题与正文两种字面混排。
_BOLD_CANDIDATES = _REGULAR_CANDIDATES + (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
)

_font_cache: dict[tuple[int, bool], ImageFont.FreeTypeFont] = {}
_path_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}
# 每个 font path 的逐码点字形探测结果（墨迹有无），进程内复用——昵称/词云
# 词是逐条调 font_for 的热点路径，不能每次重新 getmask。
_cover_cache: dict[str, dict[int, bool]] = {}
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


def _load(path: str, size: int) -> ImageFont.FreeTypeFont:
    """加载指定路径字体（带 (path, size) 缓存）；失败抛异常。"""
    key = (path, size)
    cached = _path_font_cache.get(key)
    if cached is not None:
        return cached
    loaded = ImageFont.truetype(path, size)
    _path_font_cache[key] = loaded
    return loaded


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """按候选链加载字体（带 (size, bold) 缓存）；全部失败抛异常。"""
    key = (size, bold)
    cached = _font_cache.get(key)
    if cached is not None:
        return cached
    last_err: Exception | None = None
    for path in _BOLD_CANDIDATES if bold else _REGULAR_CANDIDATES:
        try:
            loaded = _load(path, size)
            _font_cache[key] = loaded
            return loaded
        except Exception as exc:  # 缺失/损坏都试下一个
            last_err = exc
    raise last_err or OSError("no font available")


def _covers(path: str, text: str) -> bool:
    """path 字体是否完整覆盖 text 的可见字形（空白字符天然无墨迹，不计）。

    探测手段是 getmask 墨迹而非 cmap（Pillow 不暴露 cmap）：本仓默认字体的
    .notdef 是零墨迹，缺字形渲染为空白，恰好也是探测信号。探测失败的码点
    按缺字处理（宁可回落也不要空白）。
    """
    codepoints = {ord(c) for c in text if not c.isspace()}
    if not codepoints:
        return True
    cache = _cover_cache.setdefault(path, {})
    todo = [cp for cp in codepoints if cp not in cache]
    if todo:
        try:
            probe = ImageFont.truetype(path, 16)
        except Exception:
            return False
        for cp in todo:
            try:
                cache[cp] = probe.getmask(chr(cp), mode="L").getbbox() is not None
            except Exception:
                cache[cp] = False
    return all(cache[cp] for cp in codepoints)


def font_for(text: str, size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """按文本选字体：候选链上第一个**完整覆盖 text 字形**的字体。

    默认字体（屏显臻宋）是展示字体，昵称/词云词/LLM 输出里的假名、▲▼° 等
    符号不在字面里——这类动态文本用本函数整段回落到链上后续字体（Noto /
    wqy-zenhei），而不是渲染空白。链上无可分辨覆盖时返回第一个能出墨迹的
    候选（缺字表现为可见豆腐框，如 Noto 的 .notdef；不抛异常）。固定文案
    （标签/标题常量）确认过覆盖后可直接用 font()，少一次探测。
    """
    chain = _BOLD_CANDIDATES if bold else _REGULAR_CANDIDATES
    for path in chain:
        try:
            if _covers(path, text):
                return _load(path, size)
        except Exception:  # 加载失败（文件缺失/损坏）试下一个
            continue
    return font(size, bold)
