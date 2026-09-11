"""成本预算（M7）：LLM/embedding 用量按日累计 + 可选硬闸门 + 成本估算。

用量来源是 OpenAI 风格响应里的 usage 字段（prompt_tokens / completion_tokens /
total_tokens），由 LLMClient 与 EmbeddingClient 在响应处理处上报本模块。数据按月
落盘到 ``data/budget/usage-YYYY-MM.json``（按天累计、原子写、重启不丢）。

- **只记录不限流**（默认）：`AGENT_BUDGET_DAILY_TOKENS=0` 或未设置
- **软预算**：设了每日 token 预算时，`/status` 展示用量与余量，首次到达打 WARNING
- **硬闸门**：再加 `AGENT_BUDGET_ENFORCE=1`，超限后聊天（engine.run）与蒸馏
  （kb.digest）直接返回预算提示，当日不再发起任何 LLM 调用，次日自动恢复
- **成本估算**：配置单价（元/百万 token）后 `/status` 可展示当日估算成本
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

_TRUE = {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _env_float(name: str) -> float | None:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        logger.warning("budget: invalid %s=%r, ignored", name, raw)
        return None


class CostBudget:
    """按日累计的用量账本。root 指定落盘目录；其余参数缺省读环境变量。"""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        daily_tokens: int | None = None,
        enforce: bool | None = None,
        price_prompt_per_m: float | None = None,
        price_completion_per_m: float | None = None,
    ):
        self.root = Path(root) if root else Path(os.getenv("AGENT_BUDGET_DIR", "data/budget"))
        self.daily_tokens = (
            daily_tokens if daily_tokens is not None else _env_int("AGENT_BUDGET_DAILY_TOKENS", 0)
        )
        if enforce is not None:
            self.enforce = enforce
        else:
            self.enforce = (os.getenv("AGENT_BUDGET_ENFORCE") or "").strip().lower() in _TRUE
        self.price_prompt = (
            price_prompt_per_m
            if price_prompt_per_m is not None
            else _env_float("AGENT_PRICE_PROMPT_PER_M")
        )
        self.price_completion = (
            price_completion_per_m
            if price_completion_per_m is not None
            else _env_float("AGENT_PRICE_COMPLETION_PER_M")
        )
        self._loaded_month: str | None = None
        self._days: dict[str, dict] = {}

    # ---------- 持久化（按月一个 JSON，.part + os.replace 原子写） ----------
    def _month_file(self, month: str) -> Path:
        return self.root / f"usage-{month}.json"

    def _ensure_loaded(self, month: str) -> None:
        if self._loaded_month == month:
            return
        path = self._month_file(month)
        if path.is_file():
            try:
                self._days = json.loads(path.read_text(encoding="utf-8")).get("days", {})
            except Exception:
                logger.warning("budget: corrupt usage file %s, starting fresh", path)
                self._days = {}
        else:
            self._days = {}
        self._loaded_month = month

    def _save(self, month: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._month_file(month)
        tmp = path.with_suffix(".part")
        tmp.write_text(json.dumps({"days": self._days}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def _day(self, now: date) -> dict:
        self._ensure_loaded(now.strftime("%Y-%m"))
        return self._days.setdefault(
            now.isoformat(),
            {
                "prompt": 0,
                "completion": 0,
                "embedding_tokens": 0,
                "chat_requests": 0,
                "embedding_requests": 0,
            },
        )

    # ---------- 记录 / 查询 ----------
    def record(self, kind: str, prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
        """累加一次调用。kind="chat" 计入对话 token；kind="embedding" 的 token
        只记入 embedding_tokens，不参与对话预算的判定。"""
        now = date.today()  # 单次取时贯穿月与日键，避免跨月午夜竞态（评审 M4）
        day = self._day(now)
        if kind == "embedding":
            day["embedding_requests"] += 1
            day["embedding_tokens"] += int(prompt_tokens) + int(completion_tokens)
        else:
            day["chat_requests"] += 1
            day["prompt"] += int(prompt_tokens)
            day["completion"] += int(completion_tokens)
        used = day["prompt"] + day["completion"]
        if self.daily_tokens > 0 and used >= self.daily_tokens and not day.get("warned"):
            day["warned"] = True
            logger.warning(
                "budget: daily LLM token budget reached (%d/%d)%s",
                used,
                self.daily_tokens,
                "，硬闸门已开启" if self.enforce else "",
            )
        self._save(now.strftime("%Y-%m"))

    def today(self) -> dict:
        now = date.today()
        day = self._day(now)
        return {
            "date": now.isoformat(),
            "prompt": day["prompt"],
            "completion": day["completion"],
            "embedding_tokens": day["embedding_tokens"],
            "chat_requests": day["chat_requests"],
            "embedding_requests": day["embedding_requests"],
            "total": day["prompt"] + day["completion"],
        }

    def chat_blocked(self) -> tuple[bool, str]:
        """硬闸门：enforce 开启且当日对话 token 达到预算时返回 (True, 用户提示)。"""
        if not (self.enforce and self.daily_tokens > 0):
            return False, ""
        day = self._day(date.today())
        used = day["prompt"] + day["completion"]
        if used >= self.daily_tokens:
            return True, (
                f"（今日 LLM 预算已用完：{used:,}/{self.daily_tokens:,} tokens，服务明日自动恢复。）"
            )
        return False, ""

    def estimate_cost(self) -> float | None:
        """按配置的单价（元/百万 token）估算当日对话成本；未配置单价返回 None。"""
        if self.price_prompt is None and self.price_completion is None:
            return None
        day = self._day(date.today())
        cost = 0.0
        if self.price_prompt is not None:
            cost += day["prompt"] / 1e6 * self.price_prompt
        if self.price_completion is not None:
            cost += day["completion"] / 1e6 * self.price_completion
        return cost


_default = CostBudget()


def get_budget() -> CostBudget:
    return _default


def record_chat_usage(usage: dict | None) -> None:
    """记录一次对话补全的 usage；任何失败都不影响主流程。"""
    if not usage:
        return
    try:
        _default.record(
            "chat",
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )
    except Exception:
        logger.warning("budget: record chat usage failed", exc_info=True)


def record_embedding_usage(usage: dict | None) -> None:
    """记录一次 embedding 调用的 usage（total_tokens 记入 embedding_tokens）。"""
    if not usage:
        return
    try:
        _default.record(
            "embedding",
            prompt_tokens=int(usage.get("total_tokens") or usage.get("prompt_tokens") or 0),
        )
    except Exception:
        logger.warning("budget: record embedding usage failed", exc_info=True)
