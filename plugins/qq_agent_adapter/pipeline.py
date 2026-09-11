"""消息 → 引擎 payload 的组装管线（从 matcher.py 拆出，便于测试与复用）。

职责：
- 提取用户自身文本（text 段 + json 卡片关键字段），剥离前缀
- 引用(reply)/合并转发(forward)解析：优先用适配器 _check_reply 已解析好的
  event.reply（段扫描在其后必然拿不到 reply 段），兜底再走 get_msg；
  外部内容一律加「不可信数据」围栏，防止间接提示注入
- 图片处理：直发可用图 > 引用图 > 转发图 的优先级；vision 预算内 data URI 化；
  管理员落盘（配额 + 原子写）；带图消息拉取失败不回退旧图
- 最近图片缓冲（有界 + TTL），支持「先发图、后追问」
- bot 路由：payload 记录 self_id，回复优先走触发事件的 bot（多账号不串号）
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
from urllib.parse import urlsplit

from agentcore.safety import fence_untrusted

from .media import (
    MAX_PER_MESSAGE,
    MediaItem,
    _coerce_segments,
    _filename_for,
    _forward_card_markers,
    _seg_info,
    data_url_from_bytes_async,
    download_image,
    extract_forward_id,
    fetch_image_bytes,
    is_allowed_image_url,
    media_from_segments,
    resolve_forward_content,
    resolve_quoted_media,
    save_image_atomic,
    text_from_segments,
)
from .wakewords import strip_wake_word

logger = logging.getLogger(__name__)

PREFIX = os.getenv("AGENT_PREFIX", r"^[!！/]?ai\s*")

_MAX_QUOTED_TEXT = 300

# 用户可见结果标记：生产与测试共用（文案改动只需改这里）
NOTE_URL_DIRECT = "以 URL 直传"
NOTE_SAVE_FAILED = "保存到工作区失败"
NOTE_SAVED = "已保存到工作区"

_MAX_FORWARD_TOTAL = 1500


# ---------- 环境配置 ----------
def vision_enabled() -> bool:
    return (os.getenv("AGENT_VISION") or "0").strip() in {"1", "true", "yes", "on"}


def _recent_ttl() -> float:
    try:
        return max(1.0, float(os.getenv("AGENT_RECENT_IMAGE_TTL", "180")))
    except ValueError:
        return 180.0


def recent_image_group_reuse() -> bool:
    """群聊是否允许复用「最近图片」（默认否）。

    群聊多人多话题，历史图片极易被当成当前上下文（实测：「[reply] 你怎么看」会被
    群里更早的一张图回答）。按 README 的定位该能力属私聊场景；确要开启用
    ``AGENT_RECENT_IMAGE_GROUP=1``。
    """
    raw = (os.getenv("AGENT_RECENT_IMAGE_GROUP") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _recent_entries() -> int:
    try:
        return max(1, int(os.getenv("AGENT_RECENT_IMAGE_ENTRIES", "32")))
    except ValueError:
        return 32


def _vision_max_image_bytes() -> int:
    """单图识图上限（按 LLM 请求体预算设定，与 20MB 落盘上限解耦）。"""
    try:
        return max(64 * 1024, int(os.getenv("AGENT_VISION_MAX_IMAGE_KB", "5120")) * 1024)
    except ValueError:
        return 5 * 1024 * 1024


def _vision_total_bytes() -> int:
    """单条消息识图总预算（base64 后请求体 ≈ 1.37 倍，防超大单请求）。"""
    try:
        return max(_vision_max_image_bytes(), int(os.getenv("AGENT_VISION_TOTAL_KB", "8192")) * 1024)
    except ValueError:
        return 8 * 1024 * 1024


def _media_quota_bytes() -> int:
    try:
        return max(0, int(os.getenv("AGENT_MEDIA_QUOTA_MB", "200"))) * 1024 * 1024
    except ValueError:
        return 200 * 1024 * 1024


def chat_key(user_id: str, group_id: str | None) -> str:
    return f"g:{group_id}:{user_id}" if group_id else f"p:{user_id}"


# ---------- 最近图片缓冲 ----------
class RecentImageBuffer:
    """有界 + TTL 的最近图片缓冲（存 data URI / 允许的 https URL）。

    读写块内无 await，单事件循环下天然原子；写入时惰性清理过期项并按容量
    淘汰最旧条目，避免长期运行内存无上界。
    """

    def __init__(self, ttl: float = 180.0, max_entries: int = 32, max_images: int = 2):
        self.ttl = ttl
        self.max_entries = max_entries
        self.max_images = max_images
        self._data: dict[str, dict] = {}

    def put(self, key: str, urls: list[str]) -> None:
        now = time.monotonic()
        for k in [k for k, v in self._data.items() if now - v["ts"] > self.ttl]:
            self._data.pop(k, None)
        self._data[key] = {"ts": now, "urls": list(urls)[: self.max_images]}
        while len(self._data) > self.max_entries:
            oldest = min(self._data, key=lambda k: self._data[k]["ts"])
            self._data.pop(oldest, None)

    def get(self, key: str) -> list[str] | None:
        item = self._data.get(key)
        if not item:
            return None
        if time.monotonic() - item["ts"] > self.ttl:
            self._data.pop(key, None)
            return None
        return list(item["urls"])

    def clear(self, key: str) -> None:
        """丢弃某会话缓存（M5：本条消息带图却一张都没取到时，不能让旧图继续冒用）。"""
        self._data.pop(key, None)

    def __len__(self) -> int:
        return len(self._data)


recent_images = RecentImageBuffer(ttl=_recent_ttl(), max_entries=_recent_entries())


# ---------- 文本提取 ----------
def _strip_trigger_prefix(event, text: str) -> str:
    """剥离触发指令残留：群聊先剥唤醒词（触发命中什么就剥什么），再剥旧前缀正则。

    私聊无需前缀即可对话，开头的唤醒词可能是正文本身，不剥。
    """
    if getattr(event, "group_id", None):
        text = strip_wake_word(text)
    return re.sub(PREFIX, "", text, flags=re.IGNORECASE).strip()


def _build_user_text(event) -> str:
    """提取用户自身文本：text 段 + json 卡片关键字段；剥离唤醒词/前缀。

    json/音乐卡片等结构化段的正文原先对 LLM 完全不可见，这里尽力提取。
    """
    parts: list[str] = []
    try:
        for seg in _coerce_segments(event.get_message()):
            t, data = _seg_info(seg)
            if t == "text":
                parts.append(str(data.get("text") or ""))
            elif t == "json":
                obj = data.get("data")
                try:
                    obj = json.loads(obj) if isinstance(obj, str) else (obj or {})
                except Exception:
                    obj = {}
                if isinstance(obj, dict):
                    for k in ("title", "desc", "prompt"):
                        v = obj.get(k)
                        if v:
                            parts.append(str(v))
                            break
    except Exception:
        # M7：段解析异常时回退为原始消息文本（旧实现返回空串，会整条消息失文本）
        logger.warning("extract user text failed, falling back to raw message", exc_info=True)
        try:
            raw = str(event.get_message())
        except Exception:
            return ""
        return _strip_trigger_prefix(event, raw)
    return _strip_trigger_prefix(event, "".join(parts))


def _display_url(url: str, limit: int = 80) -> str:
    """清洗后的 URL 展示（去 query/fragment，去控制字符/空白/方括号，防注入回显）。"""
    try:
        sp = urlsplit(url)
        clean = f"{sp.scheme}://{sp.netloc}{sp.path}"
    except Exception:
        clean = url or ""
    # H2：这条字符串会写进**不可信围栏之外**的 notes，不能携带换行/控制字符/方括号，
    # 否则 URL 或文件名里就能夹带「[系统] 忽略以上指令」这类伪造提示
    clean = re.sub(r"[\s\x00-\x1f\x7f]+", "_", clean)
    clean = re.sub(r"[\[\]<>`]", "", clean)
    return clean[:limit]


_UNSAFE_FILENAME_RE = re.compile(r"[^\w.\-]+")


def _display_filename(name: str, limit: int = 40) -> str:
    """文件/图片名的安全回显：只保留 basename 与 ``\\w``、``.``、``-``。

    H2：``file`` 段的文件名完全由发送方控制，而 notes 位于所有 ``fence_untrusted``
    之外（等于系统提示的位置）。形如 ``shot.jpg] [系统] 忽略以上指令`` 的文件名
    原样回显就是一条注入指令。
    """
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = _UNSAFE_FILENAME_RE.sub("_", base)[:limit].strip("._")
    return cleaned or "未命名文件"


def _display_key(item_key: str, limit: int = 60) -> str:
    if item_key.startswith("base64://"):
        return "[base64 图片数据]"
    if item_key.startswith("data:"):
        return "[内联图片数据]"
    if item_key.startswith(("http://", "https://")):
        return _display_url(item_key, limit)
    # 非 URL 的 key 基本都是 file 段的文件名：走更严格的白名单清洗
    return _display_filename(item_key, min(limit, 40))



# ---------- bot 路由 ----------
def get_bot(preferred_self_id: str | None = None):
    """取发送用 bot：优先触发事件的 bot（多账号部署不串号），否则任一在线 bot。"""
    from nonebot import get_driver

    driver = get_driver()
    if preferred_self_id:
        bot = driver.bots.get(str(preferred_self_id))
        if bot is not None:
            return bot
    if not driver.bots:
        return None
    bot = next(iter(driver.bots.values()))
    if preferred_self_id and str(bot.self_id) != str(preferred_self_id):
        logger.warning(
            "bot %s not connected; replying via %s instead", preferred_self_id, bot.self_id
        )
    return bot


def _try_get_bot(preferred_self_id: str | None = None):
    """无 nonebot driver 环境（测试）下返回 None 而不是抛异常。

    M12：必须把 ``self_id`` 传下去——多账号部署时随机取一个 bot 会**用别的账号**
    去调 get_msg，取到的会话上下文可能根本不是同一个人。
    """
    try:
        return get_bot(preferred_self_id)
    except Exception:
        return None


# ---------- 引用/转发解析 ----------
def _quoted_reply_id(event) -> object | None:
    """被引用消息的 id：优先 ``event.reply.message_id``，其次消息里的 ``reply`` 段。"""
    reply_obj = getattr(event, "reply", None)
    rid = getattr(reply_obj, "message_id", None)
    if rid not in (None, ""):
        return rid
    for seg in _coerce_segments(event.get_message()):
        t, data = _seg_info(seg)
        if t == "reply" and data.get("id"):
            return data.get("id")
    return None


async def _resolve_reply(event, bot) -> tuple[str, list[MediaItem]]:
    """返回 (被引用文本, 被引用图片)。

    H1：nonebot-adapter-onebot 的 _check_reply 在进入 matcher 前就把 reply 段从
    event.message 删除并把解析结果放入 event.reply——所以优先读 event.reply。

    但 ``event.reply`` **存在不等于有内容**：群文件方式发送的图片等承载，适配器解析出来
    的段可能为空/不可识别。此时按 reply_id 回退调 ``get_msg`` 再取一次原始消息，
    而不是直接放弃（放弃会让 prompt 里没有任何引用上下文，模型只能拿历史瞎猜）。
    """
    reply_obj = getattr(event, "reply", None)
    reply_id = _quoted_reply_id(event)
    if reply_obj is not None:
        segs = _coerce_segments(getattr(reply_obj, "message", None))
        text = text_from_segments(segs, cap=_MAX_QUOTED_TEXT)
        images = [m for m in media_from_segments(segs) if m.kind == "image"]
        if text or images:
            return text, images
        # M12：适配器解析出的段为空/不可识别时，本地其实还有一份原始 CQ 串
        # （实测形如 '[CQ:file,file=shot.jpg]'，此前从未被使用）。先解析它，
        # 能取到内容就不必再打一次 get_msg。
        raw = getattr(reply_obj, "raw_message", None)
        if raw:
            raw_segs = _coerce_segments(raw)
            raw_text = text_from_segments(raw_segs, cap=_MAX_QUOTED_TEXT)
            raw_images = [m for m in media_from_segments(raw_segs) if m.kind == "image"]
            if raw_text or raw_images:
                logger.info(
                    "引用内容由 raw_message 兜底取得：id=%s text_len=%d imgs=%d",
                    reply_id,
                    len(raw_text),
                    len(raw_images),
                )
                return raw_text, raw_images
        logger.info(
            "引用内容为空，回退 get_msg：id=%s seg_types=%s",
            reply_id,
            [_seg_info(s)[0] for s in segs][:8],
        )
    if reply_id is None:
        return "", []
    if bot is None:
        logger.warning("引用无法解析：有 reply_id=%s 但 bot 不可用", reply_id)
        return "", []
    quoted = await resolve_quoted_media(bot, reply_id)
    return quoted.get("text", ""), quoted.get("images", [])


async def _resolve_forward(bot, forward_id) -> dict:
    if bot is None:
        return {"texts": [], "images": [], "count": 0, "shown": 0}
    return await resolve_forward_content(bot, forward_id, total_cap=_MAX_FORWARD_TOTAL)


# ---------- 图片处理 ----------
def _decode_base64_file(item: MediaItem, max_bytes: int) -> tuple[bytes | None, str]:
    """解码 base64:// 图片；过大/损坏返回 (None, "")。按魔数嗅探类型。"""
    from .media import sniff_image_type

    payload = item.file[len("base64://"):]
    try:
        # 长度粗估：base64 解码后 ≈ 3/4；超限直接拒绝（防内存放大）
        if len(payload) * 3 / 4 > max_bytes:
            return None, ""
        raw = base64.b64decode(payload, validate=True)
    except Exception:
        logger.warning("image base64 decode failed", exc_info=True)
        return None, ""
    return raw, (sniff_image_type(raw) or "image/jpeg")


def _save_to_workspace_sync(key: str, raw: bytes, ctype: str) -> tuple[bool, str]:
    """管理员图片落盘（配额 + 原子写）；失败不抛出，返回 (是否成功, 相对路径或原因)。"""
    from agentcore.workspace.utils import workspace_root

    try:
        root = workspace_root()
        media_dir = root / "media"
        name = _filename_for(key, ctype)
        save_image_atomic(media_dir, name, raw, quota_bytes=_media_quota_bytes())
        return True, f"media/{name}"
    except Exception:
        logger.exception("save image to workspace failed")
        return False, ""


async def _save_to_workspace(key: str, raw: bytes, ctype: str) -> tuple[bool, str]:
    # 大图（上限 20MB）写盘放线程池，避免阻塞事件循环（L7）
    return await asyncio.to_thread(_save_to_workspace_sync, key, raw, ctype)


async def _process_images(
    media: list[MediaItem],
    quoted_imgs: list[MediaItem],
    fwd_imgs: list[MediaItem],
    user_id: str,
    notes: list[str],
) -> list[str]:
    """vision 链路：直发可用图 > 引用图 > 转发图，预算内 data URI 化或 URL 直传。"""
    from agentcore.workspace.utils import is_superuser

    is_su = is_superuser(user_id)
    ordered: list[MediaItem] = []
    seen: set[str] = set()
    for m in media + quoted_imgs + fwd_imgs:  # 优先级即插入顺序
        k = m.key
        if k and k not in seen and m.available():
            seen.add(k)
            ordered.append(m)

    unusable = [m for m in media if not m.available()]
    for j, _m in enumerate(unusable, len(ordered) + 1):
        notes.append(f"[图片{j} 无可用图片数据（url 缺失或不在白名单），已忽略]")

    extra_images: list[str] = []
    max_one = _vision_max_image_bytes()
    total_budget = _vision_total_bytes()
    used = 0
    for i, item in enumerate(ordered, 1):
        if len(extra_images) >= MAX_PER_MESSAGE:
            notes.append(f"[图片较多，仅识别前 {MAX_PER_MESSAGE} 张]")
            break
        raw: bytes | None = None
        ctype = ""
        if item.url:
            fetched = await fetch_image_bytes(item.url)
            if fetched is not None:
                raw, ctype = fetched
        if raw is None and item.file.startswith("base64://"):
            raw, ctype = _decode_base64_file(item, max_one)
            if raw is None:
                notes.append(f"[图片{i} base64 数据过大或损坏，已忽略]")
        if raw is not None:
            if len(raw) > max_one or used + len(raw) > total_budget:
                notes.append(f"[图片{i} 超出识图大小预算，已跳过]")
                raw = None
            else:
                extra_images.append(await data_url_from_bytes_async(raw, ctype))
                used += len(raw)
                if is_su:
                    ok, rel = await _save_to_workspace(item.key or f"img{i}", raw, ctype)
                    notes.append(
                        f"[图片{i} {NOTE_SAVED} {rel}]" if ok else f"[图片{i} 已识图（{NOTE_SAVE_FAILED}）]"
                    )
                else:
                    notes.append(f"[图片{i} 已随消息发送给模型识图]")
                continue
        if item.url and is_allowed_image_url(item.url):
            # 本地拉取失败兜底：https URL 直传模型（engine 只接受 data:/https:）
            extra_images.append(item.url)
            notes.append(f"[图片{i} {NOTE_URL_DIRECT}模型识图]")
        elif item.url:
            notes.append(f"[图片{i} 链接不在图片域名白名单，已忽略]")
    return extra_images


async def _download_for_su(
    media: list[MediaItem], user_id: str, notes: list[str]
) -> None:
    """非 vision 模式：管理员消息里的图片落盘（原有行为）。"""
    from agentcore.workspace.utils import is_superuser, workspace_root

    if not is_superuser(user_id):
        for i, item in enumerate(media[:MAX_PER_MESSAGE], 1):
            notes.append(f"[图片{i} 用户发来了图片（{_display_key(item.key)}）]")
        return
    root = workspace_root()
    for i, item in enumerate([m for m in media if m.url][:MAX_PER_MESSAGE], 1):
        try:
            p = await download_image(item.url, root / "media", quota_bytes=_media_quota_bytes())
        except Exception:
            logger.exception("download image failed")
            p = None
        if p:
            notes.append(f"[图片{i} {NOTE_SAVED} {p.relative_to(root)}]")
        else:
            notes.append(f"[图片{i} 下载失败，URL: {_display_url(item.url)}]")


# ---------- 主入口 ----------
async def build_payload(event, user_id: str, group_id: str | None) -> dict:
    """组装 payload。整体失败时返回降级 payload——绝不让用户消息无声消失。"""
    base = {
        "user_id": user_id,
        "group_id": group_id,
        "self_id": str(getattr(event, "self_id", "") or ""),
        "chat_target": f"group:{group_id}" if group_id else f"private:{user_id}",
    }
    try:
        return await _build(event, user_id, group_id, base)
    except Exception:
        logger.exception("prepare payload failed")
        return {
            **base,
            "text": "（消息处理出错，附加内容可能未解析；请重试或简化内容后重发）",
            "images": [],
            "user_text": "",
        }


async def _build(event, user_id: str, group_id: str | None, base: dict) -> dict:
    user_text = _build_user_text(event)

    segs = _coerce_segments(event.get_message())
    seg_types = [_seg_info(s)[0] for s in segs]
    had_image_segments = "image" in seg_types
    # 合并转发可能以 forward 段或 json 卡片两种形式到达（见 media.extract_forward_id）
    forward_id = extract_forward_id(segs)
    if forward_id is None:
        # 诊断：确认是转发卡片却取不到 id 时留痕（协议端卡片形状变化时可据此定位）
        for _seg in segs:
            _t, _d = _seg_info(_seg)
            if _t == "json":
                _view, _app = _forward_card_markers(_d)
                if _view == "forward" or "multimsg" in _app:
                    # M10：只记结构标记，绝不落卡片原始正文——正文含用户内容，
                    # 本仓既有约定是日志不写消息文本（review/FIX-6c57fd9..e86fba0.md 的 M4）
                    _raw = _d.get("data") if isinstance(_d, dict) else None
                    _keys = sorted(_raw) if isinstance(_raw, dict) else []
                    logger.warning(
                        "疑似合并转发卡片但未取到 forward id（view=%r app=%r 正文键=%s）",
                        _view,
                        _app,
                        _keys,
                    )
    direct_media = [m for m in media_from_segments(segs) if m.kind == "image"]

    notes: list[str] = []
    extra_context: list[str] = []
    quoted_imgs: list[MediaItem] = []
    fwd_imgs: list[MediaItem] = []

    # bot 仅在需要调 API 时才取；有引用时也要取——event.reply 可能"存在但内容为空"
    # （如群文件方式发送的图片），需要按 reply_id 调 get_msg 兜底（见 _resolve_reply）
    reply_obj = getattr(event, "reply", None)
    has_quote = reply_obj is not None or "reply" in seg_types
    need_bot = forward_id is not None or has_quote
    self_id = str(getattr(event, "self_id", "") or "")
    bot = _try_get_bot(self_id or None) if need_bot else None

    # ---- 引用(reply)解析：优先 event.reply ----
    quoted_text, quoted_imgs = await _resolve_reply(event, bot)
    if quoted_text or quoted_imgs:
        if quoted_text:
            # L19：统一用 agentcore.safety.fence_untrusted（所有注入点共用一个函数）
            extra_context.append(fence_untrusted("引用消息", quoted_text, "其他用户发送"))
        else:
            extra_context.append("（被引用的消息含图片，见下方图片列表）")
    elif reply_obj is not None or "reply" in seg_types:
        # 有引用却取不到任何内容：必须显式告知，否则模型只看到一个「你怎么看」，
        # 会拿对话历史/记忆瞎猜（线上实测：回答成了群里更早那张图的主题）
        logger.warning(
            "引用解析为空：reply 存在但无文字/图片（reply_obj=%s）",
            "有" if reply_obj is not None else "无",
        )
        extra_context.append("（用户引用了一条消息，但其中没有可读取的文字或图片）")

    # ---- 合并转发(forward)解析 ----
    if forward_id is not None:
        fwd = await _resolve_forward(bot, forward_id)
        fwd_imgs = fwd.get("images", []) or []
        if fwd.get("count"):
            head = f"合并转发（共 {fwd['count']} 条"
            if fwd.get("shown") and fwd["shown"] < fwd["count"]:
                head += f"，仅前 {fwd['shown']} 条摘录"
            head += "）内容："
            body = head + "；".join(fwd.get("texts") or [])
            if body != head:
                extra_context.append(fence_untrusted("合并转发消息", body, "其他用户发送"))
            else:
                # 有节点但一条文本都没有：多为纯图片/表情转发，明确告知避免"被忽略"
                extra_context.append("（对方发来一条合并转发消息，其中没有可读文本，可能全是图片）")
        elif fwd.get("error"):
            # 取不到内容此前是**静默忽略**（表现为「回复了但不理转发」）；现在留痕 + 告知
            logger.warning("合并转发内容未获取：id=%s err=%s", forward_id, fwd["error"])
            extra_context.append(
                "（对方发来一条合并转发消息，但内容获取失败，无法阅读其中文字）"
            )
        else:
            extra_context.append("（对方发来一条空的合并转发消息）")

    if extra_context:
        # 引用图随直发图一起按优先级处理；文本侧只追加围栏内容
        extra_context.append("-----（引用/转发内容结束，以下为用户本人消息）-----")

    # ---- 图片处理 ----
    extra_images: list[str] = []
    if direct_media or quoted_imgs or fwd_imgs:
        if vision_enabled():
            extra_images = await _process_images(direct_media, quoted_imgs, fwd_imgs, user_id, notes)
        else:
            await _download_for_su(direct_media, user_id, notes)
            for i, item in enumerate(quoted_imgs + fwd_imgs, len(direct_media) + 1):
                notes.append(f"[图片{i} 来自引用/转发消息（{_display_key(item.key)}）]")

    # ---- 组装文本 ----
    text = user_text
    if extra_context:
        text = ("\n".join(extra_context) + "\n" + text).strip()
    if notes:
        text = f"{text}\n{chr(10).join(notes)}".strip()

    # ---- 最近图片缓冲：收紧复用条件 ----
    # 1) 群聊默认不复用：群里多人多话题，历史图片会被当成当前上下文
    #    （实测：「[reply] 你怎么看」会用群里更早的一张图回答）；要开用
    #    AGENT_RECENT_IMAGE_GROUP=1。该能力按 README 定位本属私聊场景。
    # 2) 本条消息已带引用图/转发图或 reply 段时一律不复用：用户已明确指向另一条消息，
    #    再塞一张历史图片就是错误上下文。
    if vision_enabled():
        bkey = chat_key(user_id, group_id)
        if had_image_segments:
            if extra_images:
                recent_images.put(bkey, extra_images)
            else:
                # M5：本条消息**确实带了图段**却一张都没取到（下载失败/URL 失效）时，
                # 必须清掉缓存；否则下一条纯文本消息会复用更早那张图
                # （实测 P0 有图 → P1 带图但取不到 → P2 纯文本复用了 P0 的图）
                recent_images.clear(bkey)
        else:
            has_reply = reply_obj is not None or "reply" in seg_types
            reuse_ok = (
                not quoted_imgs
                and not fwd_imgs
                and not has_reply
                and (not group_id or recent_image_group_reuse())
            )
            if reuse_ok:
                cached = recent_images.get(bkey)
                if cached:
                    extra_images = cached
                    notes.append(f"[已自动附带最近发来的 {len(cached)} 张图片]")
                    text = f"{text}\n{chr(10).join(notes)}".strip()

    if not text.strip():
        text = "（用户没有输入文字内容）" if not extra_images else "（请结合用户发来的图片回答）"

    return {**base, "text": text, "images": extra_images, "user_text": user_text}


# ---------- 合并（防抖窗口到期后调用） ----------
def merge_parts(parts: list) -> tuple[str, list[str]]:
    """合并多条消息 payload：文本按序拼接、图片去重限 4 张。纯函数，便于测试。"""
    texts: list[str] = []
    images: list[str] = []
    for p in parts:
        t = (p.get("text") or "").strip()
        if t:
            texts.append(t)
        for img in p.get("images") or []:
            if img not in images:
                images.append(img)
    return "\n".join(texts), images[:4]
