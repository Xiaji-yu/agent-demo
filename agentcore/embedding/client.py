"""Embedding 客户端：优先 OpenAI 兼容 /embeddings API（EMBEDDING_* 配置）。

**未配置**远程（无 base_url/key）时走本地确定性 hash embedding（合法降级模式）；
**配置了但调用失败**时不再降级 hash，而是响亮失败（4xx 配置错误立即抛、
临时故障按预算退避重试后抛）——历史上"失败即降级 hash"把 9% 的 KB 块污染成
垃圾向量（评审复盘 P1 / dc46b8f）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import re
import time
from collections.abc import Awaitable, Callable

import httpx

from agentcore.budget import record_embedding_usage

logger = logging.getLogger(__name__)

DEFAULT_DIM = 2048  # 与 facts 表 vector(2048) 一致
DEFAULT_EMBED_BATCH = 10  # 单次请求的文本条数上限：DashScope 等服务超限直接 400
DEFAULT_EMBED_TIMEOUT = 30.0  # 单次请求超时（秒）；本地 CPU 推理需要调大
DEFAULT_EMBED_PROGRESS_EVERY = 200  # 每嵌入多少块打一条进度日志（0 = 关闭）


class EmbeddingClient:
    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        dim: int = DEFAULT_DIM,
        batch: int = DEFAULT_EMBED_BATCH,
        timeout: float = DEFAULT_EMBED_TIMEOUT,
        progress_every: int = DEFAULT_EMBED_PROGRESS_EVERY,
    ):
        self.base_url = (base_url or "").strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        self.model = (model or "").strip() or "text-embedding-3-small"
        self.dim = int(dim or DEFAULT_DIM)
        self.batch = max(1, int(batch)) if batch is not None else DEFAULT_EMBED_BATCH
        # 本地 CPU 推理（如 Ollama + bge-m3）单批可达数十秒：超时太紧会让大文件
        # 导入**每一批**都 ReadTimeout（且 httpx 的 str(exc) 为空串，日志里看不出
        # 原因）。可用 EMBEDDING_TIMEOUT 调大。
        self.timeout = float(timeout) if timeout else DEFAULT_EMBED_TIMEOUT
        # 大批量嵌入的进度日志间隔：一个 930 块的切块要 40 多分钟才提交一次，中间
        # 没有任何输出的话，用户无法区分「在慢慢跑」与「卡死」。0 = 关闭。
        self.progress_every = max(0, int(progress_every or 0))
        self._remote = bool(self.base_url and self.api_key)
        # 运行期失败回调（由宿主注入，如推送 QQ 提醒管理员）；带冷却防刷屏
        self.on_error: Callable[[Exception], Awaitable[None]] | None = None
        # 运行期进度回调（由宿主注入，如写入 /kb samples 的任务状态）。
        # **同步**调用，避免在热循环里引入 await 调度开销。
        self.on_progress: Callable[[int, int], None] | None = None
        self._error_notify_cooldown = 600.0
        # 用 None 表示"从未通知过"。不能用 0.0：time.monotonic() 是**开机以来的秒数**，
        # 刚重启的机器（uptime < cooldown）会让 `now - 0.0 < cooldown` 成立，
        # 从而吞掉首次告警——CI（新开机 runner）实测复现，本地长 uptime 机测不出。
        self._last_error_notify: float | None = None
        # 远程临时故障（429 限流 / 5xx / 超时 / 断连）的退避重试参数（评审复盘
        # P1 落地）：429/超时重发而非降级 hash——降级曾把 9% 的 KB 块污染成
        # 垃圾向量（硅基流动 TPM 限流触发的循环污染）。仅配置错误（4xx）与
        # 重试耗尽才响亮失败。
        #
        # L13：改为「告警 + clamp」，不再静默接受脏值/超大值。
        # M2（REVIEW-6ec3f7c..a36ea1d）：批量摄取要"重试到成功"（正确性优先），
        # 但交互路径（记忆召回/事实抽取）与启动探测不能陪跑——单次调用最坏
        # 5×60s 退避 + 5×超时 ≈ 17.5 分钟，会把一次普通发言挂死、把 on_startup
        # 挂 15 分钟（probe_dim 自称"不阻塞启动"被推翻）。故分两套预算：
        #   * 批量：retry_count / retry_delay（可长，配进度日志）
        #   * 交互：interactive_retry_count + interactive_budget（墙钟上限，快速失败）
        #   * 探测：probe 单次尝试（retry_count=0）
        self.retry_count = _env_clamped_int("EMBEDDING_RETRY_COUNT", 5, 0, 10)
        self.retry_delay = _env_nonneg_float("EMBEDDING_RETRY_BASE_DELAY", 60.0)
        self.interactive_retry_count = _env_clamped_int(
            "EMBEDDING_INTERACTIVE_RETRY_COUNT", 1, 0, 10
        )
        self.interactive_budget = _env_positive_float(
            "EMBEDDING_INTERACTIVE_BUDGET", 30.0
        )
        # 实例级长生命周期连接池：交互路径每条用户消息都会 embed_many 一次，
        # 逐次新建 AsyncClient = 逐次 TCP+TLS 重握手（与 LLMClient 口径一致）
        self._http: httpx.AsyncClient | None = None
        if self._remote:
            logger.info("Embedding: remote API %s model=%s", self.base_url, self.model)
        else:
            logger.info(
                "Embedding: local fallback dim=%s (配置 EMBEDDING_BASE_URL/API_KEY/MODEL 启用语义向量)",
                self.dim,
            )

    def _http_client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=self.timeout)
        return self._http

    async def aclose(self) -> None:
        """关闭共享连接池（停机时调用；幂等）。"""
        client, self._http = self._http, None
        if client is not None and not client.is_closed:
            try:
                await client.aclose()
            except Exception:
                logger.warning("embedding client aclose failed", exc_info=True)

    async def _maybe_notify_error(self, exc: Exception) -> None:
        """远程调用失败时触发 on_error 回调（冷却期内只触发一次）。"""
        if self.on_error is None:
            return
        now = time.monotonic()
        if (
            self._last_error_notify is not None
            and now - self._last_error_notify < self._error_notify_cooldown
        ):
            return
        self._last_error_notify = now
        try:
            await self.on_error(exc)
        except Exception:
            logger.warning("embedding on_error callback failed", exc_info=True)

    async def embed(self, text: str) -> list[float]:
        """单条嵌入（交互路径）：用短预算 + 墙钟上限，绝不长时阻塞对话。"""
        return (await self.embed_many([text], interactive=True))[0]

    async def probe_dim(self) -> int:
        """探测并设置实际向量维度（远程模型以真实输出为准，本地用配置 dim）。

        探测失败**不抛异常**（README：embedding 不可达不阻塞启动），留痕后用
        配置维度；运行期语义见 embed_many（远程失败响亮失败，不再降级 hash）。

        M2：探测**单次尝试**（``retry_count=0``）。旧实现走批量退避预算，
        embedding 不可达时 `on_startup` 里的这一次 await 会被挂 ~15 分钟
        （5×60s 退避 + 5×超时），与"不阻塞启动"的声称正好相反。
        """
        if self._remote:
            try:
                vecs = await self._remote_embed(["ping"], retry_count=0)
            except Exception as exc:
                logger.warning(
                    "embedding probe 失败（%s），回退配置维度 dim=%s；"
                    "运行期首次调用会再试远程",
                    type(exc).__name__,
                    self.dim,
                )
                return self.dim
            if vecs:
                self.dim = len(vecs[0])
        return self.dim

    async def embed_many(
        self, texts: list[str], *, interactive: bool = False
    ) -> list[list[float]]:
        """批量嵌入。``interactive=True`` 用于对话路径（召回/事实抽取）。

        M2：两条路径的失败预算不同——
        * 批量摄取（默认）：``retry_count`` 次退避，可长（正确性优先，配进度日志）；
        * 交互（``interactive=True``）：``interactive_retry_count`` 次且受
          ``interactive_budget`` 墙钟上限约束，到点快速失败而不是静默挂 15 分钟。
        """
        if not texts:
            return []
        if not self._remote:
            # 未配置远程（无 base_url/key）：合法的本地模式，直接 hash。
            # 纯 Python 双循环在大批量（KB 导入）会长时间占住事件循环 → to_thread
            return [
                *await asyncio.to_thread(lambda: [self._local_embed(t) for t in texts])
            ]
        # 远程失败的 log + 宿主通知（带冷却）由 _remote_embed 统一完成，
        # 这里不再重复 try/except（曾导致同一异常通知宿主两次）——异常自然传播：
        # 4xx 配置错误与重试耗尽后响亮失败，由调用方处理（ingest 中止报错、
        # facts 抽取跳过、召回为空——都不产生垃圾向量，评审复盘 P1）。
        if interactive:
            vecs = await self._remote_embed(
                texts,
                retry_count=self.interactive_retry_count,
                deadline=time.monotonic() + self.interactive_budget,
            )
        else:
            vecs = await self._remote_embed(texts)
        # 成功路径：维度一致性检查（DB 列维度在 init 时已固定，只告警不静默改，
        # 避免运行期维度漂移导致 save_fact 全部失败）
        if vecs:
            real_dim = len(vecs[0])
            if real_dim != self.dim:
                logger.warning(
                    "embedding model returned dim=%s but runtime dim=%s; "
                    "re-run with matching config / AGENT_MIGRATE_VECTOR=1 if schema needs change",
                    real_dim,
                    self.dim,
                )
        return vecs

    async def _remote_embed(
        self,
        texts: list[str],
        *,
        retry_count: int | None = None,
        deadline: float | None = None,
    ) -> list[list[float]]:
        vecs: list[list[float]] = []
        total = len(texts)
        started = time.monotonic()
        next_log = self.progress_every
        try:
            client = self._http_client()
            for start in range(0, total, self.batch):
                batch = texts[start : start + self.batch]
                vecs.extend(
                    await self._post_embeddings(
                        client,
                        batch,
                        start,
                        total,
                        retry_count=retry_count,
                        deadline=deadline,
                    )
                )
                done = len(vecs)
                if self.on_progress is not None:
                    try:
                        self.on_progress(done, total)
                    except Exception:
                        logger.warning(
                            "embedding on_progress callback failed", exc_info=True
                        )
                        self.on_progress = None  # 不再重试，避免刷日志
                if self.progress_every and done >= next_log:
                    # 关键可观测性：一个 930 块的切块要 40 多分钟才写库，中间没有
                    # 输出的话，用户无法区分「在慢慢跑」与「卡死」
                    logger.info(
                        "embedding 进度 %d/%d（%.0f%%），已用 %.0fs",
                        done,
                        total,
                        done * 100 / total if total else 100.0,
                        time.monotonic() - started,
                    )
                    while next_log <= done:
                        next_log += self.progress_every
        except Exception as exc:
            # 超时/连接类异常（httpx.ReadTimeout 等）的 str() 是**空串**，调用方
            # 常见的 `logger.warning("... %s", e)` 会打出一行没有原因的日志。这里
            # 先按类型+repr 留痕，再原样抛出（不改变异常类型，调用方语义不变）。
            logger.warning(
                "embedding 请求失败：%s: %r（timeout=%ss, batch=%d, 文本数=%d）",
                type(exc).__name__,
                exc,
                self.timeout,
                self.batch,
                total,
            )
            # 服务不可达/报错时通知宿主（如推送提醒管理员 Ollama 未启动），再原样抛出
            await self._maybe_notify_error(exc)
            raise
        if self.progress_every and total >= self.progress_every:
            logger.info(
                "embedding 完成 %d 块，用时 %.1fs",
                len(vecs),
                time.monotonic() - started,
            )
        return vecs

    async def _post_once(self, client: httpx.AsyncClient, batch: list[str]):
        """单次 POST（不含重试）；异常原样抛出。"""
        return await client.post(
            f"{self.base_url}/embeddings",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={"model": self.model, "input": batch},
        )

    async def _post_embeddings(
        self,
        client: httpx.AsyncClient,
        batch: list[str],
        start: int,
        total: int,
        *,
        retry_count: int | None = None,
        deadline: float | None = None,
    ) -> list[list[float]]:
        """单批请求：429/5xx/超时/断连**退避重试**，4xx 配置错误**响亮失败**。

        评审复盘 P1 落地：429（限流）与临时故障重发而非降级——旧实现撞到
        硅基流动 TPM 限流就降级 hash 300 秒，把导入的 KB 块污染成垃圾向量
        （实测 42119 块中 3854 块 hash）。

        M2：``retry_count`` 与 ``deadline``（``time.monotonic()`` 绝对时刻）可由
        调用方收窄。交互路径用短预算 + 墙钟上限，到达上限即失败，不再 sleep。
        """
        budget = self.retry_count if retry_count is None else retry_count

        def _can_retry(attempt: int) -> bool:
            return attempt <= budget

        async def _backoff(attempt: int, why: str) -> bool:
            """睡 ``min(delay*attempt, 剩余预算)``；预算耗尽则放弃重试。

            旧实现 wait 恒等于 delay*attempt：默认配置下 60s 退避对 30s 交互
            预算永远越界 → EMBEDDING_INTERACTIVE_RETRY_COUNT 在默认值下从不
            生效（重审 verified：attempts 恒为 1）。
            """
            wait = self.retry_delay * attempt
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "embedding %s：交互预算已耗尽，放弃重试（快速失败）", why
                    )
                    return False
                # 短退避（>0.05s）时夹到剩余预算，把「预算内再试一次」做实
                if wait > remaining:
                    wait = max(0.05, remaining * 0.5)
            await asyncio.sleep(wait)
            return True

        attempt = 0
        while True:
            attempt += 1
            try:
                resp = await self._post_once(client, batch)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                # M15：原只捕 (TimeoutException, NetworkError)，而
                # RemoteProtocolError/ProxyError/UnsupportedProtocol 走
                # ProtocolError/TransportError 分支 → 「服务端中途断连」不重试，
                # 与 commit dc46b8f 声称的「超时/断连 → 退避重试」不符。
                # 配置类错误（UnsupportedProtocol）给指引后立即失败。
                if isinstance(e, httpx.UnsupportedProtocol):
                    raise RuntimeError(
                        f"embeddings 请求的 URL 非法（EMBEDDING_BASE_URL 需含 "
                        f"http:// 或 https:// 且不含资源路径）：{self.base_url!r}"
                    ) from e
                if _can_retry(attempt):
                    logger.warning(
                        "embedding 请求超时/断连（第 %d-%d 条 / 共 %d 条）：%s，"
                        "%.0fs 后重试（%d/%d）",
                        start + 1,
                        start + len(batch),
                        total,
                        type(e).__name__,
                        self.retry_delay * attempt,
                        attempt,
                        budget,
                    )
                    if await _backoff(attempt, type(e).__name__):
                        continue
                raise RuntimeError(
                    f"embedding 请求超时/断连（第 {start + 1}-{start + len(batch)} 条 / 共 {total} 条）："
                    f"重试 {budget} 次仍失败：{type(e).__name__}: {e!r}"
                ) from e
            if resp.status_code == 429 or resp.status_code >= 500:
                if _can_retry(attempt):
                    # L12：429 优先听服务端的 Retry-After（秒数），但受退避上限约束
                    wait = self.retry_delay * attempt
                    retry_after = _parse_retry_after(resp.headers.get("retry-after"))
                    if retry_after is not None:
                        wait = min(retry_after, self.retry_delay * attempt)
                    logger.warning(
                        "embedding 被限流/服务端错误（HTTP %d，第 %d-%d 条 / 共 %d 条），"
                        "%.0fs 后重试（%d/%d）",
                        resp.status_code,
                        start + 1,
                        start + len(batch),
                        total,
                        wait,
                        attempt,
                        budget,
                    )
                    if deadline is not None and time.monotonic() + wait > deadline:
                        logger.warning(
                            "embedding 退避 %.0fs 会超出交互预算，放弃重试（快速失败）",
                            wait,
                        )
                    else:
                        await asyncio.sleep(wait)
                        continue
                raise RuntimeError(
                    f"embeddings API {resp.status_code}"
                    f"（第 {start + 1}-{start + len(batch)} 条 / 共 {total} 条）："
                    f"重试 {budget} 次仍失败：{resp.text[:300]}"
                )
            if resp.status_code in (413, 422):
                # L12：这两类的真实含义是"请求过大/单条超 token"，唯一有效指引
                # 是调小批大小——旧实现把它们归入"检查 MODEL/BASE_URL"，是错误归因。
                raise RuntimeError(
                    f"embeddings API {resp.status_code}"
                    f"（第 {start + 1}-{start + len(batch)} 条 / 共 {total} 条）："
                    f"{resp.text[:300]}"
                    f"（请求被拒：请调小 EMBEDDING_BATCH={self.batch} 或缩短单条文本）"
                )
            if resp.status_code >= 400:
                # 4xx（404 模型名错等配置错误）：响亮失败，不重试不降级。
                # 响应体里有上游真实原因，原样透出片段便于排障。
                raise RuntimeError(
                    f"embeddings API {resp.status_code}"
                    f"（第 {start + 1}-{start + len(batch)} 条 / 共 {total} 条）："
                    f"{resp.text[:300]}"
                    "（配置错误不重试：请检查 EMBEDDING_MODEL / EMBEDDING_BASE_URL 与服务商一致）"
                )
            data = resp.json()
            # M7 成本预算：embedding 用量（total_tokens）按日累计
            record_embedding_usage(data.get("usage"), model=self.model)
            items = data.get("data") or []
            ordered = sorted(items, key=lambda it: it.get("index", 0))
            return [list(it["embedding"]) for it in ordered]

    # ---------- 本地降级：字符/双字符 bag hashing ----------
    # 说明：无 EMBEDDING_API_KEY 时的兜底方案，仅近似「词面重叠」，
    # 不具备语义泛化能力；要真正的语义召回请配置 OpenAI 兼容 embedding 服务。
    def _local_embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        s = re.sub(r"\s+", "", text or "").lower()
        chars = list(s)
        if not chars:
            chars = ["<empty>"]
        for ch in chars:
            self._add_token(vec, ch)
        for i in range(len(chars) - 1):
            self._add_token(vec, chars[i] + chars[i + 1])
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec

    @staticmethod
    def _add_token(vec: list[float], token: str) -> None:
        h = int(hashlib.blake2b(token.encode("utf-8"), digest_size=8).hexdigest(), 16)
        idx = h % len(vec)
        sign = 1.0 if (h >> 8) % 2 == 0 else -1.0
        vec[idx] += sign


def _env_positive_float(name: str, default: float) -> float:
    """读一个正浮点 env；缺省/脏值/非正数一律告警并回退默认（不崩启动）。"""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r 不是数字，回退 %.0fs", name, raw, default)
        return default
    if not math.isfinite(value) or value <= 0:
        logger.warning("%s=%r 非法（须 > 0），回退 %.0fs", name, raw, default)
        return default
    return value


def _env_nonneg_float(name: str, default: float) -> float:
    """读一个**非负**浮点 env（0 合法）；脏值/负数告警后回退默认。

    与 :func:`_env_positive_float` 分开：退避基准延迟必须允许 0（"不等待"是
    合法配置，测试也依赖它），而超时类参数 0 无意义。
    """
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r 不是数字，回退 %.0fs", name, raw, default)
        return default
    if not math.isfinite(value) or value < 0:
        logger.warning("%s=%r 非法（须 >= 0），回退 %.0fs", name, raw, default)
        return default
    return value


def _parse_retry_after(raw: str | None) -> float | None:
    """解析 ``Retry-After`` 头（秒数形式）；HTTP-date 形式与脏值一律返回 None（L12）。"""
    if not raw:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _env_clamped_int(name: str, default: int, low: int, high: int) -> int:
    """读一个带上下限的整数 env；脏值/越界一律**告警**后夹到边界（L13）。

    旧实现对 ``EMBEDDING_RETRY_COUNT`` 用裸 ``try/except ValueError`` 静默回落，
    既无告警也无上限——``EMBEDDING_RETRY_COUNT=100`` 会被原样接受，与退避 sleep
    相乘可把最坏等待从 900s 放大到数小时。这里统一成「告警 + clamp」。
    """
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r 不是整数，回退 %d", name, raw, default)
        return default
    if value < low or value > high:
        clamped = min(max(value, low), high)
        logger.warning("%s=%r 超出 [%d, %d]，夹到 %d", name, raw, low, high, clamped)
        return clamped
    return value


def load_embedding_client_from_env() -> EmbeddingClient:
    # dim/batch 走 clamp 解析而不是裸 int()：.env 留空（EMBEDDING_DIM= 是常见
    # 形态）曾让 import 期 ValueError → bot 起不来（与 llm/client.py 的评审
    # M6 同源；留空 env 的 os.getenv 返回 "" 而非默认值）
    return EmbeddingClient(
        base_url=os.getenv("EMBEDDING_BASE_URL", ""),
        api_key=os.getenv("EMBEDDING_API_KEY", ""),
        model=os.getenv("EMBEDDING_MODEL", ""),
        dim=_env_clamped_int("EMBEDDING_DIM", DEFAULT_DIM, 1, 100000),
        batch=_env_clamped_int("EMBEDDING_BATCH", DEFAULT_EMBED_BATCH, 1, 100),
        timeout=_env_positive_float("EMBEDDING_TIMEOUT", DEFAULT_EMBED_TIMEOUT),
    )
