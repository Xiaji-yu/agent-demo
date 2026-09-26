"""知识库服务门面：把 store / embedding / 配置组装成 engine、admin、scheduler 用的接口。

配置来自 config.yaml 的 `rag:` 段（可被环境变量覆盖）：
    rag:
      enabled: true
      top_k: 4              # 每次注入多少条检索结果
      threshold: 0.3        # 相似度下限
      chunk_chars: 600      # 切块目标长度
      digest_cron: "0 3 * * *"  # 每天蒸馏时间（5 段 cron）
      digest_batch: 200     # 每次最多处理多少条新消息
      max_entries: 8        # 每次蒸馏最多沉淀多少条知识
      min_chars: 200        # 新内容少于该长度则跳过本次蒸馏
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from typing import Any

from agentcore.budget import get_budget
from agentcore.rag.distill import distill_from_memory, summarize
from agentcore.rag.ingest import ingest_file, ingest_file_smart, ingest_text
from agentcore.rag.retriever import format_block, retrieve

logger = logging.getLogger(__name__)

DEFAULTS = {
    "enabled": True,
    "top_k": 4,
    "threshold": 0.3,
    "chunk_chars": 600,
    "digest_cron": "0 3 * * *",
    "digest_batch": 200,
    "max_entries": 8,
    "min_chars": 200,
    "distill_max_tokens": 2048,
}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_max_chunks(config_value) -> int | None:
    """单来源块数上限，优先级 **env > config.yaml > ingest 默认(200)**（M1）。

    此前只要 config.yaml 写了值就无条件采用，而本仓 config.yaml 默认写着 1000，
    于是 ``AGENT_KB_MAX_CHUNKS_PER_SOURCE`` 在默认部署下**完全失效**——偏偏
    `.env.example`、`config.yaml` 注释、README 三处文档和 ingest 的 WARNING、
    admin 文案、脚本汇总行三处运行时提示都在推荐用它。同文件的
    ``enabled``/``digest_cron`` 早就是 env 优先，这里对齐。

    返回值 ``None`` 表示"没有可信的显式上限"，交给 ``ingest.max_chunks_per_source()``
    继续读 env / 用默认值；脏值不抛异常（M2：此前 ``int("abc")`` 会让 bot 启动即失败）。
    """
    from agentcore.rag.ingest import MAX_CHUNKS_ENV

    raw_env = (os.getenv(MAX_CHUNKS_ENV) or "").strip()
    if raw_env:
        try:
            value = int(raw_env)
        except ValueError:
            logger.warning(
                "%s=%r 不是整数，改用 config.yaml 的值", MAX_CHUNKS_ENV, raw_env
            )
        else:
            if value > 0:
                return value
            logger.warning(
                "%s=%s 非法（须 > 0），改用 config.yaml 的值", MAX_CHUNKS_ENV, value
            )

    if config_value is None or config_value == "":
        return None
    try:
        value = int(config_value)
    except (TypeError, ValueError):
        logger.warning(
            "rag.max_chunks_per_source=%r 不是整数，回退内置默认", config_value
        )
        return None
    if value <= 0:
        logger.warning(
            "rag.max_chunks_per_source=%s 非法（须 > 0），回退内置默认", value
        )
        return None
    return value


def _resolve_positive_int(raw, *, label: str, default: int) -> int:
    """把 env/config 来的上限值收敛成正整数；脏值告警并回退默认（M2 纪律）。

    M8（REVIEW-c472e56..733f57e）：蒸馏两个 prompt 上限此前用裸 ``int()``，
    env 或 config.yaml 配了脏值会让 bot **启动即崩**——与本文件
    `_resolve_max_chunks` docstring 里记录的教训同源。
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("%s=%r 不是整数，回退默认 %d", label, raw, default)
        return default
    if value <= 0:
        logger.warning("%s=%s 非法（须 > 0），回退默认 %d", label, value, default)
        return default
    return value


def _resolve_int(raw, *, label: str, default: int, minimum: int | None = None) -> int:
    """config 数值项统一解析：脏值/越界告警后回退默认，绝不抛给启动路径。"""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("%s=%r 不是整数，回退默认 %s", label, raw, default)
        return default
    if minimum is not None and value < minimum:
        logger.warning(
            "%s=%s 非法（须 >= %s），回退默认 %s", label, value, minimum, default
        )
        return default
    return value


def _resolve_float(
    raw,
    *,
    label: str,
    default: float,
    low: float | None = None,
    high: float | None = None,
) -> float:
    """config 浮点项统一解析：脏值/越界（含 nan/inf）告警后回退默认。"""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("%s=%r 不是数字，回退默认 %s", label, raw, default)
        return default
    if not math.isfinite(value) or (
        (low is not None and value < low) or (high is not None and value > high)
    ):
        logger.warning(
            "%s=%s 非法（须在 [%s, %s] 内），回退默认 %s",
            label,
            value,
            low,
            high,
            default,
        )
        return default
    return value


class KnowledgeBase:
    def __init__(self, store, embedding, config: dict | None = None, llm=None):
        cfg = dict(DEFAULTS)
        cfg.update(config or {})
        self.store = store
        self.embedding = embedding
        self.llm = llm
        self.enabled = _env_bool("AGENT_KB_ENABLED", bool(cfg["enabled"]))
        # M8 同款纪律扩展到全部数值配置：脏值/越界告警回退默认，不再让
        # config.yaml 里的一个笔误（如 top_k: "4"）在启动期炸掉整个 bot
        self.top_k = _resolve_int(cfg["top_k"], label="rag.top_k", default=4, minimum=1)
        self.threshold = _resolve_float(
            cfg["threshold"], label="rag.threshold", default=0.3, low=0.0, high=1.0
        )
        self.chunk_chars = _resolve_int(
            cfg["chunk_chars"], label="rag.chunk_chars", default=600, minimum=1
        )
        self.digest_cron = os.getenv("AGENT_KB_DIGEST_CRON", str(cfg["digest_cron"]))
        self.digest_batch = _resolve_int(
            cfg["digest_batch"], label="rag.digest_batch", default=200, minimum=1
        )
        self.max_entries = _resolve_int(
            cfg["max_entries"], label="rag.max_entries", default=8, minimum=1
        )
        self.min_chars = _resolve_int(
            cfg["min_chars"], label="rag.min_chars", default=200, minimum=0
        )
        # 推理型模型会把预算耗在 reasoning 上 → 蒸馏需要更大的输出上限
        self.distill_max_tokens = _resolve_int(
            cfg["distill_max_tokens"],
            label="rag.distill_max_tokens",
            default=2048,
            minimum=1,
        )
        # 蒸馏 prompt 长度上限（字符）：env > config.yaml > 内置默认（与 config.yaml
        # 的 1000/20000 对齐；M8：脏值告警回退，不再让 bot 启动即崩）
        self.distill_per_message_cap = _resolve_positive_int(
            os.getenv("AGENT_KB_DISTILL_PER_MESSAGE_CAP")
            or cfg.get("distill_per_message_cap", 1000),
            label="AGENT_KB_DISTILL_PER_MESSAGE_CAP",
            default=1000,
        )
        self.distill_total_cap = _resolve_positive_int(
            os.getenv("AGENT_KB_DISTILL_TOTAL_CAP")
            or cfg.get("distill_total_cap", 20000),
            label="AGENT_KB_DISTILL_TOTAL_CAP",
            default=20000,
        )
        # L10（REVIEW-c472e56..733f57e）：total_cap 小于单行上限时 render_transcript
        # 首行即截断 → transcript 恒空 → min_chars 跳过且水位不推进 → 同批永久重试。
        # 给一个下界钳制，保证至少能装下一条完整消息。
        if self.distill_total_cap < self.distill_per_message_cap * 2:
            logger.warning(
                "distill_total_cap=%d 小于 per_message_cap=%d 的两倍，"
                "按 %d 处理（否则蒸馏会永久空转）",
                self.distill_total_cap,
                self.distill_per_message_cap,
                self.distill_per_message_cap * 2,
            )
            self.distill_total_cap = self.distill_per_message_cap * 2
        # 单来源块数上限：env > config.yaml（rag.max_chunks_per_source）> 内置默认 200
        self.max_chunks_per_source = _resolve_max_chunks(
            cfg.get("max_chunks_per_source")
        )
        # L10：手动 /kb digest 与 cron 可能同时触发，蒸馏全程持锁防双跑重复入库
        self._digest_lock = asyncio.Lock()

    # ---------- 检索（engine 用） ----------
    async def retrieve(self, query: str) -> list[dict]:
        if not self.enabled:
            return []
        return await retrieve(
            self.store,
            self.embedding,
            query,
            top_k=self.top_k,
            threshold=self.threshold,
        )

    def format_block(self, hits: list[dict]) -> str:
        return format_block(hits)

    # ---------- 摄取（管理命令用） ----------
    async def add_text(self, text: str, name: str, kind: str = "manual") -> dict:
        self._require_enabled()
        return await ingest_text(
            self.store,
            self.embedding,
            text,
            name=name,
            kind=kind,
            max_chars=self.chunk_chars,
            max_chunks=self.max_chunks_per_source,
        )

    async def add_file(
        self, path: str, name: str | None = None, kind: str = "file"
    ) -> dict:
        self._require_enabled()
        return await ingest_file(
            self.store,
            self.embedding,
            path,
            name=name,
            kind=kind,
            max_chars=self.chunk_chars,
            max_chunks=self.max_chunks_per_source,
        )

    async def add_file_smart(
        self, path: str, name: str | None = None, kind: str = "file"
    ) -> dict:
        """智能导入：大文件自动切块落盘后逐块入库（源文件保留）。

        与 ``add_file`` 的差别只在超限处理：``add_file`` 对 >2MB 直接抛错、
        对超出块数上限的部分静默砍尾；本方法改为在同目录 ``<stem>/`` 下写出
        块文件并逐块入库，不再丢内容。
        """
        self._require_enabled()
        return await ingest_file_smart(
            self.store,
            self.embedding,
            path,
            name=name,
            kind=kind,
            max_chars=self.chunk_chars,
            max_chunks=self.max_chunks_per_source,
        )

    def _require_enabled(self) -> None:
        """``AGENT_KB_ENABLED=0`` 时拒绝摄取（评审 L7）。

        此前 ``enabled`` 只门控 ``retrieve``/``digest``，``add_*`` 仍可写入，
        与 README「``AGENT_KB_ENABLED=0`` 可整体关闭」不符。
        """
        if not self.enabled:
            raise RuntimeError("知识库已关闭（AGENT_KB_ENABLED=0），拒绝摄取")

    # ---------- 蒸馏（定时任务 / 手动触发） ----------
    async def digest(self) -> dict:
        if not self.enabled:
            return {"status": "skipped", "reason": "knowledge base disabled"}
        if self.llm is None:
            return {"status": "skipped", "reason": "llm unavailable"}
        # M7 成本预算：硬闸开启且当日超预算时跳过蒸馏（知识成长让位于预算）
        blocked, _ = get_budget().chat_blocked()
        if blocked:
            return {"status": "skipped", "reason": "daily budget exceeded"}
        async with self._digest_lock:
            try:
                result = await distill_from_memory(
                    self.llm,
                    self.store,
                    self.embedding,
                    batch=self.digest_batch,
                    max_entries=self.max_entries,
                    min_chars=self.min_chars,
                    max_tokens=self.distill_max_tokens,
                    per_message_cap=self.distill_per_message_cap,
                    total_cap=self.distill_total_cap,
                )
            except Exception:
                logger.exception("knowledge distillation failed")
                return {"status": "error", "reason": "exception (see logs)"}
            logger.info("kb digest: %s", summarize(result))
            return result

    # ---------- 管理 ----------
    async def list_sources(self, limit: int = 20) -> list[dict]:
        return await self.store.kb_list_sources(limit=limit)

    async def delete_source(self, source_id: str) -> int:
        # M3：删除也必须受 AGENT_KB_ENABLED 门控。此前只门控 add_*，导致
        # 「关闭知识库」后仍能删光全库（与 --replace 组合时：删除全成功、写入全被拒，
        # 结果库中一条不剩）。
        self._require_enabled()
        return await self.store.kb_delete_source(source_id)

    async def stats(self) -> dict:
        return await self.store.kb_stats()

    def describe(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "top_k": self.top_k,
            "threshold": self.threshold,
            "digest_cron": self.digest_cron,
            "embedding": "on" if self.embedding is not None else "off",
        }
