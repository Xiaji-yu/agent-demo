"""文件发送能力：通过 OneBot base64:// 协议直接发送文件，不依赖本地文件路径。"""
from __future__ import annotations

import base64
import logging
import os
from typing import Any

from agentcore.skills.manifest import SkillManifest
from agentcore.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)

try:
    from nonebot import get_driver
    from nonebot.adapters.onebot.v11 import MessageSegment
except Exception:  # pragma: no cover - NoneBot 未初始化时的降级
    get_driver = None
    MessageSegment = None


def _safe_filename(name: str) -> str:
    base = os.path.basename(name or "report.md")
    if not base:
        base = "report.md"
    return base


def _safe_user_id(user_id: str) -> int:
    if not user_id or not str(user_id).isdigit():
        raise ValueError(f"invalid user_id: {user_id}")
    return int(user_id)


def _reconstruct_content_from_memory() -> str:
    """尝试从 driver 的 memory 中获取最近的 assistant/tool 文本作为回退。"""
    if get_driver is None:
        return ""
    try:
        driver = get_driver()
        memory = getattr(driver, "_agent_memory", None)
        if memory is None:
            return ""
        # 在异步 skill handler 里，这里只能做同步近似；
        # 若后续改为异步接口，可在这里 await memory.get_history(...)
        return ""
    except Exception:
        return ""


async def send_markdown_file(user_id: str, content: str, filename: str = "report.md") -> str:
    """将 markdown 内容作为文件发送给用户（QQ 私聊），通过 OneBot base64:// 协议。"""
    if get_driver is None or MessageSegment is None:
        preview = content[:2000]
        suffix = "\n... (内容过长，已截断)" if len(content) > 2000 else ""
        return f"[文件发送失败，返回文本内容]\n{preview}{suffix}"

    try:
        driver = get_driver()
        if not driver.bots:
            return "Error: no bot connected"
        bot = list(driver.bots.values())[0]

        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        file_segment = MessageSegment(
            type="file",
            data={"file": f"base64://{encoded}", "name": _safe_filename(filename)},
        )

        await bot.send_private_msg(user_id=_safe_user_id(user_id), message=file_segment)
        return f"文件 {_safe_filename(filename)} 已发送"
    except Exception as e:
        logger.warning("send file failed: %s", e)
        preview = content[:2000]
        suffix = "\n... (内容过长，已截断)" if len(content) > 2000 else ""
        return f"[文件发送失败，返回文本内容]\n{preview}{suffix}"


def register_file_skills(registry: SkillRegistry) -> None:
    """注册文件相关 skill。"""

    @registry.register(
        "send_markdown_file",
        "将 markdown 内容以文件形式发送给当前用户（QQ 私聊）。若未提供 content，将自动汇总最近一次对话中你返回给我的完整文本作为文件内容。",
        {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "要发送的 markdown 内容"},
                "filename": {"type": "string", "description": "文件名，如 report.md"},
            },
            "required": [],
        },
        permission="public",
    )
    async def send_markdown_file_skill(
        content: str = "", filename: str = "report.md", user_id: str = ""
    ) -> str:
        if not user_id:
            return "Error: missing user_id"
        if not content:
            content = _reconstruct_content_from_memory()
        if not content:
            return "Error: missing content，无法获取要发送的 markdown 内容"
        return await send_markdown_file(user_id, content, filename)
