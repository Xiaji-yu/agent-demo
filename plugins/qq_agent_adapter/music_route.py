"""点歌路由：唤醒词 + 子命令触发，**确定性**放歌，LLM 不参与判断。

为什么不走 LLM skill：是否放歌是有副作用的群行为（群里多一条语音、占用约 7.8s
上传、烧掉 30s 账号级冷却）。交给模型判断既不保证触发也不保证不误触，而一条
「唤醒词 + 点歌 + 歌名」的规则是确定的——这是本文件存在的全部理由。

触发是**两层**条件，缺一不触发：

1. 与 ``matcher.trigger_rule`` 同款入口门槛：群聊必须命中唤醒词或 @机器人
   （遵守 AGENTS.md:103 的路由不变量，``点歌`` 是唤醒词之后的**子命令**，
   不是被禁的 ``ai/!ai//ai`` 式触发前缀）；
2. 剥掉唤醒词后，文本以音乐子命令开头（``AGENT_MUSIC_COMMANDS``，默认「点歌,放歌」）。

本模块只在闸门通过时才被 ``__init__._load_plugin_modules`` 导入——未配置时
根本不加载，也就没有 matcher，该消息落给普通聊天，对核心零影响。
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from nonebot import on_message
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent

from agentcore.music import (
    Song,
    UnsafeURLError,
    default_cooldown,
    encode_to_silk,
    fetch_audio,
    filter_by_duration,
    is_group_allowed,
    search,
    send_group_voice,
    song_url,
)
from agentcore.music.silk import silk_available

from .acl import is_allowed
from .matcher import _is_self_message, _plain_text, trigger_rule
from .wakewords import strip_wake_word

logger = logging.getLogger(__name__)

DEFAULT_COMMANDS = "点歌,放歌"
DEFAULT_MAX_SECONDS = 300
DEFAULT_CACHE_MB = 200
_CACHE_QUOTA_BYTES = DEFAULT_CACHE_MB * 1024 * 1024


def _env_cache_mb(name: str, default: int) -> int:
    """磁盘缓存配额（MB）；脏值/负值告警后回退默认（M8：该 env 此前**从未被读取**）。"""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r 不是整数，回退 %dMB", name, raw, default)
        return default
    if value < 0:
        logger.warning("%s=%r 为负，回退 %dMB", name, raw, default)
        return default
    return value


# ---------- 配置 ----------
def music_commands() -> list[str]:
    raw = (os.getenv("AGENT_MUSIC_COMMANDS") or DEFAULT_COMMANDS).strip()
    return [c.strip() for c in raw.split(",") if c.strip()]


def max_seconds() -> int:
    """时长上限（秒）。0 = 不限制；**负值/脏值回退默认**（L10）。

    旧实现用 ``max(0, int(raw))`` 把 -1 变成 0 —— 0 在 ``filter_by_duration``
    里是"不过滤"，等于一个手滑的负号**静默关掉了 5 分钟上限**（fail-open），
    且与 ``cooldown_seconds()`` 的负值回退语义相反。
    """
    raw = (os.getenv("AGENT_MUSIC_MAX_SECONDS") or "").strip()
    if not raw:
        return DEFAULT_MAX_SECONDS
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "AGENT_MUSIC_MAX_SECONDS=%r 不是整数，回退 %d", raw, DEFAULT_MAX_SECONDS
        )
        return DEFAULT_MAX_SECONDS
    if value < 0:
        logger.warning(
            "AGENT_MUSIC_MAX_SECONDS=%r 为负，回退默认 %d（0 才表示不限制）",
            raw,
            DEFAULT_MAX_SECONDS,
        )
        return DEFAULT_MAX_SECONDS
    return value


# ---------- 触发解析 ----------
def parse_command(text: str) -> tuple[str, str] | None:
    """剥唤醒词后再认子命令，返回 ``(子命令, 歌名)``；未命中返回 None。"""
    body = strip_wake_word(text).strip()
    for cmd in music_commands():
        if body == cmd:
            return cmd, ""
        if body.startswith(cmd):
            rest = body[len(cmd) :].lstrip(" ：:，,").strip()
            return cmd, rest
    return None


def _rule(event: MessageEvent) -> bool:
    # 复用主路由的入口门槛（唤醒词 / @bot / 自身消息过滤）
    if not trigger_rule(event):
        return False
    parsed = parse_command(_plain_text(event))
    if parsed is None:
        return False
    if isinstance(event, GroupMessageEvent):
        # 群白名单放在**规则**里而不是 handler 里：未授权群的规则直接不命中，
        # 消息落给普通聊天（静默，不报错、不暴露功能存在）
        if not is_group_allowed(event.group_id):
            return False
    else:
        # M9（REVIEW-6ec3f7c..a36ea1d）：私聊必须有 ACL 门。旧实现只对群做白名单，
        # 私聊**完全不过** is_allowed —— 而主聊天路径（matcher.py:169）对私聊是
        # "仅 superuser"。否则任何能给 bot 发私聊的人都能驱动 ≤20MB 下载 +
        # ffmpeg/pysilk 编码，并占用唯一的账号级冷却（把白名单群挡在门外）。
        if not is_allowed(event):
            return False
    return True


# ---------- 歌名校验（防误触硬闸） ----------
# 命中任一条即视为「这不是歌名」。疑问词/泛称是误敲的高发形态：
# 「云崽 点歌 你喜欢听什么歌」「云崽 点歌 推荐点歌」。
_QUESTION_WORDS = (
    "什么",
    "怎么",
    "为什么",
    "哪些",
    "哪首",
    "哪个",
    "多少",
    "吗",
    "呢",
    "推荐",
    "好听",
    "好不好",
)
_GENERIC_NAMES = {"歌", "音乐", "来一首", "来点", "随便", "放一首", "一首"}
_MAX_NAME_LEN = 50
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def invalid_song_name(name: str) -> str:
    """返回拒绝原因；空串表示通过。"""
    if not name or not name.strip():
        return "没给歌名"
    if len(name) > _MAX_NAME_LEN:
        return f"歌名过长（>{_MAX_NAME_LEN} 字）"
    if _CONTROL_RE.search(name):
        return "歌名含控制字符"
    if "？" in name or "?" in name:
        return "歌名含问号"
    for w in _QUESTION_WORDS:
        if w in name:
            return f"歌名像疑问句（含「{w}」）"
    if name.strip() in _GENERIC_NAMES:
        return "歌名过于笼统"
    return ""


# ---------- silk 缓存 ----------
class SilkCache:
    """歌曲 silk 字节的内存缓存（``song_id`` → bytes），带字节配额与最旧淘汰。

    同一首歌被重复点中时不重新下载+编码——那是约 1s 编码 + 下载的全部成本。
    冷门功能下命中率不高，但成本极低；配额超限淘汰最旧，内存有界。
    """

    __slots__ = ("quota", "_data")

    def __init__(self, quota: int = _CACHE_QUOTA_BYTES):
        self.quota = quota
        self._data: dict[str, bytes] = {}

    def get(self, key: str) -> bytes | None:
        return self._data.get(key)

    def put(self, key: str, value: bytes) -> None:
        if not key or not value:
            return
        self._data.pop(key, None)
        self._data[key] = value
        self._evict()

    def _evict(self) -> None:
        while self.quota and self._total() > self.quota and len(self._data) > 1:
            oldest = next(iter(self._data))
            self._data.pop(oldest, None)

    def _total(self) -> int:
        return sum(len(v) for v in self._data.values())

    def clear(self) -> None:
        self._data.clear()


_cache = SilkCache()


# ---------- 编排 ----------
# M10（REVIEW-6ec3f7c..a36ea1d）：song.id 来自音乐接口响应（外部可控数据），
# 直接拼进路径会让 `"/etc/cron.d/evil"`（pathlib 绝对路径吃掉左侧）或
# `"../../x"` 逃出缓存目录。只放行安全字符集。
_SONG_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _cache_path(song: Song) -> Path | None:
    """歌曲 mp3 的落盘路径；id 不合法返回 None（调用方跳过下载）。"""
    if not _SONG_ID_RE.match(song.id or ""):
        logger.warning("歌曲 id 不合法，拒绝用于文件名：%r", (song.id or "")[:64])
        return None
    root = Path(os.getenv("AGENT_MUSIC_CACHE_DIR") or "data/cache/music")
    path = root / f"{song.id}.mp3"
    # 双保险：解析后必须仍在 root 内（防未来放宽正则时回归）
    try:
        root_res = root.resolve()
        if not str(path.resolve()).startswith(str(root_res) + os.sep):
            logger.warning("歌曲缓存路径逃出缓存目录，拒绝：%s", path)
            return None
    except OSError:
        return None
    return path


def _purge_disk_cache(root: Path, quota_bytes: int) -> None:
    """把磁盘 mp3 缓存压回配额内（按 mtime 最旧优先删）。

    M11（REVIEW-6ec3f7c..a36ea1d）：旧实现只约束**内存** silk，mp3 从不清理，
    磁盘随点歌次数单调增长（每次 ≤ AGENT_MUSIC_MAX_DOWNLOAD_MB）。
    """
    if quota_bytes <= 0 or not root.is_dir():
        return
    try:
        files = [p for p in root.glob("*.mp3") if p.is_file()]
    except OSError:
        return
    total = sum(p.stat().st_size for p in files)
    if total <= quota_bytes:
        return
    for p in sorted(files, key=lambda f: f.stat().st_mtime):
        try:
            size = p.stat().st_size
            p.unlink()
            total -= size
        except OSError:
            continue
        if total <= quota_bytes:
            break


async def _play(event: MessageEvent, song_name: str) -> None:
    """搜索 → 取地址 → 下载 → 编码 → 发送。每一步失败都明确告知，不静默。"""
    # 歌名校验必须在取冷却额度**之前**：误触（「点歌 你喜欢听什么歌」）不该
    # 白烧掉 30s 账号级冷却，那会连带挡住群里其他人的正常点歌
    reason = invalid_song_name(song_name)
    if reason:
        await event.reply(f"没识别出歌名（{reason}），直接把歌名告诉我就行～")
        return

    # M9：能不能发要先判定。语音目前只能发群（sender 只有 send_group_msg），
    # 旧实现把这一步放在**下载+编码之后**，私聊点歌会白下载 ≤20MB、白编码，
    # 再把账号级冷却也一并烧掉。
    group_id = getattr(event, "group_id", None)
    if group_id is None:
        await event.reply("私聊暂时只支持文字，语音放歌仅在群里可用。")
        return

    cooldown = default_cooldown()
    left = cooldown.try_acquire()
    if left > 0:
        await event.reply(f"刚放完一首，{int(left) + 1}s 后再来～")
        return

    songs = await search(song_name)
    if not songs:
        await event.reply(f"没搜到《{song_name}》，换个关键词试试？")
        return

    usable = filter_by_duration(songs, max_seconds())
    if not usable:
        longest = max(s.duration_seconds for s in songs)
        await event.reply(
            f"搜到的都超过 {max_seconds() // 60} 分钟上限（最长的 {longest // 60}:{longest % 60:02d}），不发语音了。"
        )
        return

    song = usable[0]

    # M11：缓存优先。旧实现无条件先 fetch_audio 再查缓存 → 命中缓存也只省编码，
    # 每次仍重新下载 0.5–20MB（docstring 却声称"不重新下载"）。
    silk = _cache.get(song.id)
    if silk is None:
        src = _cache_path(song)
        if src is None:
            await event.reply("这首歌的标识异常，换一首吧。")
            return
        got = await song_url(song.id)
        if got is None:
            await event.reply(
                f"《{song.label}》拿不到可播放的地址（可能无版权或需 VIP）。"
            )
            return
        audio_url, _size = got

        try:
            await fetch_audio(audio_url, src)
        except UnsafeURLError as e:
            logger.warning("音频地址未通过安全校验：%s（%s）", audio_url, e)
            await event.reply("这首歌的音频地址不可用，换一首吧。")
            return
        except Exception as e:
            logger.exception("音频下载失败：%s", audio_url)
            await event.reply(f"下载失败（{type(e).__name__}），换一首吧。")
            return

        try:
            silk = await encode_to_silk(src)
        except Exception as e:
            logger.exception("silk 编码失败：%s", src)
            await event.reply(f"音频转码失败（{type(e).__name__}）。")
            return
        _cache.put(song.id, silk)
        # M11：silk 已在内存缓存，mp3 不必留在盘上；顺便把磁盘缓存压回配额
        try:
            src.unlink(missing_ok=True)
        except OSError:
            logger.warning("清理音频缓存文件失败：%s", src)
        _purge_disk_cache(
            src.parent,
            _env_cache_mb("AGENT_MUSIC_CACHE_MB", DEFAULT_CACHE_MB) * 1024 * 1024,
        )

    status = await send_group_voice(
        int(group_id), silk, label=f"{song.label}（{song.duration_seconds}s）"
    )
    if status == "ok":
        await event.reply(f"♪ {song.label}")
    elif status == "uncertain":
        await event.reply("发送结果不确定（可能已送达），我没有重发以免重复。")
    else:
        await event.reply("发送失败了，稍后再试。")


# ---------- 依赖探测 + 条件注册（F2） ----------
def _missing_deps() -> list[str]:
    """返回缺失的依赖描述；空列表表示可以注册。"""
    missing: list[str] = []
    ok, reason = silk_available()
    if not ok:
        missing.append(reason)
    from agentcore.music import api_configured, http_configured

    if not api_configured():
        missing.append("AGENT_MUSIC_API_URL 未配置")
    if not http_configured():
        missing.append("NAPCAT_HTTP_URL 未配置（OneBot HTTP 发送不可用）")
    return missing


_missing = _missing_deps()
if _missing:
    # 绝不向上抛异常：本模块是被 _load_plugin_modules 条件导入的，
    # 抛异常会连带影响整个插件加载。缺依赖就只是不注册。
    logger.info("点歌功能未启用：%s", "；".join(_missing))
else:
    music_matcher = on_message(rule=_rule, priority=5, block=True)

    @music_matcher.handle()
    async def handle_music(event: MessageEvent) -> None:
        if _is_self_message(event):
            return
        parsed = parse_command(_plain_text(event))
        if parsed is None:
            return
        _cmd, song_name = parsed
        try:
            await _play(event, song_name)
        except Exception:
            logger.exception("点歌处理失败")
            await event.reply("放歌出错啦，稍后再试。")
