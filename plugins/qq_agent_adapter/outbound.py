"""出站投递：节流 + 长回复分层（单条 / 合并转发 / 合并转发 + 文件）。

**为什么要分层**：QQ 对发言频率敏感。把一条长回复硬切成 N 条连发，既打断阅读，
也把自己暴露在风控面里。合并转发（OneBot ``send_group_forward_msg`` /
``send_private_forward_msg``）把 N 段合成**一条**消息（群视角是一个「聊天记录」
卡片），发言次数从 N 降到 1，且文本仍可选中/复制/搜索——比"渲染成图片"划算得多。

阈值分层（环境变量可覆盖）
--------------------------
- ``len <= AGENT_REPLY_SINGLE_MAX``（默认 1500）→ 单条文本，与旧行为一致
- ``<= AGENT_REPLY_FORWARD_MAX``（默认 4500）→ 合并转发，N 个节点
- ``>  AGENT_REPLY_FORWARD_MAX`` → 合并转发，私聊再补一份 md 文件

**硬降级**：合并转发任一环节失败（实现不支持该 action、被风控拒绝、超时）一律回落
到逐条发送——**收不到回复比风控严重得多**。

节流
----
所有出站都过 :class:`OutboundThrottle`：per-target 最小间隔 + 每窗口条数上限，
外加一个**全局（账号级）最小间隔**。QQ 风控是按账号计的，只做 per-target 不足以
压住「多个群同时被推送」这类瞬时并发。

时钟与 sleep 可注入，测试不必真的等待。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import deque

logger = logging.getLogger(__name__)

try:  # 与 file_sender 一致：NoneBot 未初始化时仍可导入（测试/纯解析场景）
    from nonebot.adapters.onebot.v11 import MessageSegment
except Exception:  # pragma: no cover - 依赖缺失时的降级
    MessageSegment = None

DEFAULT_SINGLE_MAX = 1500
DEFAULT_FORWARD_MAX = 4500
DEFAULT_MAX_NODES = 10
DEFAULT_MIN_INTERVAL = 1.0
DEFAULT_GLOBAL_MIN_INTERVAL = 0.4
DEFAULT_PER_WINDOW = 20
DEFAULT_WINDOW = 60.0
DEFAULT_MAX_WAIT = 10.0

MODE_SINGLE = "single"
MODE_FORWARD = "forward"
MODE_CHUNKED = "chunked"
SUFFIX_FILE = "+file"


# ---------------------------------------------------------------------------
# 配置读取（每次调用时读，测试可 monkeypatch 环境变量）
# ---------------------------------------------------------------------------
def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r 不是整数，回退默认值 %s", name, raw, default)
        return default
    if value < minimum:
        logger.warning("%s=%s 小于下限 %s，回退默认值 %s", name, value, minimum, default)
        return default
    return value


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r 不是数字，回退默认值 %s", name, raw, default)
        return default
    if value < minimum:
        logger.warning("%s=%s 小于下限 %s，回退默认值 %s", name, value, minimum, default)
        return default
    return value


def _env_bool(name: str, default: bool = True) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def single_max() -> int:
    return _env_int("AGENT_REPLY_SINGLE_MAX", DEFAULT_SINGLE_MAX, minimum=100)


def forward_max() -> int:
    """合并转发阈值；保证不小于单条阈值，否则分层语义自相矛盾。"""
    value = _env_int("AGENT_REPLY_FORWARD_MAX", DEFAULT_FORWARD_MAX, minimum=100)
    return max(value, single_max())


def max_nodes() -> int:
    return _env_int("AGENT_REPLY_FORWARD_MAX_NODES", DEFAULT_MAX_NODES, minimum=1)


def forward_enabled() -> bool:
    return _env_bool("AGENT_REPLY_FORWARD", True)


def bot_nickname() -> str:
    """合并转发节点的显示名（必须用 bot 自己的身份，伪造他人是明确的风控点）。"""
    return (os.getenv("AGENT_BOT_NICKNAME") or "").strip() or "助手"


# ---------------------------------------------------------------------------
# 消息切分（原 matcher._split_qq_message，行为不变）
# ---------------------------------------------------------------------------
def split_message(text: str, max_len: int = DEFAULT_SINGLE_MAX) -> list[str]:
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


# ---------------------------------------------------------------------------
# 节流
# ---------------------------------------------------------------------------
class _Bucket:
    """单个维度的发送记录：最近一次时间 + 窗口内的发送时间。"""

    __slots__ = ("last", "history")

    def __init__(self) -> None:
        self.last: float | None = None
        self.history: deque[float] = deque()

    def wait_needed(
        self, now: float, *, min_interval: float, per_window: int, window: float
    ) -> float:
        while self.history and now - self.history[0] >= window:
            self.history.popleft()
        wait = 0.0
        if self.last is not None and now - self.last < min_interval:
            wait = min_interval - (now - self.last)
        if per_window > 0 and len(self.history) >= per_window:
            wait = max(wait, window - (now - self.history[0]))
        return wait

    def record(self, now: float) -> None:
        self.history.append(now)
        self.last = now


class OutboundThrottle:
    """出站节流：per-target 最小间隔 + 每窗口条数上限 + 全局（账号级）最小间隔。

    ``max_wait`` 是**软上限**：算出来的等待超过它就只等这么久，然后照发并告警。
    宁可冒一点风控风险，也不要让提醒/回复无限期卡住（死等会造成更严重的故障）。
    """

    def __init__(
        self,
        *,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        per_window: int = DEFAULT_PER_WINDOW,
        window: float = DEFAULT_WINDOW,
        global_min_interval: float = DEFAULT_GLOBAL_MIN_INTERVAL,
        max_wait: float = DEFAULT_MAX_WAIT,
        clock=time.monotonic,
        sleep=asyncio.sleep,
    ) -> None:
        self.min_interval = min_interval
        self.per_window = per_window
        self.window = window
        self.global_min_interval = global_min_interval
        self.max_wait = max_wait
        self._clock = clock
        self._sleep = sleep
        self._locks: dict[str, asyncio.Lock] = {}
        self._targets: dict[str, _Bucket] = {}
        self._global = _Bucket()

    def _bucket(self, target: str) -> _Bucket:
        bucket = self._targets.get(target)
        if bucket is None:
            bucket = self._targets[target] = _Bucket()
        return bucket

    def wait_for(self, target: str) -> float:
        """不 sleep、不记录，只算出当前还需要等多久（供测试与观测）。"""
        now = self._clock()
        return max(
            self._global.wait_needed(
                now,
                min_interval=self.global_min_interval,
                per_window=0,
                window=self.window,
            ),
            self._bucket(target).wait_needed(
                now,
                min_interval=self.min_interval,
                per_window=self.per_window,
                window=self.window,
            ),
        )

    async def acquire(self, target: str) -> float:
        """取到一次发送额度；返回实际等待秒数。同 target 串行，跨 target 仅受全局约束。"""
        lock = self._locks.get(target)
        if lock is None:
            lock = self._locks[target] = asyncio.Lock()
        async with lock:
            waited = 0.0
            while True:
                now = self._clock()
                wait = self.wait_for(target)
                if wait <= 0:
                    self._global.record(now)
                    self._bucket(target).record(now)
                    return waited
                if waited + wait > self.max_wait:
                    logger.warning(
                        "出站节流：%s 还需等 %.1fs，超过软上限 %.1fs，放行并告警",
                        target,
                        wait,
                        self.max_wait,
                    )
                    self._global.record(now)
                    self._bucket(target).record(now)
                    return waited
                await self._sleep(wait)
                waited += wait

    def reset(self) -> None:
        self._locks.clear()
        self._targets.clear()
        self._global = _Bucket()


_default_throttle: OutboundThrottle | None = None


def default_throttle() -> OutboundThrottle:
    """进程级共享实例：回复与主动推送共用同一份额度，避免两边各自计数。"""
    global _default_throttle
    if _default_throttle is None:
        _default_throttle = OutboundThrottle(
            min_interval=_env_float("AGENT_OUTBOUND_MIN_INTERVAL", DEFAULT_MIN_INTERVAL),
            per_window=_env_int("AGENT_OUTBOUND_PER_MIN", DEFAULT_PER_WINDOW),
            global_min_interval=_env_float(
                "AGENT_OUTBOUND_GLOBAL_MIN_INTERVAL", DEFAULT_GLOBAL_MIN_INTERVAL
            ),
            max_wait=_env_float("AGENT_OUTBOUND_MAX_WAIT", DEFAULT_MAX_WAIT, minimum=0.0),
        )
    return _default_throttle


# ---------------------------------------------------------------------------
# 发送
# ---------------------------------------------------------------------------
def _resolve_throttle(throttle: OutboundThrottle | None) -> OutboundThrottle:
    return throttle if throttle is not None else default_throttle()


async def _send_text(
    bot,
    kind: str,
    ident: int,
    text: str,
    throttle: OutboundThrottle | None = None,
) -> None:
    await _resolve_throttle(throttle).acquire(f"{kind}:{ident}")
    if kind == "group":
        await bot.send_group_msg(group_id=ident, message=text)
    else:
        await bot.send_private_msg(user_id=ident, message=text)


async def _try_forward(
    bot,
    kind: str,
    ident: int,
    chunks: list[str],
    self_id: str,
    nickname: str,
    throttle: OutboundThrottle | None = None,
) -> bool:
    """尝试合并转发；成功返回 True，任何失败返回 False（由调用方降级）。"""
    if MessageSegment is None:  # pragma: no cover - 依赖缺失
        logger.warning("合并转发不可用：MessageSegment 未导入，降级逐条发送")
        return False
    uid_text = str(self_id or "").strip()
    if not uid_text.isdigit():
        # node 必须标 bot 自己的 QQ；拿不到就退回逐条，避免伪造他人身份（风控点）
        logger.warning("合并转发缺少合法的 self_id（%r），降级逐条发送", self_id)
        return False

    nodes = [
        MessageSegment.node_custom(user_id=int(uid_text), nickname=nickname, content=c)
        for c in chunks
    ]
    if kind == "group":
        attempts: list[tuple[str, dict]] = [
            ("send_group_forward_msg", {"group_id": ident, "messages": nodes}),
            (
                "send_forward_msg",
                {"message_type": "group", "group_id": ident, "messages": nodes},
            ),
        ]
    else:
        attempts = [
            ("send_private_forward_msg", {"user_id": ident, "messages": nodes}),
            (
                "send_forward_msg",
                {"message_type": "private", "user_id": ident, "messages": nodes},
            ),
        ]

    throttle_obj = _resolve_throttle(throttle)
    for api, params in attempts:
        try:
            await throttle_obj.acquire(f"{kind}:{ident}")
            await bot.call_api(api, **params)
            logger.info("合并转发成功：%s api=%s nodes=%d", f"{kind}:{ident}", api, len(nodes))
            return True
        except Exception as e:
            logger.warning(
                "合并转发失败：%s api=%s err=%s，尝试下一方案", f"{kind}:{ident}", api, e
            )
    return False


async def _send_file(user_id: int, text: str) -> bool:
    """私聊附发 md 文件；失败只记日志，不影响已完成的文本投递。"""
    try:
        from agentcore.skills.file_sender import FILE_SEND_OK_PREFIX, send_markdown_file

        result = await send_markdown_file(str(user_id), text)
        if str(result).startswith(FILE_SEND_OK_PREFIX):
            return True
        logger.warning("长回复附发文件未成功：%s", result)
    except Exception:
        logger.exception("长回复附发文件异常")
    return False


async def deliver_reply(
    bot,
    *,
    kind: str,
    ident: int,
    text: str,
    self_id: str = "",
    nickname: str = "",
    throttle: OutboundThrottle | None = None,
) -> str:
    """按阈值分层投递一条回复，返回实际使用的模式。

    模式：``single`` / ``forward`` / ``forward+file`` / ``chunked`` / ``chunked+file``
    （``chunked`` 即合并转发失败的硬降级路径）。
    """
    text = text or ""
    chunks = split_message(text, single_max())
    if len(chunks) <= 1:
        await _send_text(bot, kind, ident, text or "（回复内容为空）", throttle)
        return MODE_SINGLE

    mode = ""
    if forward_enabled() and len(chunks) <= max_nodes():
        if await _try_forward(
            bot, kind, ident, chunks, self_id, nickname or bot_nickname(), throttle
        ):
            mode = MODE_FORWARD
    if not mode:
        logger.info("长回复降级为逐条发送：%s chunks=%d", f"{kind}:{ident}", len(chunks))
        for chunk in chunks:
            await _send_text(bot, kind, ident, chunk, throttle)
        mode = MODE_CHUNKED

    if kind == "private" and len(text) > forward_max():
        if await _send_file(ident, text):
            mode += SUFFIX_FILE
    return mode
