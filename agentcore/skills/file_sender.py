"""文件发送能力：优先通过 NapCat HTTP API 上传文件，支持 token 验证。"""
from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx
from agentcore.skills.manifest import SkillManifest
from agentcore.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)


async def send_markdown_file(user_id: str, content: str, filename: str = "report.md") -> str:
    """将 markdown 内容作为文件发送给用户（QQ 私聊）。"""
    # 方案1：NapCat HTTP 上传（跨机器推荐，支持 token）
    napcat_url = os.getenv("NAPCAT_HTTP_URL", "").strip().rstrip("/")
    napcat_token = os.getenv("NAPCAT_HTTP_TOKEN", "").strip()
    if napcat_url:
        try:
            return await _send_via_napcat_http(napcat_url, napcat_token, user_id, content, filename)
        except Exception as e:
            logger.warning("NapCat HTTP upload failed: %s", e)

    # 方案2：OneBot base64:// 协议（不依赖 HTTP）
    try:
        return await _send_via_base64(user_id, content, filename)
    except Exception as e:
        logger.warning("base64 send failed: %s", e)

    # 方案3：降级为文本
    preview = content[:2000]
    suffix = "\n... (内容过长，已截断)" if len(content) > 2000 else ""
    return f"[文件发送失败，返回文本内容]\n{preview}{suffix}"


async def _send_via_napcat_http(
    base_url: str, token: str, user_id: str, content: str, filename: str
) -> str:
    """通过 NapCat HTTP API 上传私聊文件。"""
    import tempfile

    temp_dir = Path(tempfile.gettempdir()) / "agent-demo-files"
    temp_dir.mkdir(parents=True, exist_ok=True)
    file_path = temp_dir / filename
    file_path.write_text(content, encoding="utf-8")

    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    payload = {
        "user_id": int(user_id),
        "file": str(file_path.absolute()),
        "name": filename,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{base_url}/onebot/v11/upload_private_file",
            headers=headers,
            content=json.dumps(payload),
        )
        resp.raise_for_status()
        result = resp.json()
        if result.get("status") == "ok" or result.get("retcode") == 0:
            return f"文件 {filename} 已发送"
        return f"NapCat 上传失败: {result}"


async def _send_via_base64(user_id: str, content: str, filename: str) -> str:
    """通过 OneBot base64:// 协议发送文件。"""
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


def register_file_skills(registry: SkillRegistry) -> None:
    """注册文件相关 skill。"""

    @registry.register(
        "send_markdown_file",
        "将 markdown 内容以文件形式发送给当前用户（QQ 私聊）。content 是你刚刚整理好的完整 markdown 文本。",
        {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "要发送的 markdown 内容"},
                "filename": {"type": "string", "description": "文件名，如 report.md"},
            },
            "required": ["content"],
        },
        permission="public",
    )
    async def send_markdown_file_skill(
        content: str, filename: str = "report.md", user_id: str = ""
    ) -> str:
        if not user_id:
            return "Error: missing user_id"
        return await send_markdown_file(user_id, content, filename)
