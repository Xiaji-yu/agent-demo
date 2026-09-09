"""文件发送能力：本地缓存 + NapCat HTTP API 上传，或降级为 OneBot base64://。"""
from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Any

import httpx

from agentcore.skills.manifest import SkillManifest
from agentcore.skills.registry import SkillRegistry

logger = logging.getLogger(__name__)

try:
    from nonebot import get_driver
    from nonebot.adapters.onebot.v11 import MessageSegment
except Exception:  # pragma: no cover - NoneBot 未初始化时的降级
    get_driver = None
    MessageSegment = None

DEFAULT_CACHE_DIR = Path("data/cache")
NAPCAT_HTTP_URL = (os.getenv("NAPCAT_HTTP_URL") or "").strip().rstrip("/")
NAPCAT_HTTP_TOKEN = (os.getenv("NAPCAT_HTTP_TOKEN") or "").strip()

# 文件发送成功的统一前缀，供上层（matcher 兜底等）判断
FILE_SEND_OK_PREFIX = "FILE_OK:"


def _ensure_cache_dir() -> Path:
    path = DEFAULT_CACHE_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_filename(name: str) -> str:
    base = Path(name or "report.md").name
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
        return ""
    except Exception:
        return ""


async def _napcat_upload_private_file(user_id: str, content: str, filename: str) -> str:
    """通过 NapCat HTTP API 上传私聊文件。"""
    if not NAPCAT_HTTP_URL:
        raise RuntimeError("NAPCAT_HTTP_URL not configured")

    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    payload = {
        "user_id": _safe_user_id(user_id),
        "file": f"base64://{encoded}",
        "name": _safe_filename(filename),
    }
    headers = {"Content-Type": "application/json"}
    if NAPCAT_HTTP_TOKEN:
        headers["Authorization"] = f"Bearer {NAPCAT_HTTP_TOKEN}"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{NAPCAT_HTTP_URL}/upload_private_file",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
        if str(data.get("status")) == "ok" or data.get("message_id"):
            return f"{FILE_SEND_OK_PREFIX} 文件 {_safe_filename(filename)} 已通过 NapCat HTTP 发送"
        return f"NapCat 返回异常：{data}"


async def send_markdown_file(user_id: str, content: str, filename: str = "report.md") -> str:
    """将 markdown 内容作为文件发送给用户（QQ 私聊）。优先走 NapCat HTTP API，否则降级为 OneBot base64://。"""
    cache_path = _ensure_cache_dir() / _safe_filename(filename)
    try:
        cache_path.write_text(content, encoding="utf-8")
    except Exception:
        logger.warning("write cache file failed: %s", cache_path, exc_info=True)

    if NAPCAT_HTTP_URL:
        try:
            return await _napcat_upload_private_file(user_id, content, filename)
        except Exception as e:
            logger.warning("NapCat HTTP upload failed: %s", e, exc_info=True)

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
        return f"{FILE_SEND_OK_PREFIX} 文件 {_safe_filename(filename)} 已发送"
    except Exception as e:
        logger.warning("send file failed: %s", e, exc_info=True)
        preview = content[:2000]
        suffix = "\n... (内容过长，已截断)" if len(content) > 2000 else ""
        return f"[文件发送失败，返回文本内容]\n{preview}{suffix}"


def register_file_skills(registry: SkillRegistry) -> None:
    """注册文件相关 skill。"""

    @registry.register(
        "send_markdown_file",
        "当用户要求'发文件'、'发文档'、'发md'、'整理成md文档发我'时，必须调用此 skill 发送文件。content 放完整 markdown 内容，filename 放文件名如 report.md。",
        {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "要发送的完整 markdown 内容"},
                "filename": {"type": "string", "description": "文件名，如 report.md"},
            },
            "required": ["content"],
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
