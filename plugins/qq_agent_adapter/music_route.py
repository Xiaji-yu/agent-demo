"""点歌能力：LLM skill（``play_music``）+ 确定性序号选歌兜底。

**为什么改成 skill**（修订 AC-music-playback.md「直路由、LLM 完全不参与」的方向）：
用户希望由模型判断是否放歌——「放首歌听听」「想听稻香」与「点歌 稻香」应等价。
因此触发从「唤醒词 + 点歌/放歌 子命令」的确定性规则，改为模型在 tool-loop 里
调用 ``play_music``；``AGENT_MUSIC_COMMANDS`` 从**硬触发词**降级为 skill 描述
里的别名提示（仍可配置，运营可以改说法）。

安全性**不依赖模型自觉**，handler 内保留三道硬闸：
  1. 群白名单（``is_group_allowed``，未开放群直接拒绝）；
  2. 歌名校验（``invalid_song_name``）——模型把疑问句/泛称当歌名传进来时拒绝，
     且**不烧冷却**（否则误触一次就连带挡住群里其他人的正常点歌）；
  3. 账号级冷却（``default_cooldown``）。
模型只决定"要不要调"，不决定"能不能发"。

序号选歌仍是**确定性 matcher**（``selection_matcher``）：候选列表由 skill 列出，
用户回一个裸序号「2」时不必再绕一次 LLM——``priority=4`` 先于 chat_matcher
且 ``block=True``，规则要求"确有待选项且是在范围内的序号"，不会吞普通数字。
发送侧只有这一个入口（``handle_selection`` → ``_play_song``），skill 入口
（``_play`` → ``_play_song``）与之汇合，两条路径的冷却/缓存/安全校验语义一致。

本模块只在闸门通过时才被 ``__init__._load_plugin_modules`` 导入——未配置时
根本不加载，skill 不注册，消息照常走普通聊天，对核心零影响。
"""

from __future__ import annotations

import logging
import os
import re
import time
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

from .acl import is_allowed, is_superuser_id
from .matcher import _is_self_message, _plain_text

logger = logging.getLogger(__name__)

DEFAULT_COMMANDS = "点歌,放歌"
DEFAULT_MAX_SECONDS = 300
DEFAULT_CACHE_MB = 200
_CACHE_QUOTA_BYTES = DEFAULT_CACHE_MB * 1024 * 1024

# skill 名：__init__ 合并点歌白名单进 PermissionChecker.group_skills 时要用
SKILL_NAME = "play_music"


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
    """点歌别名（``AGENT_MUSIC_COMMANDS``）。

    skill 化之后它不再是**硬触发词**，只作为 skill 描述里的意图提示
    （告诉模型"用户说这些词时该调 play_music"），运营仍可增删。
    """
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


# ---------- 序号选歌规则 ----------
def _selection_rule(event: MessageEvent) -> bool:
    """回复序号选歌（确定性兜底，不依赖模型）。

    **不要求唤醒词**：bot 刚问过"回复序号选择"，此时再要用户打「云崽 2」是多余
    的。取而代之的门是**更强的上下文条件**——该用户在当前会话里**确实有待选项**
    且这条消息就是一个在范围内的序号。任一不满足即返回 False，消息落回普通聊天
    （群里一个裸「2」不会凭空被吞掉）。

    权限门与 skill handler 保持一致（群白名单 / 私聊 superuser / 自身消息过滤）；
    待选列表本身按 (会话, 用户) 隔离，别人回复序号不会命中你的列表。
    """
    if _is_self_message(event):
        return False
    if isinstance(event, GroupMessageEvent):
        if not is_group_allowed(event.group_id):
            return False
    elif not is_allowed(event):
        return False
    songs = _selections.peek(_event_selection_key(event))
    if not songs:
        return False
    return parse_selection(_plain_text(event), len(songs)) is not None


# ---------- 歌名校验（防误触硬闸） ----------
# 命中任一条即视为「这不是歌名」。疑问词/泛称是误敲的高发形态：
# 「点歌 你喜欢听什么歌」「点歌 推荐点歌」。skill 化后模型可能把这类文本
# 直接当歌名传进来，这道闸是**不依赖模型自觉**的第二道防线。
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


def _sanitize_url(url: str) -> str:
    """日志用：去掉 query/fragment，只保留 scheme + host + path 前缀，避免 token 泄漏。"""
    try:
        from urllib.parse import urlsplit

        sp = urlsplit(url)
        path = sp.path[:64] if sp.path else ""
        return f"{sp.scheme}://{sp.hostname}{path}"
    except Exception:
        return str(url)[:64]


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


# ---------- 候选选择（多版本让用户挑） ----------
# 实测动机：搜「稻香」前 5 条全是翻唱（Lucky小爱 / Lie / 卡罗尔…），而网易云
# 没有周杰伦版权——原唱根本搜不到。旧实现盲取 songs[0]，用户既无从选择，也要
# 等语音放完才从「♪ 稻香 - Lucky小爱」看出不是原唱。这里在存在多个候选时先列
# 出（带歌手与专辑，翻唱一眼可辨），由用户回复序号决定放哪一个。
_MAX_CANDIDATES = 5
_SELECTION_TTL = 60.0
_SELECTION_RE = re.compile(r"^\s*([1-9])\s*$")


class PendingSelections:
    """per-(群/私聊, 用户) 的待选列表，短 TTL + 定长，取用即消费。

    内存态、无持久化：过期或重启就丢，用户重新点一次即可；不引入任何队列。
    """

    __slots__ = ("ttl", "_clock", "_items")

    def __init__(self, ttl: float = _SELECTION_TTL, clock=time.monotonic):
        self.ttl = ttl
        self._clock = clock
        self._items: dict[tuple, tuple[float, list]] = {}

    def put(self, key: tuple, songs: list) -> None:
        self._prune()
        self._items[key] = (self._clock() + self.ttl, list(songs))

    def peek(self, key: tuple) -> list | None:
        self._prune()
        item = self._items.get(key)
        return item[1] if item else None

    def take(self, key: tuple) -> list | None:
        self._prune()
        item = self._items.pop(key, None)
        return item[1] if item else None

    def _prune(self) -> None:
        now = self._clock()
        for k in [k for k, (exp, _) in self._items.items() if now > exp]:
            self._items.pop(k, None)


_selections = PendingSelections()


def _selection_key(user_id: str, group_id: str | None) -> tuple:
    """候选归属：群聊按 (群, 用户)，私聊按 (私聊, 用户)。

    skill 入口拿的是 ``user_id``/``group_id`` 字符串（registry 注入），序号
    matcher 入口拿的是 event——两边必须产出**同一个** key，否则 skill 列出的
    候选、matcher 取不到。event 侧统一走 ``_event_selection_key`` 转调本函数。
    """
    return ("g" if group_id else "p", str(group_id or ""), str(user_id or ""))


def _event_group_id(event: MessageEvent) -> str | None:
    return str(event.group_id) if isinstance(event, GroupMessageEvent) else None


def _event_selection_key(event: MessageEvent) -> tuple:
    return _selection_key(str(event.get_user_id()), _event_group_id(event))


def parse_selection(text: str, count: int) -> int | None:
    """把回复解析成 1..count 的序号；不是合法序号返回 None。

    只认 ASCII 数字：全角「１」等一律不认（AGENTS.md §5 的坑），避免
    "看着像选了、其实没选"。
    """
    m = _SELECTION_RE.match(text or "")
    if not m:
        return None
    n = int(m.group(1))
    return n if 1 <= n <= count else None


def _format_candidates(songs: list) -> str:
    """候选列表：序号 + 歌名 - 歌手 + 时长 + 专辑（专辑常暴露"深情版/Cover"）。"""
    lines = [
        f"找到 {len(songs)} 个版本，回复序号选择（{int(_SELECTION_TTL)} 秒内有效）："
    ]
    for i, s in enumerate(songs, 1):
        mm, ss = divmod(s.duration_seconds, 60)
        album = f"｜{s.album}" if s.album else ""
        lines.append(f"{i}. {s.label}（{mm}:{ss:02d}）{album}")
    return "\n".join(lines)


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


async def _play(song_name: str, *, user_id: str, group_id: str | None) -> str:
    """搜索 → 列候选/取地址 → 下载 → 编码 → 发送。**返回**要转述给用户的文本。

    本函数是 ``play_music`` skill 的唯一实现：模型只负责决定调用与转述，
    发送语义（歌名闸门 / ACL / 冷却 / 缓存 / 安全校验）全在这里收口。
    每一步失败都明确告知，不静默。

    为什么不接 event：skill handler 从 registry 拿到的只有 ``user_id`` /
    ``group_id`` 字符串；且 OneBot v11 事件的 reply 是**引用消息数据字段**
    （普通消息为 None）不是方法——线上实测每一次尝试调用它都 ``TypeError``
    （2026-09-19 事故：点歌连错误提示都发不出，用户侧完全静默）。返回文本让
    "怎么发"留给调用方（matcher.send / 模型转述），从结构上消灭这一类坑。
    """
    # 歌名校验必须在取冷却额度**之前**：误触（模型把「你喜欢听什么歌」当歌名）
    # 不该白烧掉 15s 账号级冷却，那会连带挡住群里其他人的正常点歌
    reason = invalid_song_name(song_name)
    if reason:
        return f"没识别出歌名（{reason}），直接把歌名告诉我就行～"

    # M9：能不能发要先判定。语音目前只能发群（sender 只有 send_group_msg），
    # 旧实现把这一步放在**下载+编码之后**，私聊点歌会白下载 ≤20MB、白编码，
    # 再把账号级冷却也一并烧掉。
    if not group_id:
        return "私聊暂时只支持文字，语音放歌仅在群里可用。"

    # 冷却**不在这里取**：本函数可能只走到"列候选"（用户还没决定放哪首），
    # 展示列表不该消耗额度。真正的取用在 _play_song（唯一会发送的地方）。
    songs = await search(song_name)
    if not songs:
        return f"没搜到《{song_name}》，换个关键词试试？"

    usable = filter_by_duration(songs, max_seconds())
    if not usable:
        longest = max(s.duration_seconds for s in songs)
        return (
            f"搜到的都超过 {max_seconds() // 60} 分钟上限"
            f"（最长的 {longest // 60}:{longest % 60:02d}），不发语音了。"
        )

    # 多个候选时不擅自替用户选版本（实测：搜「稻香」前 5 条全是翻唱，
    # 而用户想要的是周杰伦原唱——网易云没有其版权，只能让用户看清后自选）
    usable = usable[:_MAX_CANDIDATES]
    if len(usable) > 1:
        _selections.put(_selection_key(user_id, group_id), usable)
        return _format_candidates(usable)

    return await _play_song(usable[0], group_id)


async def _play_song(song: Song, group_id: str | None) -> str:
    """播放**指定的**一首：冷却 → 取址 → 下载 → 编码 → 发送。返回转述文本。

    skill 入口（``_play`` 选中唯一候选）与序号选歌（``handle_selection``）
    都汇到这里，保证两条入口的冷却、缓存、安全校验、发送语义完全一致。
    """
    # 防御：序号 matcher 的规则允许私聊 superuser 命中，而语音只能发群
    if not group_id:
        return "私聊暂时只支持文字，语音放歌仅在群里可用。"

    cooldown = default_cooldown()
    left = cooldown.try_acquire()
    if left > 0:
        return f"点歌冷却中，{int(left) + 1}s 后再来～"

    # M11：缓存优先。旧实现无条件先 fetch_audio 再查缓存 → 命中缓存也只省编码，
    # 每次仍重新下载 0.5–20MB（docstring 却声称"不重新下载"）。
    silk = _cache.get(song.id)
    if silk is None:
        src = _cache_path(song)
        if src is None:
            cooldown.reset()
            return "这首歌的标识异常，换一首吧。"
        got = await song_url(song.id)
        if got is None:
            cooldown.reset()
            return f"《{song.label}》拿不到可播放的地址（可能无版权或需 VIP）。"
        audio_url, _size = got

        try:
            await fetch_audio(audio_url, src)
        except UnsafeURLError as e:
            logger.warning(
                "音频地址未通过安全校验：%s（%s）", _sanitize_url(audio_url), e
            )
            cooldown.reset()
            return "这首歌的音频地址不可用，换一首吧。"
        except Exception as e:
            logger.exception("音频下载失败：%s", _sanitize_url(audio_url))
            cooldown.reset()
            return f"下载失败（{type(e).__name__}），换一首吧。"

        try:
            silk = await encode_to_silk(src)
        except Exception as e:
            logger.exception("silk 编码失败：%s", src)
            cooldown.reset()
            return f"音频转码失败（{type(e).__name__}）。"
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
        return f"♪ {song.label}"
    if status == "uncertain":
        return "发送结果不确定（可能已送达），我没有重发以免重复。"
    return "发送失败了，稍后再试。"


# ---------- skill 注册 ----------
def _skill_description() -> str:
    aliases = "、".join(music_commands())
    return (
        f"在 QQ 群里搜索并播放一首歌的语音。当用户想听歌、说「{aliases}」、"
        "给出具体歌名或歌手（如「点歌 稻香」「放首歌听听」「想听周杰伦的晴天」）"
        "时调用本 skill。"
        "song_name 只填真实歌名或「歌名 歌手」；不能是序号、数字，"
        "也不能是「随便 / 推荐 / 来一首」这类泛称或任何疑问句"
        "（那种情况直接正常聊天回复，不要调用）。"
        "搜到多个版本时本 skill 会返回候选列表——把列表原样转述给用户、"
        "让他回复序号选择，不要自己替用户挑。"
        "搜索、下载、转码、发送都由本 skill 完成；你只转述返回的文本，"
        "除非返回文本明确说已播放，否则不要声称已经播放。"
    )


def register_music_skill(registry) -> None:
    """把点歌注册成 LLM skill（``play_music``）。依赖缺失时**不注册**。

    权限串用**非 public**：默认部署（``config.yaml`` 的 ``superusers: ["*"]``）
    下人人可见，真正拦人是 handler 里的群白名单；hardened 部署（真实 superuser
    名单）下未授权用户连 schema 都看不到——白名单群由 ``__init__`` 合并进
    ``PermissionChecker.group_skills`` 显式授权。两层都要：权限层管"看不看得见"，
    handler 管"发不发得出去"。
    """
    missing = _missing_deps()
    if missing:
        logger.info("点歌 skill 未注册：%s", "；".join(missing))
        return

    @registry.register(
        SKILL_NAME,
        _skill_description(),
        {
            "type": "object",
            "properties": {
                "song_name": {
                    "type": "string",
                    "description": "真实歌名或「歌名 歌手」；"
                    "不能是序号/数字/泛称/疑问句",
                }
            },
            "required": ["song_name"],
        },
        permission="restricted",
    )
    async def play_music_skill(
        song_name: str = "",
        user_id: str = "",
        group_id: str = "",
    ) -> str:
        # ACL 硬闸（不依赖模型自觉）：群白名单 / 私聊 superuser。
        # 群白名单与主聊天的 ALLOWED_GROUPS 是两套：点歌能力 opt-in，不继承。
        if group_id:
            if not is_group_allowed(group_id):
                logger.info("play_music 拒绝：群 %s 不在点歌白名单", group_id)
                return "这个群没有开放语音点歌。"
        elif not is_superuser_id(user_id):
            # 私聊主路径本就只放行 superuser（matcher.handle_chat），这里是纵深：
            # 换任何入口调 skill 都过同一道闸
            logger.info("play_music 拒绝：私聊非 superuser %s", user_id)
            return "私聊没有开放语音点歌。"
        return await _play(
            song_name, user_id=str(user_id or ""), group_id=group_id or None
        )


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
    # 序号选歌：priority 4 让它先于 chat_matcher 判定；规则已保证
    # "只有真有待选项且确实是序号"才命中，故不会吞掉普通数字消息
    selection_matcher = on_message(rule=_selection_rule, priority=4, block=True)

    @selection_matcher.handle()
    async def handle_selection(event: MessageEvent) -> None:
        if _is_self_message(event):
            return
        key = _event_selection_key(event)
        songs = _selections.peek(key)
        if not songs:
            return
        idx = parse_selection(_plain_text(event), len(songs))
        if idx is None:
            return
        # M2：冷却期内先不消费待选项——_play_song 第一步就会 try_acquire 被拒，
        # 若此处已 take 则候选凭空丢失，用户必须重新点歌。
        cooldown = default_cooldown()
        if cooldown.remaining() > 0:
            await selection_matcher.send(
                f"点歌冷却中，{int(cooldown.remaining()) + 1}s 后再来～"
            )
            return
        _selections.take(key)  # 取用即消费，防重复播放
        song = songs[idx - 1]
        try:
            text = await _play_song(song, _event_group_id(event))
        except Exception:
            logger.exception("候选选择播放失败")
            await selection_matcher.send("放歌出错啦，稍后再试。")
            return
        await selection_matcher.send(text)
