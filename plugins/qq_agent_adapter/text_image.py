"""文字卡片图片 skill：渲染在 agentcore，发送走 outbound 的图片通道。

为什么 skill 注册在 plugins 而不是 ``agentcore/skills``：分层铁律——渲染是纯绘制
（``agentcore/render/text_card.py``，不依赖平台），发送需要 NoneBot/OneBot。
``agentcore/skills/file_sender.py`` 是历史豁免，不再新增第二处。

发送纪律与 ``file_sender`` 一致：

- ``IMAGE_OK:`` 成功 / ``IMAGE_UNCERTAIN:`` 结果未知（超时/断连）**绝不重发**
- 无字体、渲染失败、图片过大 → 文本降级，内容不丢
- 发送走 ``outbound._send_image``，与文本/表格图共用同一进程级节流

门禁在 ``agentcore/render/text_card.py``：6000 字软上限（截断并标注）、
12000 字硬上限（入口拒绝）、画布高 ≤4000px；渲染放线程并加 10s 超时。
**线程无法被 wait_for 取消**，所以字数门禁必须在绘制前，超时只是兜底。
"""

from __future__ import annotations

import asyncio
import logging

from nonebot import get_driver

from agentcore.render.text_card import (
    MAX_TEXT_CHARS_HARD,
    render_text_card_png,
)
from agentcore.skills.file_sender import (
    _safe_group_id,
    _safe_user_id,
    is_uncertain_send_error,
)
from agentcore.skills.registry import SkillRegistry

from .outbound import MODE_TEXT_IMAGE, OutboundThrottle, _send_image

logger = logging.getLogger(__name__)

IMAGE_SEND_OK_PREFIX = "IMAGE_OK:"
IMAGE_SEND_UNCERTAIN_PREFIX = "IMAGE_UNCERTAIN:"

RENDER_TIMEOUT_S = 10
MAX_PNG_BYTES = 5 * 1024 * 1024

_REASON_TEXT = {
    "empty": "内容为空",
    "no_font": "没有可用的中文字体",
    "too_long": "内容过长",
    "too_large": "图片高度超限",
    "render_error": "渲染异常",
}


def _text_fallback(reason: str, text: str) -> str:
    preview = text[:2000]
    suffix = "\n... (内容过长，已截断)" if len(text) > 2000 else ""
    return f"[图片渲染失败：{reason}，返回文本内容]\n{preview}{suffix}"


async def send_text_image(
    user_id: str,
    content: str,
    title: str = "",
    *,
    group_id: str | None = None,
    bot=None,
    throttle: OutboundThrottle | None = None,
) -> str:
    """渲染并发送文字卡片；返回 ``IMAGE_OK:`` / ``IMAGE_UNCERTAIN:`` / 降级文本。"""
    text = (content or "").strip()
    if not text:
        return "Error: missing content"

    if len(text) > MAX_TEXT_CHARS_HARD:
        return (
            f"Error: 内容 {len(text)} 字，超过渲染上限 {MAX_TEXT_CHARS_HARD} 字；"
            "请先摘要，或分 2–3 段发送。"
        )

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(render_text_card_png, text, title),
            RENDER_TIMEOUT_S,
        )
    except TimeoutError:
        logger.error(
            "文字卡片渲染超时（%ss，%d 字），线程仍在后台收尾",
            RENDER_TIMEOUT_S,
            len(text),
        )
        return _text_fallback("渲染超时", text)
    except Exception:
        logger.exception("文字卡片渲染异常")
        return _text_fallback("渲染异常", text)

    if result.png is None:
        return _text_fallback(
            _REASON_TEXT.get(result.reason, result.reason or "未知原因"), text
        )

    png = result.png
    if len(png) > MAX_PNG_BYTES:
        logger.warning(
            "文字卡片图片过大：%d bytes（上限 %d），降级文本", len(png), MAX_PNG_BYTES
        )
        return _text_fallback("图片过大", text)

    if bot is None:
        bot = _first_bot()
    if bot is None:
        return _text_fallback("没有已连接的 bot", text)

    try:
        if group_id:
            await _send_image(
                bot,
                "group",
                _safe_group_id(group_id),
                png,
                throttle,
                mode=MODE_TEXT_IMAGE,
            )
        else:
            await _send_image(
                bot,
                "private",
                _safe_user_id(user_id),
                png,
                throttle,
                mode=MODE_TEXT_IMAGE,
            )
    except Exception as e:
        if is_uncertain_send_error(e):
            logger.error(
                "文字卡片发送结果未确认（可能已发送，不再重发）：%s", e, exc_info=True
            )
            return f"{IMAGE_SEND_UNCERTAIN_PREFIX} 图片发送结果未确认：{e}"
        logger.warning("文字卡片发送失败：%s", e, exc_info=True)
        return f"图片发送失败：{e}\n{_text_fallback('发送失败', text)}"

    note = "（内容过长，已截断）" if result.truncated else ""
    logger.info(
        "文字卡片已发送：chat=%s chars=%d png=%d bytes truncated=%s",
        f"group:{group_id}" if group_id else f"private:{user_id}",
        result.chars,
        len(png),
        result.truncated,
    )
    return f"{IMAGE_SEND_OK_PREFIX} 图片已发送（{len(png)} bytes）{note}"


def _first_bot():
    try:
        driver = get_driver()
    except Exception:
        return None
    if not driver.bots:
        return None
    return next(iter(driver.bots.values()))


def register_text_image_skill(registry: SkillRegistry) -> None:
    """注册文字卡片渲染 skill（由 plugins 装配时调用，发送侧需要平台能力）。"""

    @registry.register(
        "render_text_image",
        "当用户要求把文字、歌词、长文、文章、清单渲染成图片/长图/卡片，"
        "或说「渲染成图片发我」「做成图」时，必须调用此 skill。"
        "content 放要渲染的完整文字（最多 12000 字，超出会被拒绝，应先摘要或分段）；"
        "title 放可选标题（如歌名、文章名）。图片会直接发到当前会话。",
        {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "要渲染成图片的完整文字，保留换行",
                },
                "title": {
                    "type": "string",
                    "description": "可选标题，如歌曲名、文章标题",
                },
            },
            "required": ["content"],
        },
        permission="public",
    )
    async def render_text_image_skill(
        content: str = "",
        title: str = "",
        user_id: str = "",
        group_id: str = "",
    ) -> str:
        if not user_id:
            return "Error: missing user_id"
        return await send_text_image(user_id, content, title, group_id=group_id or None)
