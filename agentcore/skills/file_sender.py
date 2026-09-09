"""文件发送能力：将内容保存为 markdown 文件并通过 NapCat 发送给用户。"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import httpx
from agentcore.skills.manifest import SkillManifest
from agentcore.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)


async def save_markdown_file(content: str, filename: str = "report.md") -> dict:
    """将 markdown 内容保存为临时文件，返回文件元信息。"""
    temp_dir = Path(tempfile.gettempdir()) / "agent-demo-files"
    temp_dir.mkdir(parents=True, exist_ok=True)
    file_path = temp_dir / filename
    file_path.write_text(content, encoding="utf-8")
    return {
        "path": str(file_path),
        "filename": filename,
        "size": len(content.encode("utf-8")),
    }


async def send_file_to_user(user_id: str, file_path: str, filename: str = "report.md") -> str:
    """尝试通过 NapCat HTTP API 发送文件；失败则降级为返回文本。"""
    napcat_url = os.getenv("NAPCAT_HTTP_URL", "").strip().rstrip("/")
    
    # 方案1：通过 NapCat HTTP API 上传文件（跨机器推荐）
    if napcat_url:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                with open(file_path, "rb") as f:
                    resp = await client.post(
                        f"{napcat_url}/onebot/v11/upload_private_file",
                        files={"file": (filename, f, "text/markdown")},
                        data={"user_id": int(user_id)},
                    )
                    resp.raise_for_status()
                    result = resp.json()
                    if result.get("retcode") == 0:
                        return f"文件 {filename} 已发送"
                    return f"NapCat 上传失败: {result}"
        except Exception as e:
            logger.warning("NapCat HTTP upload failed: %s", e)
            # 降级到方案2
    
    # 方案2：尝试 file:// 发送（仅 NapCat 与 agent-demo 同机时有效）
    try:
        from nonebot import get_driver
        from nonebot.adapters.onebot.v11 import MessageSegment

        driver = get_driver()
        bot = list(driver.bots.values())[0]
        file_url = f"file://{Path(file_path).absolute()}"
        await bot.send_private_msg(
            user_id=int(user_id),
            message=MessageSegment.text("📄 ") + MessageSegment(
                type="file", data={"file": file_url, "name": filename}
            ),
        )
        return f"文件 {filename} 已发送"
    except Exception as e:
        logger.warning("file:// send failed: %s", e)
        # 降级到方案3
    
    # 方案3：降级为文本
    content = Path(file_path).read_text(encoding="utf-8")
    return f"[文件发送失败，返回文本内容]\\n{content}"


def register_file_skills(registry: SkillRegistry) -> None:
    """注册文件相关 skill。"""
    
    @registry.register(
        "save_markdown_file",
        "将 markdown 内容保存为文件，用于后续发送或归档。",
        {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "要保存的 markdown 内容"},
                "filename": {"type": "string", "description": "文件名，如 report.md"},
            },
            "required": ["content"],
        },
        permission="public",
    )
    async def save_markdown_file_skill(content: str, filename: str = "report.md") -> str:
        meta = await save_markdown_file(content, filename)
        return f"文件已保存: {meta['path']} ({meta['size']} bytes)"

    @registry.register(
        "send_file_to_user",
        "将已保存的文件发送给当前用户（QQ 私聊）。",
        {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "文件路径（由 save_markdown_file 返回）"},
                "filename": {"type": "string", "description": "文件名，如 report.md"},
            },
            "required": ["file_path"],
        },
        permission="public",
    )
    async def send_file_to_user_skill(file_path: str, filename: str = "report.md", user_id: str = "") -> str:
        if not user_id:
            return "Error: missing user_id"
        return await send_file_to_user(user_id, file_path, filename)
