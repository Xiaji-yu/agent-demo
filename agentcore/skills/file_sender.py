"""文件发送能力：通过 OneBot base64:// 协议直接发送文件，不依赖 NapCat HTTP。"""
from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Any

from agentcore.skills.manifest import SkillManifest
from agentcore.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)


async def send_markdown_file(user_id: str, content: str, filename: str = "report.md") -> str:
    """将 markdown 内容作为文件发送给用户（通过 OneBot base64:// 协议）。"""
    try:
        from nonebot import get_driver
        from nonebot.adapters.onebot.v11 import MessageSegment

        driver = get_driver()
        bot = list(driver.bots.values())[0]

        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        file_segment = MessageSegment(
            type="file",
            data={"file": f"base64://{encoded}", "name": filename},
        )

        await bot.send_private_msg(user_id=int(user_id), message=file_segment)
        return f"文件 {filename} 已发送"
    except Exception as e:
        logger.warning("send file failed: %s", e)
        # 降级：返回格式化文本
        preview = content[:2000]
        suffix = "\\n... (内容过长，已截断)" if len(content) > 2000 else ""
        return f"[文件发送失败，返回文本内容]\\n{preview}{suffix}"


def register_file_skills(registry: SkillRegistry) -> None:
    """注册文件相关 skill。"""

    @registry.register(
        "send_markdown_file",
        "将 markdown 内容以文件形式发送给当前用户（QQ 私聊）。",
        {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "要发送的 markdown 内容"},
                "filename": {"type": "string", "description": "文件名，如 report.md"},
                "user_id": {"type": "string", "description": "接收用户 ID（私聊 QQ 号）"},
            },
            "required": ["content", "user_id"],
        },
        permission="public",
    )
    async def send_markdown_file_skill(content: str, filename: str = "report.md", user_id: str = "") -> str:
        if not user_id:
            return "Error: missing user_id"
        return await send_markdown_file(user_id, content, filename)
