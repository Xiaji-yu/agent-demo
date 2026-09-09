"""QQ 图片媒体处理：从事件消息提取图片 URL，受控下载到 workspace/media/。

安全约束：
- 仅允许 https:// 的图片 URL
- 限流（每消息最多下载 MAX_PER_MESSAGE 张）、限大小、限超时
- 只接受 image/* 类型；失败时保留 URL 文本供上层提示用户
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from pathlib import Path
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)

MAX_PER_MESSAGE = 3
MAX_BYTES = 20 * 1024 * 1024  # 20MB
_TIMEOUT = 15.0

_SAFE_EXT_RE = re.compile(r"\.(jpg|jpeg|png|gif|webp|bmp|avif)$", re.IGNORECASE)


class MediaItem:
    def __init__(self, kind: str, url: str = "", file: str = ""):
        self.kind = kind
        self.url = (url or "").strip()
        self.file = (file or "").strip()

    @property
    def presentable(self) -> bool:
        return bool(self.url or self.file)


def is_allowed_image_url(url: str) -> bool:
    """只允许 https 图片链接，防止 SSRF/本地文件。"""
    if not url:
        return False
    return url.lower().startswith("https://")


def _filename_for(url: str, content_type: str = "") -> str:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    ct = (content_type or "").lower()
    if "png" in ct:
        ext = ".png"
    elif "gif" in ct:
        ext = ".gif"
    elif "webp" in ct:
        ext = ".webp"
    else:
        m = _SAFE_EXT_RE.search(url)
        ext = m.group(0).lower() if m else ".jpg"
    return f"{digest}{ext}"


async def download_image(
    url: str,
    save_dir: Path,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[Path]:
    """下载 https 图片到 save_dir；返回保存路径，失败/超限返回 None。"""
    if not is_allowed_image_url(url):
        logger.warning("image url rejected (not https): %s", url[:80])
        return None
    try:
        save_dir.mkdir(parents=True, exist_ok=True)
        own_client = client is None
        if own_client:
            client = httpx.AsyncClient(
                timeout=_TIMEOUT,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (agent-demo)"},
            )
        try:
            async with client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    logger.warning("image download http %s: %s", resp.status_code, url[:80])
                    return None
                content_type = resp.headers.get("content-type", "")
                if content_type and "image/" not in content_type and "octet-stream" not in content_type:
                    logger.warning("image download not an image: %s", content_type)
                    return None
                size = 0
                chunks = []
                async for chunk in resp.aiter_bytes(65536):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        logger.warning("image too large (>%s), skip: %s", MAX_BYTES, url[:80])
                        return None
                    chunks.append(chunk)
                if not chunks:
                    return None
                path = save_dir / _filename_for(url, content_type)
                path.write_bytes(b"".join(chunks))
                return path
        finally:
            if own_client:
                await client.aclose()
    except Exception:
        logger.exception("image download failed: %s", url[:80])
        return None


def extract_media(event) -> List[MediaItem]:
    """从事件消息提取图片/语音等媒体段（OneBot segments）。"""
    items: List[MediaItem] = []
    try:
        for seg in event.get_message():
            if seg.type == "image":
                data = getattr(seg, "data", {}) or {}
                items.append(
                    MediaItem(
                        kind="image",
                        url=str(data.get("url") or ""),
                        file=str(data.get("file") or ""),
                    )
                )
    except Exception:
        logger.exception("extract media failed")
    return items


async def handle_images_in_message(event, workspace_root: Path) -> str:
    """下载消息中的图片到 workspace/media，返回要拼进用户文本的说明（可空）。"""
    media = [m for m in extract_media(event) if m.kind == "image" and m.url]
    if not media:
        return ""
    media = media[:MAX_PER_MESSAGE]
    lines: List[str] = []
    for i, item in enumerate(media, 1):
        path = await download_image(item.url, workspace_root / "media")
        if path:
            rel = path.relative_to(workspace_root)
            lines.append(f"[图片{i} 已保存到工作区 {rel}]")
        else:
            lines.append(f"[图片{i} 下载失败，URL: {item.url}]")
    return "\n" + "\n".join(lines)


def media_display_summary(event) -> str:
    """仅提取媒体提示（不下载），用于日志等。"""
    parts = [f"{m.kind}(url={m.url[:60]})" for m in extract_media(event) if m.presentable]
    return "; ".join(parts)
