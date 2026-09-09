import logging
import os
import re
from pathlib import Path
from nonebot import on_message
from nonebot.adapters.onebot.v11 import MessageEvent, PrivateMessageEvent, GroupMessageEvent
from .acl import is_allowed

logger = logging.getLogger(__name__)

PREFIX = os.getenv("AGENT_PREFIX", r"^[!！/]?ai\s*")
engine = None  # set by __init__.py


def _plain_text(event: MessageEvent) -> str:
    """只取消息里的 text 段（跳过图片/at 等媒体段的 CQ 码噪音）。"""
    try:
        parts = [
            str(seg.data.get("text") or "")
            for seg in event.get_message()
            if seg.type == "text"
        ]
        return "".join(parts)
    except Exception:
        return str(event.get_message())


def trigger_rule(event: MessageEvent):
    text = str(event.get_message()).strip()
    if isinstance(event, PrivateMessageEvent):
        return True
    if isinstance(event, GroupMessageEvent):
        return bool(re.match(PREFIX, text, re.IGNORECASE)) or event.is_tome()
    return False


chat_matcher = on_message(rule=trigger_rule, priority=10, block=True)


@chat_matcher.handle()
async def handle_chat(event: MessageEvent):
    if not is_allowed(event):
        await chat_matcher.finish("你没有权限使用这个功能。")

    text = _plain_text(event)
    text = re.sub(PREFIX, "", text, flags=re.IGNORECASE).strip()

    user_id = str(event.get_user_id())
    group_id = str(event.group_id) if isinstance(event, GroupMessageEvent) else None
    chat_target = f"group:{group_id}" if group_id else f"private:{user_id}"

    # ---------- 图片处理 ----------
    # vision 开启（AGENT_VISION=1）：图片以 data URI 传给多模态模型识图；
    # 超管发送的图片同时落盘 workspace/media。vision 关闭时维持「只提示/落盘」。
    from .media import data_url_from_bytes, extract_media, fetch_image_bytes, _filename_for
    from agentcore.workspace.utils import is_superuser as _is_su

    media = [m for m in extract_media(event) if m.kind == "image" and m.url]
    extra_images: list[str] = []
    if media:
        vision_on = (os.getenv("AGENT_VISION") or "0").strip() in {"1", "true", "yes", "on"}
        is_su = _is_su(user_id)
        notes: list[str] = []
        for i, item in enumerate(media[:2], 1):
            if vision_on:
                fetched = await fetch_image_bytes(item.url)
                if fetched is None:
                    notes.append(f"[图片{i} 拉取失败，URL: {item.url}]")
                    continue
                raw, content_type = fetched
                extra_images.append(data_url_from_bytes(raw, content_type))
                if is_su:
                    root = Path(os.getenv("WORKSPACE_DIR", "data/workspace")).resolve()
                    media_dir = root / "media"
                    media_dir.mkdir(parents=True, exist_ok=True)
                    p = media_dir / _filename_for(item.url, content_type)
                    p.write_bytes(raw)
                    notes.append(f"[图片{i} 已保存到工作区 {p.relative_to(root)}]")
                else:
                    notes.append(f"[图片{i} 已随消息发送给模型识图]")
            else:
                if is_su:
                    root = Path(os.getenv("WORKSPACE_DIR", "data/workspace")).resolve()
                    from .media import download_image

                    p = await download_image(item.url, root / "media")
                    if p:
                        notes.append(f"[图片{i} 已保存到工作区 {p.relative_to(root)}]")
                    else:
                        notes.append(f"[图片{i} 下载失败，URL: {item.url}]")
                else:
                    notes.append(f"[图片{i} 用户发来了图片，URL 见原始消息]")
        if notes:
            text = f"{text}\n{chr(10).join(notes)}".strip()

    logger.info("[msg] %s | user=%s | text=%s", chat_target, user_id, _truncate(text, 200))

    context = {
        "user_id": user_id,
        "group_id": group_id,
        "platform": "qq",
    }

    try:
        reply = await engine.run(context, text, extra_images=extra_images or None)
    except Exception:
        logger.exception("Agent engine failed")
        reply = None

    if not reply:
        # M0 无 LLM Key 时的回声模式，验证 NapCat ↔ NoneBot 链路
        reply = f"[echo] {text}"

    # 兜底：如果用户明确要文件，但 agent 只返回了文本，自动把这段文本作为文件发送
    # （文件内容保留原始 markdown，仅聊天文本做 QQ 纯文本化）
    if (
        _user_asked_for_file(text)
        and reply
        and not reply.startswith("[skill error]")
        and not reply.startswith("（")
        and not reply.startswith("[echo]")
    ):
        try:
            file_result = await engine.skills.execute(
                "send_markdown_file",
                user_id=user_id,
                group_id=group_id,
                content=reply,
            )
            logger.info("[auto_file] %s", file_result)
            from agentcore.skills.file_sender import FILE_SEND_OK_PREFIX

            if str(file_result).startswith(FILE_SEND_OK_PREFIX):
                reply = "文件已发送"
        except Exception:
            logger.exception("auto file send failed")

    display_text = _qq_plain(reply)
    if not display_text:
        display_text = "（回复内容为空）"

    try:
        for chunk in _split_qq_message(display_text):
            await chat_matcher.send(chunk)
            logger.info("[reply] %s | text=%s", chat_target, _truncate(chunk, 200))
    except Exception as e:
        await chat_matcher.finish(f"出错啦：{e}")


def _user_asked_for_file(text: str) -> bool:
    return any(k in text for k in ["文件", "文档", "md文档", "markdown", "发我文件", "发我文档"])


def _qq_plain(text: str) -> str:
    """QQ 聊天框不渲染 Markdown：把回复做轻量纯文本化，剥掉渲染符号但保留换行/列表。

    - 围栏代码块 ```...``` 整体保留、不做任何改写（占位符保护）
    - 仅影响 QQ 里显示的文本；以文件形式发送的内容仍是原始 markdown。
    """
    if not text:
        return text
    t = text
    code_blocks: list[str] = []

    def _protect(m):
        code_blocks.append(m.group(0))
        return f"\x01CODE{len(code_blocks) - 1}\x02"

    t = re.sub(r"```.*?```", _protect, t, flags=re.S)
    # 粗体 **x** / __x__
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t, flags=re.S)
    t = re.sub(r"__(.+?)__", r"\1", t, flags=re.S)
    # 行首标题：### 标题 / # 标题 -> 标题
    t = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", t)
    # 行首引用 > -> 空
    t = re.sub(r"(?m)^\s{0,3}>\s?", "", t)
    # 行首无序列表 * 统一成 -
    t = re.sub(r"(?m)^(\s*)\*\s+", r"\1- ", t)
    # 行内代码 `x`
    t = re.sub(r"`([^`\n]+)`", r"\1", t)
    # 链接 [text](url) -> text（url）
    t = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r"\1（\2）", t)
    # 还原代码块
    for i, b in enumerate(code_blocks):
        t = t.replace(f"\x01CODE{i}\x02", b)
    # 删除行首/行尾多余空白（保留行间换行）
    t = re.sub(r"[ \t]+\n", "\n", t)
    return t.strip()


def _split_qq_message(text: str, max_len: int = 1500) -> list[str]:
    """按句边界切分，避免在词/代码/URL 中间断开。"""
    if not text:
        return []
    if len(text) <= max_len:
        return [text]

    sentence_breaks = re.compile(r'(?<=[。！？；\n])\s*')
    segments = sentence_breaks.split(text)

    chunks: list[str] = []
    buf = ""

    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        if len(buf) + len(seg) + 1 <= max_len:
            buf = f"{buf}\n{seg}" if buf else seg
        else:
            if buf:
                chunks.append(buf.strip())
            if len(seg) <= max_len:
                buf = seg
            else:
                # 超长段落硬切：优先换行、空格、中文标点边界，避免切断词/代码
                start = 0
                while start < len(seg):
                    end = min(start + max_len, len(seg))
                    if end < len(seg):
                        cut = seg.rfind("\n", start, end)
                        if cut == -1 or cut <= start:
                            cut = seg.rfind(" ", start, end)
                        if cut == -1 or cut <= start:
                            for p in "。，；、！？：":
                                cut = seg.rfind(p, start, end)
                                if cut > start:
                                    cut += 1
                                    break
                        if cut == -1 or cut <= start:
                            cut = end
                    else:
                        cut = end
                    chunks.append(seg[start:cut].strip())
                    start = cut
                buf = ""

    if buf:
        chunks.append(buf.strip())
    # 过滤空白块（纯空格/仅符号被剥掉后可能为空）
    return [c for c in chunks if c]


def _truncate(text: str, max_len: int = 200) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."
