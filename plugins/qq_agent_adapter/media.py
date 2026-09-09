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


async def fetch_image_bytes(
    url: str,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[tuple[bytes, str]]:
    """拉取 https 图片到内存。返回 (bytes, content_type)；失败/超限/非图返回 None。"""
    if not is_allowed_image_url(url):
        logger.warning("image url rejected (not https): %s", url[:80])
        return None
    own_client = client is None
    try:
        if own_client:
            client = httpx.AsyncClient(
                timeout=_TIMEOUT,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (agent-demo)"},
            )
        try:
            async with client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    logger.warning("image http %s: %s", resp.status_code, url[:80])
                    return None
                content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                if content_type and content_type != "application/octet-stream" and not content_type.startswith("image/"):
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
                return b"".join(chunks), content_type
        finally:
            if own_client:
                await client.aclose()
    except Exception:
        logger.exception("image fetch failed: %s", url[:80])
        return None


async def download_image(
    url: str,
    save_dir: Path,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[Path]:
    """拉取 https 图片并写入 save_dir；返回保存路径，失败返回 None。"""
    fetched = await fetch_image_bytes(url, client=client)
    if fetched is None:
        return None
    data, content_type = fetched
    try:
        save_dir.mkdir(parents=True, exist_ok=True)
        path = save_dir / _filename_for(url, content_type)
        path.write_bytes(data)
        return path
    except Exception:
        logger.exception("image save failed: %s", url[:80])
        return None


def data_url_from_bytes(data: bytes, content_type: str = "") -> str:
    """把图片字节转成 data URI（多模态消息 content 用）。"""
    import base64

    mime = (content_type or "").split(";")[0].strip().lower()
    if not mime.startswith("image/"):
        mime = "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def data_url_for(image_path: Path) -> Optional[str]:
    """把工作区图片文件转成 data URI（多模态消息用）。"""
    try:
        ext = image_path.suffix.lower().lstrip(".")
        mime = {
            "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
            "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
        }.get(ext, "image/jpeg")
        import base64

        b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except Exception:
        logger.exception("image to data url failed")
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
