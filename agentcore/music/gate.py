"""点歌的准入闸门：群白名单 + 账号级冷却。

冷却是**账号级**而不是 per-group：瓶颈在账号——一个 QQ 号的 highway 上传天然串行
（实测 5 分钟语音上传约 7.8s）。per-group 冷却挡不住两个群同时点歌各发一条、把
彼此的延迟顶到 16s。

冷却 15s > 最坏任务耗时（编码 ~1s + 上传 ~8s），所以冷却期内到达的请求会被直接
拒绝，**结构上不存在并发**，也就不需要队列或 asyncio.Lock——对冷门功能，
「稍后再试」比排队更合适。
"""

from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)

DEFAULT_COOLDOWN = 15


def _env_str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def allowed_groups() -> set[str]:
    """点歌群白名单（``AGENT_MUSIC_ALLOWED_GROUPS``，逗号分隔）。

    **默认空 = 全关，且不继承 ``ALLOWED_GROUPS``**：否则以后往 ALLOWED_GROUPS
    加一个新群，那个群就自动获得点歌能力——属于权限的意外扩大。冷门功能走 opt-in。
    """
    raw = _env_str("AGENT_MUSIC_ALLOWED_GROUPS")
    # QQ 号必须是 ASCII 数字：全角 "１２" 能过 isdigit()（§5 的坑），
    # 放进集合后永远匹配不上真实 group_id，只会让白名单静默失效
    return {
        g.strip()
        for g in raw.split(",")
        if g.strip() and g.strip().isascii() and g.strip().isdigit()
    }


def is_group_allowed(group_id: str | int | None) -> bool:
    """群聊准入。``group_id`` 为空（私聊）时不由本函数把关。"""
    if group_id in (None, ""):
        return True
    groups = allowed_groups()
    return bool(groups) and str(group_id) in groups


def cooldown_seconds() -> int:
    """账号级冷却秒数；``AGENT_MUSIC_COOLDOWN=0`` 关闭冷却。"""
    raw = _env_str("AGENT_MUSIC_COOLDOWN")
    if not raw:
        return DEFAULT_COOLDOWN
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "AGENT_MUSIC_COOLDOWN=%r 不是整数，回退默认 %ds", raw, DEFAULT_COOLDOWN
        )
        return DEFAULT_COOLDOWN
    if value < 0:
        logger.warning(
            "AGENT_MUSIC_COOLDOWN=%d 为负，回退默认 %ds", value, DEFAULT_COOLDOWN
        )
        return DEFAULT_COOLDOWN
    return value


class PlayCooldown:
    """账号级放歌冷却。单例、进程内存，重启即丢（冷门功能可接受）。

    ``_last_play`` 的哨兵必须是 ``None`` 而不是 ``0.0``：``time.monotonic()``
    是**开机时长**，用 0.0 会让 ``now - 0.0 < cooldown`` 在刚启动时恒成立，
    第一次点歌被误判在冷却期内（§5 记的就是这个坑，见 embedding 客户端的同类修复）。
    """

    __slots__ = ("cooldown", "_last_play", "_clock")

    def __init__(self, cooldown: float | None = None, clock=time.monotonic):
        self.cooldown = DEFAULT_COOLDOWN if cooldown is None else float(cooldown)
        self._last_play: float | None = None
        self._clock = clock

    def remaining(self) -> float:
        """还需等多少秒；0 表示可以放。只读，不记账。"""
        if self._last_play is None or self.cooldown <= 0:
            return 0.0
        left = self.cooldown - (self._clock() - self._last_play)
        return left if left > 0 else 0.0

    def try_acquire(self) -> float:
        """取一次放歌额度。返回本次自身等待秒数（恒为 0——冷却是拒绝而非等待）。

        冷却期内**直接拒绝**（返回剩余秒数 > 0 表示被拒），这正是"无队列"的来源。
        """
        left = self.remaining()
        if left > 0:
            return left
        self._last_play = self._clock()
        return 0.0

    def reset(self) -> None:
        self._last_play = None


_default_cooldown: PlayCooldown | None = None


def default_cooldown() -> PlayCooldown:
    """进程级共享实例：所有群共用同一份额度（账号级语义的唯一事实来源）。"""
    global _default_cooldown
    if _default_cooldown is None:
        _default_cooldown = PlayCooldown(cooldown=cooldown_seconds())
    return _default_cooldown
