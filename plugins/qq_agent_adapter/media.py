"""QQ 图片媒体处理：从事件消息提取图片，受控下载/识图。

安全约束：
- 仅允许 https:// 且域名命中白名单（AGENT_IMAGE_HOSTS，逗号分隔后缀；
  默认 QQ 图床系域名）。**显式置空不再等于「允许任意域名」**（旧语义可被一个
  空环境变量整体关闭 SSRF 防护）：空值回落默认白名单，确需放开须显式设置
  ``AGENT_IMAGE_ALLOW_ANY_HOST=1``（M3）
- 域名白名单之外再做 IP 层校验：连接前解析域名，任一解析结果落在
  内网/回环/链路本地/保留段即拒绝（防 DNS rebinding 打内网与云元数据）
- 重定向不自动跟随：逐跳重新过白名单 + IP 校验（follow_redirects=False，最多 3 跳）
- 单张图片整体 deadline（30s）+ 流式大小上限 + 只接受 image/*
- 缺 content-type / application/octet-stream 时按魔数嗅探，嗅探不出即拒绝
- 日志不记录引用消息的原始结构与图片 URL（M4）
- get_msg/get_forward_msg 参数名（message_id/id）与返回结构（dict/pydantic/
  嵌套 data/CQ 码字符串）做宽容兼容

残留风险：IP 校验与实际连接是两次独立解析，理论上仍存在 DNS rebinding 的
时间窗（需先控制白名单内的子域）。彻底方案为固定解析结果后连接，待后续处理。
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

MAX_PER_MESSAGE = 3           # 每条消息最多处理的图片数（识图/下载共用，单一事实来源）
MAX_BYTES = 20 * 1024 * 1024  # 下载落盘单图上限
_TIMEOUT = 15.0               # httpx 单操作超时
_TOTAL_DEADLINE = 30.0        # 单张图片整体 deadline（含重定向）
_MAX_REDIRECTS = 3

_DEFAULT_HOSTS = "qpic.cn,qq.com,qq.com.cn,gtimg.cn,gtimg.com,idqqimg.com,qlogo.cn"
_SAFE_EXT_RE = re.compile(r"\.(jpg|jpeg|png|gif|webp|bmp|avif)$", re.IGNORECASE)


class MediaItem:
    def __init__(self, kind: str, url: str = "", file: str = ""):
        self.kind = kind
        self.url = (url or "").strip()
        self.file = (file or "").strip()

    @property
    def key(self) -> str:
        """去重键：url 优先，其次 file。"""
        return self.url or self.file or ""

    def available(self) -> bool:
        """是否具备可处理的图片数据（https 白名单内 url 或 base64 file）。"""
        return (self.url and is_allowed_image_url(self.url)) or self.file.startswith("base64://")


def _allowed_hosts() -> list[str]:
    """图片域名白名单。

    未设置或显式置空都回落默认 QQ 图床白名单——旧实现把空串当作「允许任意域名」，
    一个空环境变量即可整体关闭 SSRF 防护（M3）。
    """
    raw = os.environ.get("AGENT_IMAGE_HOSTS")
    if raw is None or not raw.strip():
        raw = _DEFAULT_HOSTS
    return [h.strip().lower() for h in raw.split(",") if h.strip()]


def _allow_any_host() -> bool:
    """显式放开域名白名单的开关（仍需通过 IP 层校验）。"""
    return (os.environ.get("AGENT_IMAGE_ALLOW_ANY_HOST") or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _is_forbidden_ip(ip: str) -> bool:
    """内网/回环/链路本地/保留/组播/未指定地址一律不可访问。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # 解析不出，按不安全处理
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


async def host_ips_are_safe(host: str) -> tuple[bool, str]:
    """解析 host 的全部地址，任一落在内网/保留段即判定不安全（防 SSRF/元数据访问）。

    DNS 解析放在线程池，避免阻塞事件循环。
    """
    if not host:
        return False, "空 host"
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, host, None, proto=socket.IPPROTO_TCP
        )
    except OSError as e:
        return False, f"域名无法解析：{e.strerror or e}"
    ips = {info[4][0] for info in infos if info[4]}
    if not ips:
        return False, "域名无解析结果"
    for ip in sorted(ips):
        if _is_forbidden_ip(ip):
            return False, f"目标解析到内网/保留地址：{ip}"
    return True, ""


def is_allowed_image_url(url: str) -> bool:
    """只允许 https 且域名命中白名单的图片链接，防 SSRF/本地文件/重定向逃逸。"""
    if not url or not url.lower().startswith("https://"):
        return False
    try:
        host = (httpx.URL(url).host or "").lower()
    except Exception:
        return False
    if not host:
        return False
    if _allow_any_host():
        return True
    hosts = _allowed_hosts()
    return any(host == h or host.endswith("." + h) for h in hosts)


def sniff_image_type(data: bytes) -> str:
    """按魔数识别常见图片格式；不是可识别图片返回空串。"""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"GIF8":
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


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


def _decide_content_type(data: bytes, content_type: str) -> str | None:
    """缺失或 octet-stream 时魔数嗅探；非 image 一律拒绝。"""
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in ("", "application/octet-stream"):
        sniffed = sniff_image_type(data)
        if not sniffed:
            logger.warning("image content-type missing/unknown and sniff failed")
            return None
        return sniffed
    if not ct.startswith("image/"):
        logger.warning("image download not an image: %s", ct)
        return None
    return ct


async def fetch_image_bytes(
    url: str,
    client: httpx.AsyncClient | None = None,
) -> tuple[bytes, str] | None:
    """拉取白名单内 https 图片到内存。返回 (bytes, content_type)；失败/超限/非图返回 None。

    重定向不自动跟随：逐跳校验（每跳都必须 https + 域名白名单），整体有 deadline。
    """
    if not is_allowed_image_url(url):
        logger.warning("image url rejected (not https / not in allowlist): %s", url[:80])
        return None
    own_client = client is None
    try:
        async with asyncio.timeout(_TOTAL_DEADLINE):
            if own_client:
                client = httpx.AsyncClient(
                    timeout=_TIMEOUT,
                    follow_redirects=False,
                    headers={"User-Agent": "Mozilla/5.0 (agent-demo)"},
                )
            try:
                current = url
                for _hop in range(_MAX_REDIRECTS + 1):
                    # 每跳都做 IP 层校验：白名单域名也可能被解析/重定向到内网（M3）
                    safe, why = await host_ips_are_safe(
                        httpx.URL(current).host or ""
                    )
                    if not safe:
                        logger.warning(
                            "image host rejected (%s): %s", why, current[:80]
                        )
                        return None
                    async with client.stream("GET", current) as resp:
                        if resp.status_code in (301, 302, 303, 307, 308):
                            loc = resp.headers.get("location") or ""
                            current = str(httpx.URL(current).join(loc))
                            if not is_allowed_image_url(current):
                                logger.warning("image redirect rejected: %s", current[:80])
                                return None
                            continue
                        if resp.status_code >= 400:
                            logger.warning("image http %s: %s", resp.status_code, url[:80])
                            return None
                        content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                        size = 0
                        chunks = []
                        async for chunk in resp.aiter_bytes(65536):
                            size += len(chunk)
                            if size > MAX_BYTES:
                                logger.warning("image too large (>%s), skip: %s", MAX_BYTES, url[:80])
                                return None
                            chunks.append(chunk)
                        data = b"".join(chunks)
                        if not data:
                            return None
                        ct = _decide_content_type(data, content_type)
                        if ct is None:
                            return None
                        return data, ct
                logger.warning("too many redirects: %s", url[:80])
                return None
            finally:
                if own_client:
                    await client.aclose()
    except TimeoutError:
        logger.warning("image fetch deadline (%ss) exceeded: %s", _TOTAL_DEADLINE, url[:80])
        return None
    except Exception:
        logger.exception("image fetch failed: %s", url[:80])
        return None


def save_image_atomic(
    save_dir: Path,
    name: str,
    data: bytes,
    quota_bytes: int | None = None,
) -> Path:
    """原子写盘（tmp + os.replace）；提供配额时先按最旧优先清理目录。"""
    save_dir.mkdir(parents=True, exist_ok=True)
    if quota_bytes:
        prune_media_dir(save_dir, quota_bytes, incoming=len(data))
    path = save_dir / name
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return path


def prune_media_dir(media_dir: Path, quota_bytes: int, incoming: int = 0) -> None:
    """目录总大小超过配额时按最旧优先删除，直到容纳 incoming 后仍不超配额。"""
    try:
        files = [
            (p.stat().st_mtime, p.stat().st_size, p)
            for p in media_dir.iterdir()
            if p.is_file() and not p.name.endswith(".part")
        ]
    except OSError:
        return
    total = sum(s for _, s, _ in files)
    if total + incoming <= quota_bytes:
        return
    for _mtime, size, p in sorted(files):
        if total + incoming <= quota_bytes:
            break
        try:
            p.unlink()
            total -= size
        except OSError:
            logger.exception("media prune failed for %s", p)


async def download_image(
    url: str,
    save_dir: Path,
    client: httpx.AsyncClient | None = None,
    quota_bytes: int | None = None,
) -> Path | None:
    """拉取 https 图片并原子写入 save_dir；返回保存路径，失败返回 None。"""
    fetched = await fetch_image_bytes(url, client=client)
    if fetched is None:
        return None
    data, content_type = fetched
    try:
        return await asyncio.to_thread(
            save_image_atomic, save_dir, _filename_for(url, content_type), data, quota_bytes
        )
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


async def data_url_from_bytes_async(data: bytes, content_type: str = "") -> str:
    """data URI 的异步版本：base64 编码大图是 CPU 密集的同步操作，放线程池执行（L2）。"""
    return await asyncio.to_thread(data_url_from_bytes, data, content_type)


def _seg_info(seg):
    """兼容 pydantic Segment（.type/.data）与 dict 两种形式。"""
    if hasattr(seg, "type"):
        return str(seg.type or ""), seg.data or {}
    if isinstance(seg, dict):
        return str(seg.get("type") or ""), seg.get("data") or {}
    return "", {}


def _coerce_segments(body) -> list[object]:
    """把段列表 / pydantic Message / CQ 码字符串统一成段列表。

    部分协议端会以 CQ 码字符串回传消息体；直接 list(str) 会得到单字符列表，
    这里显式按 OneBot v11 Message 解析，解析失败返回空并告警。
    """
    if body is None:
        return []
    if isinstance(body, list):
        return body
    if isinstance(body, str):
        if not body.strip():
            return []
        try:
            from nonebot.adapters.onebot.v11 import Message

            return list(Message(body))
        except Exception:
            logger.warning("failed to parse CQ-string message body (%d chars)", len(body))
            return []
    try:
        return list(body)  # pydantic Message 可迭代
    except Exception:
        return []


_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}


def _image_like_file_name(data) -> str:
    """file 段里「看起来是图片」的文件名（含扩展名），否则空串。

    NapCat 在群里常把「图片以文件发送」表示成 ``file`` 段，
    被引用/转发消息里尤其常见——只认 ``image`` 段的实现在这类消息上取不到任何东西。
    """
    if not isinstance(data, dict):
        return ""
    for key in ("file", "name", "file_name"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            name = value.strip()
            if Path(name).suffix.lower() in _IMAGE_EXTS:
                return name
    return ""


def media_from_segments(segs) -> list[MediaItem]:
    """从任意消息段列表提取图片（含以 file 段发送的图片）。"""
    out: list[MediaItem] = []
    for seg in _coerce_segments(segs):
        t, data = _seg_info(seg)
        if t == "image":
            out.append(
                MediaItem(
                    kind="image",
                    url=str(data.get("url") or ""),
                    file=str(data.get("file") or ""),
                )
            )
        elif t == "file":
            name = _image_like_file_name(data)
            if name:
                out.append(
                    MediaItem(
                        kind="image",
                        url=str(data.get("url") or ""),
                        file=name,
                    )
                )
    return out


def text_from_segments(segs, cap: int = 1500) -> str:
    """从任意消息段列表提取纯文本；``file`` 段给可读占位。

    占位（``[文件：x.jpg]``）用于**引用/转发**内容：``file`` 段此前完全不可见，
    会让「引用了一条图片/文件消息」在 prompt 里变成空的引用上下文，模型只能拿历史瞎猜。
    图片段不在这里加占位——它们本就走 ``media_from_segments`` 的图片通道。
    注意：用户本人文本不走这里（见 ``pipeline._build_user_text``），不会污染 user_text。
    """
    parts = []
    for seg in _coerce_segments(segs):
        t, data = _seg_info(seg)
        if t == "text" and data.get("text"):
            parts.append(str(data["text"]))
        elif t == "file":
            name = _image_like_file_name(data) or str(data.get("file") or "").strip()
            parts.append(f"[文件：{name}]" if name else "[文件]")
    s = "".join(parts).strip()
    if len(s) > cap:
        s = s[:cap] + "…"
    return s


def _coerce_msg_id(value) -> object:
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)


def _as_dict(obj) -> dict:
    """兼容 dict / pydantic（.dict()/.model_dump()）。"""
    if isinstance(obj, dict):
        return obj
    for method in ("model_dump", "dict"):
        fn = getattr(obj, method, None)
        if callable(fn):
            try:
                out = fn()
                if isinstance(out, dict):
                    return out
            except Exception:
                pass
    return {}


def _find_segments(data, _depth: int = 0) -> list[object]:
    """宽容地取消息段列表：list / dict.message / dict.data.message / CQ 码字符串 / pydantic。"""
    if _depth > 5:  # 防间接循环引用导致 RecursionError
        return []
    if isinstance(data, list):
        return data
    d = _as_dict(data)
    v = d.get("message")
    if v is not None:
        segs = _coerce_segments(v)
        if segs or isinstance(v, str | list):
            return segs
    nested = d.get("data")
    if isinstance(nested, dict) and nested is not d:
        return _find_segments(nested, _depth + 1)
    return []


async def resolve_quoted_media(bot, reply_id, max_images: int = MAX_PER_MESSAGE) -> dict:
    """通过 get_msg 取被引用消息的内容与图片。异常返回空结构。"""
    result = {"text": "", "images": []}
    try:
        data = await bot.get_msg(message_id=_coerce_msg_id(reply_id))
    except Exception:
        logger.warning("resolve quoted message failed: reply_id=%s", reply_id, exc_info=True)
        return result
    segs = _find_segments(data)
    images = media_from_segments(segs)
    result["text"] = text_from_segments(segs, cap=300)
    result["images"] = images[:max_images]
    if not result["text"] and not result["images"]:
        # 定位「引用群文件图片」等形状的直接证据；只记形状不记内容（隐私约束 M4）
        logger.warning(
            "get_msg 引用内容仍为空：reply_id=%s shape=%s seg_types=%s",
            reply_id,
            type(data).__name__,
            [_seg_info(s)[0] for s in segs][:12],
        )
    # 只记结构统计与段类型，不记原始结构/图片 URL（避免用户内容落日志，M4）
    logger.debug(
        "quoted msg=%s shape=%s segs=%d text_len=%d imgs=%d(url=%d) types=%s",
        reply_id,
        type(data).__name__,
        len(segs),
        len(result["text"]),
        len(images),
        sum(1 for m in images if m.url),
        [_seg_info(s)[0] for s in segs][:12],
    )
    return result


def _json_card_payload(data) -> dict:
    """json 段的内层对象：``data.data`` 可能是 JSON 字符串，也可能是 dict。"""
    raw = data.get("data") if isinstance(data, dict) else None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return {}
    return raw if isinstance(raw, dict) else {}


def _find_forward_resid(obj, _depth: int = 0) -> str | None:
    """在 json 卡片里递归找合并转发的 resid（QQ/NapCat 的 multimsg 卡片）。"""
    if _depth > 4 or not isinstance(obj, dict):
        return None
    for key in ("resid", "resId", "res_id", "forward_id", "file"):
        v = obj.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    for v in obj.values():
        found = _find_forward_resid(v, _depth + 1)
        if found:
            return found
    return None


def extract_forward_id(segs) -> str | None:
    """从消息段里提取合并转发 id，兼容两种承载方式。

    - **OneBot v11 标准**：``{"type": "forward", "data": {"id": "..."}}``
      （部分实现用 ``message_id`` / ``resid`` / ``file`` 作键名）
    - **QQ/NapCat 卡片**：合并转发有时被包成 ``json`` 段
      （``app=com.tencent.multimsg`` / ``view=Forward``，正文里带 ``resid``）；
      此前只认 ``forward`` 段，这类消息会被当成普通卡片而**取不到转发内容**。

    未识别返回 None。
    """
    for seg in segs or []:
        stype, data = _seg_info(seg)
        if stype == "forward":
            for key in ("id", "message_id", "resid", "file"):
                value = data.get(key)
                if value not in (None, ""):
                    return str(value)
        elif stype == "json":
            payload = _json_card_payload(data)
            if not payload:
                continue
            view = str(payload.get("view") or "").lower()
            app = str(payload.get("app") or "").lower()
            # 只对确认是「合并转发」的卡片取 resid，避免把普通分享卡片误当转发
            if view == "forward" or "multimsg" in app:
                rid = _find_forward_resid(payload)
                if rid:
                    return rid
    return None


def _looks_like_forward_card(data) -> bool:
    """诊断用：json 段是否**看起来**是合并转发卡片（用于取不到 id 时留痕）。"""
    raw = data.get("data") if isinstance(data, dict) else None
    text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False) if raw else ""
    return "Forward" in text or "multimsg" in text


def _messages_of_forward(data, _depth: int = 0) -> list[object]:
    """宽容解析 get_forward_msg 返回结构（dict{messages} / list / 嵌套 data / pydantic）。"""
    if _depth > 5:
        return []
    if isinstance(data, list):
        return data
    d = _as_dict(data)
    for key in ("messages", "message"):
        v = d.get(key)
        if isinstance(v, list):
            return v
    nested = d.get("data")
    if isinstance(nested, dict) and nested is not d:
        return _messages_of_forward(nested, _depth + 1)
    return []


async def _call_forward_api(bot, forward_id):
    """get_forward_msg 参数名兼容：先 message_id，失败再试标准参数名 id。

    go-cqhttp/NapCat/Lagrange 接受 message_id；严格按 OneBot v11 标准实现的
    协议端只接受 id。
    """
    try:
        return await bot.get_forward_msg(message_id=_coerce_msg_id(forward_id))
    except Exception:
        return await bot.get_forward_msg(id=_coerce_msg_id(forward_id))


def _forward_item_segments(item) -> list:
    """单个转发节点 → 消息段列表（兼容多种承载）。

    这是「合并转发能取到但内容为空」的根因所在：OneBot v11 的
    ``get_forward_msg`` 返回的每个元素是 **node 段**
    （``{"type": "node", "data": {"content": [...]}}``），文本在
    ``data.content`` 里，而旧实现只找 ``item["message"]`` / ``item["content"]``。

    兼容：
    - node 段：``{"type":"node","data":{"content":[…]}}``
    - 旧/简化格式：``{"message":[…]}`` / ``{"content":[…]}``
    - 元素本身就是段列表：``[{...}, {...}]``
    - 元素本身是段 dict（无外层包装且像消息段）：原样返回
    """
    if isinstance(item, list):
        return item
    if not isinstance(item, dict):
        return []
    node_type = str(item.get("type") or "")
    if node_type == "node":
        data = _as_dict(item.get("data"))
        if isinstance(data, dict):
            body = data.get("content")
            if body is None:
                body = data.get("message")
            return _coerce_segments(body)
        return []
    body = item.get("message")
    if body is None:
        body = item.get("content")
    if body is None and node_type:
        # 元素本身就是消息段（如 {"type":"text","data":{...}}）
        return [item]
    return _coerce_segments(body)


async def resolve_forward_content(
    bot,
    forward_id,
    max_items: int = 15,
    per_item_cap: int = 300,
    total_cap: int = 1500,
) -> dict:
    """通过 get_forward_msg 取合并转发内容：逐条文本 + 图片。

    count 为转发内消息总数（截断前），shown 为实际摘录条数——避免「共 40 条
    只摘 15 条」被误报成 15 条。
    ``error`` 非空表示**没取到**（无 bot / API 失败 / 返回结构不认），
    调用方据此给出可见提示而不是静默忽略。
    """
    result = {"texts": [], "images": [], "count": 0, "shown": 0, "error": ""}
    if bot is None:
        result["error"] = "no bot available to call get_forward_msg"
        logger.warning("resolve forward: bot 不可用，无法拉取 forward_id=%s", forward_id)
        return result
    try:
        data = await _call_forward_api(bot, forward_id)
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        logger.warning("resolve forward failed: forward_id=%s err=%s", forward_id, e, exc_info=True)
        return result
    messages = _messages_of_forward(data)
    result["count"] = len(messages)
    if not messages:
        shape = _as_dict(data)
        result["error"] = f"unrecognized payload ({type(data).__name__})"
        logger.warning(
            "forward 返回结构未识别：forward_id=%s type=%s keys=%s",
            forward_id,
            type(data).__name__,
            list(shape.keys())[:8] if isinstance(shape, dict) else [],
        )
        return result
    texts: list[str] = []
    images: list[MediaItem] = []
    total = 0
    extracted_items = 0
    for item in messages[:max_items]:
        segs = _forward_item_segments(item)
        if not segs:
            continue
        extracted_items += 1
        t = text_from_segments(segs, cap=per_item_cap)
        if t and total < total_cap:
            texts.append(t)
            total += len(t)
        images.extend(media_from_segments(segs))
    if extracted_items == 0:
        result["error"] = "nodes present but no recognizable segments"
        logger.warning(
            "forward 有 %d 个节点但未解析出任何段：forward_id=%s sample_type=%s",
            result["count"],
            forward_id,
            type(messages[0]).__name__,
        )
    result["texts"] = texts
    result["images"] = images[:MAX_PER_MESSAGE]
    result["shown"] = len(messages[:max_items])
    logger.debug(
        "forward msg=%s shape=%s count=%d shown=%d extracted=%d",
        forward_id, type(data).__name__, result["count"], result["shown"], extracted_items,
    )
    return result


def extract_media(event) -> list[MediaItem]:
    """从事件消息提取图片媒体段（OneBot segments）。"""
    try:
        return media_from_segments(event.get_message())
    except Exception:
        logger.exception("extract media failed")
        return []
