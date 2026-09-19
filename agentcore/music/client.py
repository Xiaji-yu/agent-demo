"""NeteaseCloudMusicApi 客户端：搜索 + 取音频地址。

只有**搜索/取地址**走这里，主机由运维在 ``AGENT_MUSIC_API_URL`` 配死（视为显式
授权，不过 SSRF 校验）；API 返回的音频地址另走 ``download.py`` 的受控校验——那条
防的是"接口返回了一个坏地址"。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "http://127.0.0.1:16300"
_TIMEOUT = 10.0
_SEARCH_LIMIT = 5
# standard 档实测 3:59 = 0.46 MB，无需 VIP；更高档可能触发会员校验，不赌
_LEVEL = "standard"


@dataclass(frozen=True)
class Song:
    id: str
    name: str
    artists: str
    album: str
    duration_ms: int

    @property
    def duration_seconds(self) -> int:
        return self.duration_ms // 1000

    @property
    def label(self) -> str:
        return f"{self.name} - {self.artists}" if self.artists else self.name


def api_url() -> str:
    import os

    return (os.getenv("AGENT_MUSIC_API_URL") or DEFAULT_API_URL).strip().rstrip("/")


def api_configured() -> bool:
    """是否显式配置了音乐 API。空串视为未配置（注册闸门第一项）。"""
    import os

    return bool((os.getenv("AGENT_MUSIC_API_URL") or "").strip())


async def _post(path: str, payload: dict) -> dict | None:
    """POST 并取回 JSON；任何异常或非预期结构都返回 None（调用方降级，不抛）。"""
    url = f"{api_url()}{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        logger.warning("音乐 API 请求失败：%s%s err=%s", api_url(), path, e)
        return None
    return data if isinstance(data, dict) else None


def parse_search(data: dict | None, limit: int = _SEARCH_LIMIT) -> list[Song]:
    """解析 /search 响应。结构不符时返回空列表而不是抛异常。

    上游是非自建也可能被换掉的第三方接口，形状变化属于常态；让一次坏响应只导致
    「没搜到」，而不是让整个点歌请求崩掉。
    """
    if not isinstance(data, dict):
        return []
    try:
        raw = data.get("result", {}).get("songs") or []
    except AttributeError:
        return []
    songs: list[Song] = []
    for item in raw[:limit]:
        if not isinstance(item, dict):
            continue
        try:
            artists = "、".join(
                str(a.get("name", ""))
                for a in (item.get("artists") or [])
                if isinstance(a, dict)
            )
            album = (item.get("album") or {}).get("name", "")
            songs.append(
                Song(
                    id=str(item.get("id", "")),
                    name=str(item.get("name", "")),
                    artists=artists,
                    album=str(album or ""),
                    duration_ms=int(item.get("duration") or 0),
                )
            )
        except (TypeError, ValueError):
            logger.warning("跳过结构异常的歌曲条目：%r", str(item)[:80])
            continue
    return [s for s in songs if s.id]


async def search(keyword: str, limit: int = _SEARCH_LIMIT) -> list[Song]:
    """按关键词搜歌。"""
    data = await _post(
        "/search", {"keywords": keyword, "limit": limit, "type": 1, "offset": 0}
    )
    return parse_search(data, limit)


def filter_by_duration(songs: list[Song], max_seconds: int) -> list[Song]:
    """剔除超过 ``max_seconds`` 的歌。**在下载之前**调用，避免为发不出去的音频白下一遍。

    用 ``>`` 不是 ``>=``：上限是"最长可发时长"，正好等于上限的歌必须保留。
    实测 4:28 可发，上限取 300s，那么 300s 整是被允许的边界。

    L11（REVIEW-6ec3f7c..a36ea1d）：比较在**毫秒**上做（``duration_ms``），
    不再先 ``// 1000`` 取整 —— 否则 300.9s 会被当成 300s 放行，与 AC A2 的
    ``duration_ms > max*1000`` 语义不一致，且无用例能区分。
    """
    if max_seconds <= 0:
        return list(songs)
    limit_ms = max_seconds * 1000
    return [s for s in songs if s.duration_ms <= limit_ms]


async def song_url(song_id: str) -> tuple[str, int] | None:
    """取歌曲的可下载地址与字节数。拿不到返回 None（不返回空串）。"""
    data = await _post("/song/url/v1", {"id": song_id, "level": _LEVEL})
    if not isinstance(data, dict):
        return None
    entries = data.get("data")
    if not isinstance(entries, list) or not entries:
        return None
    first = entries[0]
    if not isinstance(first, dict):
        return None
    url = str(first.get("url") or "").strip()
    if not url:
        logger.info("歌曲无可下载地址（可能无版权或需 VIP）：id=%s", song_id)
        return None
    size = first.get("size")
    return url, int(size) if isinstance(size, int) else 0
