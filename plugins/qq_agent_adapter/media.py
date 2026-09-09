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


def _seg_info(seg):
    """兼容 pydantic Segment（.type/.data）与 dict 两种形式。"""
    if hasattr(seg, "type"):
        return str(seg.type or ""), seg.data or {}
    if isinstance(seg, dict):
        return str(seg.get("type") or ""), seg.get("data") or {}
    return "", {}


def media_from_segments(segs) -> List[MediaItem]:
    """从任意消息段列表提取图片。"""
    out: List[MediaItem] = []
    for seg in segs:
        t, data = _seg_info(seg)
        if t == "image":
            out.append(
                MediaItem(
                    kind="image",
                    url=str(data.get("url") or ""),
                    file=str(data.get("file") or ""),
                )
            )
    return out


def text_from_segments(segs, cap: int = 1500) -> str:
    """从任意消息段列表提取纯文本。"""
    parts = []
    for seg in segs:
        t, data = _seg_info(seg)
        if t == "text" and data.get("text"):
            parts.append(str(data["text"]))
    s = "".join(parts).strip()
    if len(s) > cap:
        s = s[:cap] + "…"
    return s


def _coerce_msg_id(value) -> object:
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)


async def resolve_quoted_media(bot, reply_id, max_images: int = 3) -> dict:
    """通过 get_msg 取被引用消息的内容与图片。异常返回空结构。"""
    result = {"text": "", "images": []}
    try:
        data = await bot.get_msg(message_id=_coerce_msg_id(reply_id))
        if not isinstance(data, dict):
            return result
        message = data.get("message")
        segs = list(message) if message is not None else []
        result["text"] = text_from_segments(segs, cap=300)
        result["images"] = media_from_segments(segs)[:max_images]
    except Exception:
        logger.warning("resolve quoted message failed: reply_id=%s", reply_id, exc_info=True)
    return result


def _messages_of_forward(data) -> List[object]:
    """宽容解析 get_forward_msg 返回结构（dict{messages} / list / content 形式）。"""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in ("messages", "message"):
        v = data.get(key)
        if isinstance(v, list):
            return v
    return []


async def resolve_forward_content(
    bot,
    forward_id,
    max_items: int = 15,
    per_item_cap: int = 300,
    total_cap: int = 1500,
) -> dict:
    """通过 get_forward_msg 取合并转发内容：逐条文本 + 图片。异常返回空结构。"""
    result = {"texts": [], "images": [], "count": 0}
    try:
        data = await bot.get_forward_msg(message_id=_coerce_msg_id(forward_id))
        messages = _messages_of_forward(data)
        if not messages:
            return result
        messages = messages[:max_items]
        texts: List[str] = []
        images: List[MediaItem] = []
        total = 0
        for item in messages:
            if not isinstance(item, dict):
                continue
            body = item.get("message", item.get("content"))
            if body is None:
                continue
            segs = list(body) if not isinstance(body, list) else body
            t = text_from_segments(segs, cap=per_item_cap)
            if t and total < total_cap:
                texts.append(t)
                total += len(t)
            for im in media_from_segments(segs):
                images.append(im)
        result["texts"] = texts
        result["images"] = images[:3]
        result["count"] = len(messages)
    except Exception:
        logger.warning("resolve forward failed: forward_id=%s", forward_id, exc_info=True)
    return result


def extract_media(event) -> List[MediaItem]:
    """从事件消息提取图片/语音等媒体段（OneBot segments）。"""
    try:
        return media_from_segments(event.get_message())
    except Exception:
        logger.exception("extract media failed")
        return []


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
