"""群聊上下文缓冲：记录机器人所在群的最近消息，供被唤醒时理解语境。

背景：NoneBot 只在「唤醒词 / @机器人」时把消息交给 agent，群里其他人的
消息此前完全不可见——于是「帮我看看他们在聊什么」「这句话什么意思」这类
请求没有任何上下文可依据，模型只能拿长期记忆瞎猜（实测表现为"又提起很久
以前那张图"）。

本模块只做一件事：把群里最近的消息留在内存里（有界 + TTL，绝不落库），
被唤醒时作为**不可信围栏内容**注入，供模型理解语境，不当作指令执行。

配置（.env）：
- ``AGENT_GROUP_CONTEXT=0``        关闭（默认开启）
- ``AGENT_GROUP_CONTEXT_LINES=10`` 保留/注入的最近消息条数
- ``AGENT_GROUP_CONTEXT_TTL=900``  内存中保留秒数
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

MAX_LINES_DEFAULT = 10
TTL_DEFAULT = 900.0
MAX_CHARS_DEFAULT = 1200
PER_LINE_CHARS = 160  # 单条消息入库前截断，避免超长消息占满缓冲


def context_enabled() -> bool:
    """群聊上下文记录/注入开关，默认开启。"""
    raw = (os.getenv("AGENT_GROUP_CONTEXT") or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def context_lines() -> int:
    try:
        return max(1, int(os.getenv("AGENT_GROUP_CONTEXT_LINES", "10")))
    except ValueError:
        return MAX_LINES_DEFAULT


def _ttl() -> float:
    try:
        return max(1.0, float(os.getenv("AGENT_GROUP_CONTEXT_TTL", "900")))
    except ValueError:
        return TTL_DEFAULT


class GroupContextBuffer:
    """有界 + TTL 的群消息环形缓冲（内存态，单事件循环下读写无 await，天然原子）。"""

    def __init__(
        self,
        max_lines: int = MAX_LINES_DEFAULT,
        ttl: float = TTL_DEFAULT,
        max_chars: int = MAX_CHARS_DEFAULT,
    ):
        self.max_lines = max(1, int(max_lines))
        self.ttl = float(ttl)
        self.max_chars = max(64, int(max_chars))
        self._data: dict[str, list[dict]] = {}

    def record(
        self,
        group_id: str,
        who: str,
        text: str,
        message_id: Optional[str] = None,
        has_image: bool = False,
        now: Optional[float] = None,
    ) -> None:
        """记录一条群消息；纯媒体/空消息用占位符表示。"""
        line = (text or "").strip()
        if len(line) > PER_LINE_CHARS:
            line = line[:PER_LINE_CHARS] + "…"
        if not line:
            if not has_image:
                return  # 表情/戳一戳等无正文消息不入上下文，避免噪音
            line = "[图片]"
        ts = time.monotonic() if now is None else now
        rows = self._data.setdefault(str(group_id), [])
        rows.append(
            {
                "who": (who or "群成员").strip()[:32] or "群成员",
                "text": line,
                "mid": str(message_id) if message_id is not None else "",
                "ts": ts,
            }
        )
        if len(rows) > self.max_lines:
            del rows[: -self.max_lines]

    def snapshot(
        self,
        group_id: str,
        exclude_message_id: Optional[str] = None,
        limit: Optional[int] = None,
        now: Optional[float] = None,
    ) -> list[dict]:
        """取最近消息（按时间顺序），排除当前这条，并按总字数上限截断旧消息。"""
        rows = self._data.get(str(group_id))
        if not rows:
            return []
        cur = time.monotonic() if now is None else now
        alive = [r for r in rows if cur - r["ts"] <= self.ttl]
        if not alive:
            self._data.pop(str(group_id), None)
            return []
        exclude = str(exclude_message_id) if exclude_message_id is not None else ""
        picked = [r for r in alive if not (exclude and r["mid"] == exclude)]
        if limit is not None:
            picked = picked[-max(1, int(limit)) :]
        # 从最新往回累计字数，超预算就丢更旧的消息
        out: list[dict] = []
        total = 0
        for row in reversed(picked):
            cost = len(row["who"]) + len(row["text"]) + 2
            if out and total + cost > self.max_chars:
                break
            out.append(row)
            total += cost
        out.reverse()
        return [{"who": r["who"], "text": r["text"]} for r in out]

    def clear(self, group_id: str) -> None:
        self._data.pop(str(group_id), None)

    def __len__(self) -> int:
        return len(self._data)


group_context = GroupContextBuffer(
    max_lines=context_lines(),
    ttl=_ttl(),
)
