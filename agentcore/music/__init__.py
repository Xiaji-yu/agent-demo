"""点歌能力：歌源 + silk 编码 + 闸门 + 发送。

分层：``agentcore/music/`` 全程不 import nonebot（可单测、可复用）；
平台适配只有 ``plugins/qq_agent_adapter/music_route.py`` 一处。
"""

from .client import Song, api_configured, filter_by_duration, search, song_url
from .download import UnsafeURLError, audio_hosts, fetch_audio
from .gate import (
    PlayCooldown,
    cooldown_seconds,
    default_cooldown,
    is_group_allowed,
)
from .sender import (
    SEND_FAILED,
    SEND_OK,
    SEND_UNCERTAIN,
    http_configured,
    send_group_voice,
)
from .silk import SILK_HEADER, encode_to_silk, silk_available

__all__ = [
    "Song",
    "SILK_HEADER",
    "SEND_OK",
    "SEND_FAILED",
    "SEND_UNCERTAIN",
    "UnsafeURLError",
    "PlayCooldown",
    "api_configured",
    "audio_hosts",
    "cooldown_seconds",
    "default_cooldown",
    "encode_to_silk",
    "fetch_audio",
    "filter_by_duration",
    "http_configured",
    "is_group_allowed",
    "search",
    "send_group_voice",
    "silk_available",
    "song_url",
]
