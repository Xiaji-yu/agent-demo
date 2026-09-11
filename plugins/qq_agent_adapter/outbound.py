"""出站投递：节流 + 长回复分层（单条 / 合并转发 / 合并转发 + 文件）。

**为什么要分层**：QQ 对发言频率敏感。把一条长回复硬切成 N 条连发，既打断阅读，
也把自己暴露在风控面里。合并转发（OneBot ``send_group_forward_msg`` /
``send_private_forward_msg``）把 N 段合成**一条**消息（群视角是一个「聊天记录」
卡片），发言次数从 N 降到 1，且文本仍可选中/复制/搜索——比"渲染成图片"划算得多。

阈值分层（环境变量可覆盖）
--------------------------
- 总字数 ``> AGENT_REPLY_FORWARD_MAX``（默认 **1500**）→ **直接发 md 文件**
  （私聊走 file_sender，群聊走 ``upload_group_file``）。超长内容发文件比刷屏或超大卡片都合适；
  发文件失败会降级回下面的文本分层，不让用户什么都收不到
- 段数 ``<= AGENT_REPLY_MERGE_SEGMENTS``（默认 **3**）→ 逐条文本（每段约
  ``AGENT_REPLY_SINGLE_MAX`` = 100 字）。少发几条更像真人，也降低风控面
- 段数 ``> 3`` → 合并转发成**一张卡片**（节点上限 ``AGENT_REPLY_FORWARD_MAX_NODES``，
  默认 30——100 字/段时 1500 字约 15 段，上限必须跟得上）

**为什么每段只 100 字**：QQ 单条消息虽有更大的字面上限，但人类不会一次发一大段；
按 100 字左右切分并按段数决定「逐条 / 卡片 / 文件」，既保留可读性，也不把自己暴露在风控面里。

**硬降级**：合并转发判定为「确定没发出去」时回落逐条发送——收不到回复比风控严重。
但**超时/连接断开不算「确定没发出去」**：请求可能已经送达，只是响应丢了，此时一律
不重发，返回 ``forward-unconfirmed`` 并打 ERROR 日志（宁可少发一次，也不要重复刷屏）。

节流
----
所有出站都过 :class:`OutboundThrottle`：per-target 最小间隔 + 每窗口条数上限，
外加一个**全局（账号级）最小间隔**。QQ 风控是按账号计的，只做 per-target 不足以
压住「多个群同时被推送」这类瞬时并发。

时钟与 sleep 可注入，测试不必真的等待。
"""
from __future__ import annotations

import asyncio
import base64
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

DEFAULT_SINGLE_MAX = 100        # 每段目标长度（切分粒度）：太长像刷屏且易触发风控
DEFAULT_MERGE_SEGMENTS = 3      # 段数**超过**它才合并转发；不超过就逐条发（更像真人）
DEFAULT_FORWARD_MAX = 1500      # 总字数超过它 → 直接发 md 文件，不再发文本/卡片
DEFAULT_MAX_NODES = 30          # 合并转发节点上限（100 字/段时 1500 字≈15 段，故放宽）
DEFAULT_MIN_INTERVAL = 1.0
DEFAULT_GLOBAL_MIN_INTERVAL = 0.4
DEFAULT_PER_WINDOW = 20
DEFAULT_WINDOW = 60.0
DEFAULT_MAX_WAIT = 10.0
DEFAULT_MAX_TARGETS = 4096

MODE_SINGLE = "single"
MODE_FORWARD = "forward"
MODE_CHUNKED = "chunked"
MODE_FILE = "file"
MODE_UNCONFIRMED = "forward-unconfirmed"

# _try_forward 的三态结果：只有 FAILED 才允许降级重发，UNCERTAIN 一律不重发
FORWARD_OK = "ok"
FORWARD_FAILED = "failed"
FORWARD_UNCERTAIN = "uncertain"


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
    """每段目标长度（切分粒度）。"""
    return _env_int("AGENT_REPLY_SINGLE_MAX", DEFAULT_SINGLE_MAX, minimum=100)


def merge_segments() -> int:
    """段数**超过**该值才合并转发；不超过则逐条发送（人不会一句话刷满屏）。"""
    return _env_int("AGENT_REPLY_MERGE_SEGMENTS", DEFAULT_MERGE_SEGMENTS, minimum=1)


def forward_max() -> int:
    """发文件阈值：总字数超过它就**直接发 md 文件**（不再发文本或卡片）。

    保证不小于单条阈值，否则分层语义自相矛盾。
    """
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
    """按句边界切分，避免在词/代码/URL 中间断开。

    **保留行内空白**：只在边界处断开，不逐行 strip——否则长回复里的代码块缩进与空行
    会被压平（评审发现：``def f(x):`` + 缩进的 ``if x:`` 会被压成顶格）。
    只裁掉每个分块**首尾**的空白。
    """
    if not text:
        return []
    if len(text) <= max_len:
        return [text]

    # 边界字符留在前一段、且不吞掉后续空白：'a\\n    b' -> ['a\\n', '    b']
    segments = re.split(r"(?<=[。！？；\n])", text)

    chunks: list[str] = []
    buf = ""

    for seg in segments:
        if not seg:
            continue
        if len(buf) + len(seg) <= max_len:
            buf += seg
            continue
        if buf:
            chunks.append(buf.strip())
            buf = ""
        if len(seg) <= max_len:
            buf = seg
            continue
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

    def prune(self, now: float, window: float) -> None:
        while self.history and now - self.history[0] >= window:
            self.history.popleft()

    def interval_wait(self, now: float, min_interval: float) -> float:
        """最小间隔分量：**硬约束**，不参与软上限。"""
        if self.last is not None and now - self.last < min_interval:
            return min_interval - (now - self.last)
        return 0.0

    def window_wait(self, now: float, per_window: int, window: float) -> float:
        """每窗口条数分量：**软约束**，超出时最多再等 max_wait 就放行。"""
        self.prune(now, window)
        if per_window > 0 and len(self.history) >= per_window:
            return window - (now - self.history[0])
        return 0.0

    def record(self, now: float) -> None:
        self.history.append(now)
        self.last = now


# acquire 的迭代上界：正常时钟下一两次就够；注入时钟（sleep 不推进时间）时靠它兜底，
# 否则会出现「残差被浮点吸收 → 永远差一点点 → 死循环」（评审已复现）。
_MAX_ACQUIRE_ITER = 32
_EPS = 1e-6


class OutboundThrottle:
    """出站节流：per-target 最小间隔 + 每窗口条数上限 + 全局（账号级）最小间隔。

    两个分量的性质不同（这是第一版写错的地方）：

    - **最小间隔（per-target + 全局）是硬约束**，任何情况下都完整执行。第一版把
      「最小间隔」和「窗口条数」合并成一个 wait，再对合并值套软上限，结果一旦越过
      窗口上限就**连最小间隔一起失效**——默认配置下同一会话可瞬时放行 21 条，
      正好与该模块「防风控」的目标相反。
    - **窗口条数上限是软约束**：需要等待超过剩余 ``max_wait`` 时，最多再等
      ``max_wait`` 就放行并告警，避免提醒/回复被无限期卡住。

    注意 ``max_wait`` 只约束窗口分量，不约束最小间隔：若把它配得比
    ``min_interval`` 还小，最小间隔仍然会被完整执行（``default_throttle`` 会告警）。
    另外它也不约束**排队**：同一 target 并发 N 次发送时，最后一个的总阻塞可达
    (N-1) × 单次持锁时间。
    """

    def __init__(
        self,
        *,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        per_window: int = DEFAULT_PER_WINDOW,
        window: float = DEFAULT_WINDOW,
        global_min_interval: float = DEFAULT_GLOBAL_MIN_INTERVAL,
        max_wait: float = DEFAULT_MAX_WAIT,
        max_targets: int = DEFAULT_MAX_TARGETS,
        clock=time.monotonic,
        sleep=asyncio.sleep,
    ) -> None:
        self.min_interval = min_interval
        self.per_window = per_window
        self.window = window
        self.global_min_interval = global_min_interval
        self.max_wait = max_wait
        self.max_targets = max_targets
        self._clock = clock
        self._sleep = sleep
        self._locks: dict[str, asyncio.Lock] = {}
        self._targets: dict[str, _Bucket] = {}
        self._global = _Bucket()
        self._last_seen = 0.0

    def _now(self) -> float:
        """读时钟并做单调钳制：时钟回拨会让窗口永远滚不过去、history 无界增长。"""
        now = self._clock()
        if now < self._last_seen:
            now = self._last_seen
        self._last_seen = now
        return now

    def _bucket(self, target: str) -> _Bucket:
        bucket = self._targets.get(target)
        if bucket is None:
            bucket = self._targets[target] = _Bucket()
        return bucket

    def _waits(self, target: str, now: float) -> tuple[float, float]:
        """返回 (最小间隔分量, 窗口条数分量)；都基于同一个 now。

        只读：不会为没见过的 target 建桶（``wait_for`` 因此没有副作用）。
        """
        wait_min = self._global.interval_wait(now, self.global_min_interval)
        wait_win = 0.0
        bucket = self._targets.get(target)
        if bucket is not None:
            wait_min = max(wait_min, bucket.interval_wait(now, self.min_interval))
            wait_win = bucket.window_wait(now, self.per_window, self.window)
        return wait_min, wait_win

    def wait_for(self, target: str) -> float:
        """当前还需要等多久（硬间隔与窗口分量取大者，不套软上限）。

        只读、不记账、不建桶，供测试与观测。
        """
        now = self._now()
        wait_min, wait_win = self._waits(target, now)
        return max(wait_min, wait_win)

    async def acquire(self, target: str) -> float:
        """取到一次发送额度，返回**本次自身**等待秒数（不含排队时间）。

        同 target 串行（FIFO，无饥饿），跨 target 只受全局约束。
        """
        lock = self._locks.get(target)
        if lock is None:
            lock = self._locks[target] = asyncio.Lock()
        async with lock:
            waited = 0.0
            for _ in range(_MAX_ACQUIRE_ITER):
                now = self._now()
                wait_min, wait_win = self._waits(target, now)
                # 最小间隔全量执行；窗口分量最多再等 max_wait 的余额
                grace = max(self.max_wait - waited, 0.0)
                wait = max(wait_min, min(wait_win, grace))
                if wait <= _EPS:
                    self._record(target, now)
                    return waited
                await self._sleep(wait)
                waited += wait
            logger.warning(
                "出站节流：%s 在 %d 轮内仍未满足间隔（注入时钟或极端参数？），放行并告警",
                target,
                _MAX_ACQUIRE_ITER,
            )
            self._record(target, self._now())
            return waited

    def _record(self, target: str, now: float) -> None:
        self._global.record(now)
        self._bucket(target).record(now)
        self._evict_idle()

    def _evict_idle(self) -> None:
        """限制 target 表大小：长期运行的机器人可能见过 1e5 个会话，不清理会缓慢泄漏。

        只淘汰当前没有被持锁的桶；被淘汰的桶会丢掉自己的窗口记录，属于有界性换精度的
        取舍（默认上限 4096 个会话，远大于同时活跃量）。
        """
        if self.max_targets <= 0 or len(self._targets) <= self.max_targets:
            return
        overflow = len(self._targets) - self.max_targets
        for target in list(self._targets.keys()):
            if overflow <= 0:
                return
            lock = self._locks.get(target)
            if lock is not None and lock.locked():
                continue
            self._targets.pop(target, None)
            self._locks.pop(target, None)
            overflow -= 1

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
            max_targets=_env_int("AGENT_OUTBOUND_MAX_TARGETS", DEFAULT_MAX_TARGETS, minimum=0),
        )
        th = _default_throttle
        if th.max_wait and th.max_wait < max(th.min_interval, th.global_min_interval):
            logger.warning(
                "AGENT_OUTBOUND_MAX_WAIT=%.1fs 小于最小间隔（per-target %.1fs / 全局 %.1fs）："
                "软上限只约束「窗口条数」分量，最小间隔仍会完整执行",
                th.max_wait,
                th.min_interval,
                th.global_min_interval,
            )
    return _default_throttle


# ---------------------------------------------------------------------------
# 发送
# ---------------------------------------------------------------------------
def _is_uncertain_failure(err: BaseException) -> bool:
    """异常是否**无法判断请求有没有送达**（超时/连接断开）。

    这类异常不能当作「没发出去」：请求可能已经抵达 OneBot 实现并发送成功，只是响应
    没回来。此时重试或降级重发都会让用户收到重复内容（评审已实测复现）。
    """
    if isinstance(err, TimeoutError):  # 3.11+ asyncio.TimeoutError 即 TimeoutError
        return True
    if type(err).__name__ in {"NetworkError", "WebSocketClosed", "ConnectionClosed"}:
        return True
    text = str(err).lower()
    return "timeout" in text or "timed out" in text


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
) -> str:
    """尝试合并转发，返回 ``FORWARD_OK`` / ``FORWARD_FAILED`` / ``FORWARD_UNCERTAIN``。

    **只有「确定没发出去」才允许降级重发**。超时/连接断开这类异常无法判断请求是否
    已经抵达实现（评审实测：把异常当成"没发出去"会导致同一份卡片发两次），此时返回
    ``FORWARD_UNCERTAIN``，由调用方放弃投递而不是重复发送。
    """
    if MessageSegment is None:  # pragma: no cover - 依赖缺失
        logger.warning("合并转发不可用：MessageSegment 未导入，降级逐条发送")
        return FORWARD_FAILED
    uid_text = str(self_id or "").strip()
    if not uid_text.isdigit():
        # node 必须标 bot 自己的 QQ；拿不到就退回逐条，避免伪造他人身份（风控点）
        logger.warning("合并转发缺少合法的 self_id（%r），降级逐条发送", self_id)
        return FORWARD_FAILED

    nodes = [
        MessageSegment.node_custom(user_id=int(uid_text), nickname=nickname, content=c)
        for c in chunks
    ]
    if kind == "group":
        attempts: list[tuple[str, dict]] = [
            ("send_group_forward_msg", {"group_id": ident, "messages": nodes}),
            # go-cqhttp 风格的统一接口（带 message_type）。注意：这条只在第一个 API
            # 抛错后才会用到，且字段形状未经真机验证——若你的实现两者都不认，
            # 会直接降级为逐条发送（见 README「长回复投递」）。
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
    # 额度按「一次逻辑投递」取一次：备用 API 只是同一份消息的另一种发法，
    # 若每次尝试都取额度，转发不被支持时每条长回复会白吃 2 份额度与 2 个最小间隔。
    await throttle_obj.acquire(f"{kind}:{ident}")
    for api, params in attempts:
        try:
            await bot.call_api(api, **params)
            logger.info("合并转发成功：%s api=%s nodes=%d", f"{kind}:{ident}", api, len(nodes))
            return FORWARD_OK
        except Exception as e:
            if _is_uncertain_failure(e):
                logger.error(
                    "合并转发结果未确认：%s api=%s err=%s —— 可能已送达，不再重发（避免重复）",
                    f"{kind}:{ident}",
                    api,
                    e,
                )
                return FORWARD_UNCERTAIN
            logger.warning(
                "合并转发失败：%s api=%s err=%s，尝试下一方案", f"{kind}:{ident}", api, e
            )
    return FORWARD_FAILED


async def _send_file(
    bot,
    kind: str,
    ident: int,
    text: str,
    *,
    throttle: OutboundThrottle | None = None,
) -> bool:
    """把长回复作为 md 文件发送；失败只记日志并返回 False（由调用方降级为文本）。

    - **私聊**：走 ``agentcore.skills.file_sender``（NapCat HTTP 优先，退化为 base64://）
    - **群聊**：走 OneBot ``upload_group_file``（``base64://`` 承载，免落盘）

    文件和文本一样要走节流；并且必须用**触发本次回复的 bot**，否则多账号部署时
    正文和附件会来自不同账号。
    """
    filename = "reply.md"
    await _resolve_throttle(throttle).acquire(f"{kind}:{ident}")
    if kind == "group":
        try:
            encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
            await bot.upload_group_file(
                group_id=int(ident), file=f"base64://{encoded}", name=filename
            )
            logger.info("长回复以群文件发送：%s (%d 字符)", f"{kind}:{ident}", len(text))
            return True
        except Exception:
            logger.warning("群文件发送失败，降级为文本投递：group=%s", ident, exc_info=True)
            return False
    try:
        from agentcore.skills.file_sender import FILE_SEND_OK_PREFIX, send_markdown_file

        result = await send_markdown_file(str(ident), text, bot=bot)
        if str(result).startswith(FILE_SEND_OK_PREFIX):
            return True
        logger.warning("长回复发文件未成功：%s", result)
    except Exception:
        logger.exception("长回复发文件异常")
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
    """按「长度分层」投递一条回复，返回实际使用的模式。

    分层（默认阈值，均可用 env 覆盖）：

    - 总字数 ``> AGENT_REPLY_FORWARD_MAX``（默认 1500）→ **直接发 md 文件**
      （长内容发文件比刷屏或超大卡片更合适；私聊走 file_sender，群聊走 upload_group_file。
      发文件失败则降级到下面的文本路径，不让用户什么都收不到）
    - 段数 ``<= AGENT_REPLY_MERGE_SEGMENTS``（默认 3）→ **逐条发送**（每段约 SINGLE_MAX 字）
    - 段数 ``> 3`` → **合并转发成一张卡片**

    模式：``file`` / ``single`` / ``chunked`` / ``forward`` /
    ``forward-unconfirmed``（投递结果未知，**刻意不重发**以免重复刷屏）。
    """
    text = text or ""

    # 1) 超长：直接发文件（失败则继续走文本分层，至少不静默丢消息）
    if len(text) > forward_max():
        if await _send_file(bot, kind, ident, text, throttle=throttle):
            return MODE_FILE
        logger.warning("长回复发文件失败，降级为文本投递：%s:%s", kind, ident)

    chunks = split_message(text, single_max())
    if len(chunks) <= 1:
        await _send_text(bot, kind, ident, chunks[0] if chunks else "（回复内容为空）", throttle)
        return MODE_SINGLE

    mode = ""
    # 2) 段数超过阈值才合并；否则逐条（≤3 条，阅读上更像真人连续发言）
    if forward_enabled() and len(chunks) > merge_segments() and len(chunks) <= max_nodes():
        status = await _try_forward(
            bot, kind, ident, chunks, self_id, nickname or bot_nickname(), throttle
        )
        if status == FORWARD_OK:
            mode = MODE_FORWARD
        elif status == FORWARD_UNCERTAIN:
            # 可能已送达：再发一遍就是重复刷屏，宁可不发（有 ERROR 日志可查）
            return MODE_UNCONFIRMED
    if not mode:
        logger.info("逐条发送：%s chunks=%d", f"{kind}:{ident}", len(chunks))
        failed = 0
        for index, chunk in enumerate(chunks, 1):
            try:
                await _send_text(bot, kind, ident, chunk, throttle)
            except Exception:
                # 单块失败不中断：否则后面所有分块都被丢掉，用户只看到一条报错
                failed += 1
                logger.exception("逐条发送失败：%s 第 %d/%d 块", f"{kind}:{ident}", index, len(chunks))
        if failed == len(chunks):
            raise RuntimeError(f"逐条发送全部失败（{failed} 块）")
        if failed:
            logger.warning("长回复有 %d/%d 块发送失败：%s", failed, len(chunks), f"{kind}:{ident}")
        mode = MODE_CHUNKED
    return mode
