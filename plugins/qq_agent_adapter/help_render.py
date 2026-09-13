"""帮助菜单图片渲染：用 Pillow 把指令菜单画成卡片图发到 QQ。

- 依赖 Pillow 与系统中文字体（按候选列表探测）；任一不可用即返回 None，
  调用方（admin.handle_help）自动退回纯文本帮助。
- `AGENT_HELP_IMAGE=0` 可整体关闭图片形态。
"""

from __future__ import annotations

import io
import os
from pathlib import Path

_TRUE = {"1", "true", "yes", "on"}

_FONT_CANDIDATES = (
    # 项目本地字体（优先，基于源码文件位置定位，不依赖 CWD）
    str(Path(__file__).resolve().parents[2] / "data" / "fonts" / "wqy-zenhei.ttc"),
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "C:/Windows/Fonts/msyh.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)

# 卡片配色：浅底 + 蓝色顶栏，命令列深蓝、说明列灰
_BG = (249, 250, 247)
_ACCENT = (46, 104, 168)
_TITLE_FILL = (255, 255, 255)
_HEAD = (46, 104, 168)
_CMD = (24, 62, 112)
_TEXT = (62, 70, 82)
_MUTED = (140, 148, 158)
_DIVIDER = (226, 229, 233)
_WIDTH = 880
_PAD = 36

_SECTIONS: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "对话",
        [
            ("群聊", "@我 或以唤醒词开头；引用消息 + @我 可带图提问"),
            ("私聊", "直接发消息"),
        ],
    ),
    (
        "管理",
        [
            ("/reset", "重置当前会话"),
            ("/status", "运行状态与今日 LLM 用量"),
            ("/skills · /skill …", "查看 / 安装 / 卸载技能"),
        ],
    ),
    (
        "知识库",
        [
            ("/kb search 词", "语义检索公共知识库"),
            ("/kb stats · /kb list", "规模 / 最近来源"),
            ("/kb samples", "后台导入 kb_samples 新文档"),
            ("/kb add · file · forget · digest", "投喂 / 删除 / 蒸馏（管理员）"),
        ],
    ),
    (
        "人格与提醒",
        [
            ("/persona", "查看与切换人格"),
            ("定时提醒", "直接说「每天 9 点提醒我喝水」"),
        ],
    ),
]

_FOOTER = "「管理员」命令需 SUPERUSERS · 图片渲染失败会自动退回文本帮助"


def image_enabled() -> bool:
    return (os.getenv("AGENT_HELP_IMAGE") or "1").strip().lower() in _TRUE


def _load_font(size: int):
    for path in _FONT_CANDIDATES:
        if Path(path).is_file():
            try:
                from PIL import ImageFont

                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return None


def render_help_image() -> bytes | None:
    """渲染帮助菜单卡片，返回 PNG 字节。

    未开启、缺 Pillow、缺中文字体时返回 None，调用方退回纯文本；任何渲染异常
    都在本函数内吞掉并返回 None，绝不影响命令回复。
    """
    if not image_enabled():
        return None
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None

    title_f = _load_font(38)
    if title_f is None:  # 无中文字体
        return None
    head_f = _load_font(30)
    body_f = _load_font(25)
    small_f = _load_font(20)

    pad = _PAD
    header_h = 96
    row_h = 44
    sec_gap = 20
    height = pad + header_h + 18
    for _, rows in _SECTIONS:
        height += 50 + row_h * len(rows) + sec_gap
    height += 12 + 40 + pad

    img = Image.new("RGB", (_WIDTH, height), _BG)
    draw = ImageDraw.Draw(img)

    draw.rounded_rectangle(
        (pad, pad, _WIDTH - pad, pad + header_h), radius=16, fill=_ACCENT
    )
    draw.text((pad + 26, pad + 16), "云崽 · 使用帮助", font=title_f, fill=_TITLE_FILL)
    draw.text(
        (_WIDTH - pad - 132, pad + 30), "agent-demo", font=small_f, fill=(214, 226, 240)
    )

    y = pad + header_h + 18
    for head, rows in _SECTIONS:
        draw.text((pad + 4, y), head, font=head_f, fill=_HEAD)
        draw.line((pad, y + 42, _WIDTH - pad, y + 42), fill=_DIVIDER, width=2)
        y += 50
        desc_x = (
            pad
            + 8
            + max(int(draw.textlength(cmd, font=body_f)) for cmd, _ in rows)
            + 32
        )
        for cmd, desc in rows:
            draw.text((pad + 8, y), cmd, font=body_f, fill=_CMD)
            draw.text((desc_x, y), desc, font=body_f, fill=_TEXT)
            y += row_h
        y += sec_gap

    draw.text((pad, height - pad - 32), _FOOTER, font=small_f, fill=_MUTED)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
