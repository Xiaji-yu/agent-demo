"""成本预算（M7）：LLM/embedding 用量按日累计 + 可选硬闸门 + 成本估算。

用量来源是 OpenAI 风格响应里的 usage 字段（prompt_tokens / completion_tokens /
total_tokens），由 LLMClient 与 EmbeddingClient 在响应处理处上报本模块。数据按月
落盘到 ``data/budget/usage-YYYY-MM.json``（按天累计、原子写、重启不丢）。

- **只记录不限流**（默认）：`AGENT_BUDGET_DAILY_TOKENS=0` 或未设置
- **软预算**：设了每日 token 预算时，`/status` 展示用量与余量，首次到达打 WARNING
- **硬闸门**：再加 `AGENT_BUDGET_ENFORCE=1`，超限后新的聊天轮次（engine.run）与蒸馏
  （kb.digest）直接返回预算提示；对话进行中的 tool-loop **每一步也会复查并中止**
  （否则一轮最多还能再打 max_iterations 次 LLM）。次日按日键自动恢复
- **成本估算**：配置单价（元/百万 token）后 `/status` 可展示当日估算成本
"""
from __future__ import annotations

import json
import logging
import math
import os
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

_TRUE = {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    """整数环境变量：脏值/负值一律告警后回退，绝不静默（评审 REVIEW-bbd8913..f6dffcc.md 的 M2）。

    此前 ``int(os.getenv(...))`` 对 ``1,000,000`` / ``1e6`` 这类写法静默回退 0，
    等于把硬闸门无声关掉，且 ``/status`` 也不再显示预算行，运维无法察觉。
    """
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            "budget: invalid %s=%r, falling back to %d（注意 1,000,000 / 1e6 这类写法不会被识别）",
            name,
            raw,
            default,
        )
        return default
    if value < 0:
        logger.warning("budget: negative %s=%s, falling back to %d", name, value, default)
        return default
    return value


def _env_float(name: str) -> float | None:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        logger.warning("budget: invalid %s=%r, ignored", name, raw)
        return None
    # nan/inf/负数会让 /status 显示 "≈ nan 元" 或产生无意义成本（评审 L11）
    if not math.isfinite(value) or value < 0:
        logger.warning("budget: invalid %s=%r（须为有限的非负数）, ignored", name, raw)
        return None
    return value


def _blank_day() -> dict:
    """一天账本的完整键集；用于新建与补齐缺失键（评审 M4）。"""
    return {
        "prompt": 0,
        "completion": 0,
        "embedding_tokens": 0,
        "chat_requests": 0,
        "embedding_requests": 0,
    }


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
        """加载月份账本；对结构异常做**显式校验**而不是抛给下游（评审 M4）。

        此前 `json.loads(...).get("days", {})` 只挡 JSON 语法错：`{"days": null}` 会让
        `chat_blocked()` 抛 AttributeError（engine.run 顶部未捕获 → 对话静默降级为 echo，
        且闸门失效）；日条目缺键则 KeyError。现在只接受形状正确的日条目。
        """
        if self._loaded_month == month:
            return
        path = self._month_file(month)
        days: dict = {}
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                candidate = raw.get("days") if isinstance(raw, dict) else None
                if isinstance(candidate, dict):
                    days = {k: v for k, v in candidate.items() if isinstance(v, dict)}
                    if len(days) != len(candidate):
                        logger.warning("budget: %s 含非字典日条目，已忽略", path)
                else:
                    logger.warning(
                        "budget: %s 的 days 不是字典（%s），按空账本处理",
                        path,
                        type(candidate).__name__,
                    )
            except Exception:
                logger.warning("budget: corrupt usage file %s, starting fresh", path)
        self._days = days
        self._loaded_month = month

    def _save(self, month: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._month_file(month)
        # 临时名带 pid：多进程/多实例并发时不再互踩同一个 .part（评审 L9/M3）
        tmp = path.with_name(f"{path.name}.{os.getpid()}.part")
        tmp.write_text(json.dumps({"days": self._days}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def _day(self, now: date) -> dict:
        self._ensure_loaded(now.strftime("%Y-%m"))
        key = now.isoformat()
        day = self._days.get(key)
        if not isinstance(day, dict):
            day = self._days[key] = _blank_day()
        else:
            # 补齐缺失/类型错误的键，避免 today()/chat_blocked() 抛 KeyError
            for k, v in _blank_day().items():
                if not isinstance(day.get(k), int) or isinstance(day.get(k), bool):
                    day[k] = v
        return day

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
        """硬闸门：enforce 开启且当日对话 token 达到预算时返回 (True, 用户提示)。

        提示文案**不含**具体 token 数字（评审 L12）：这条文本会直接发给任何群成员，
        内部用量/预算档位只应出现在管理员可见的 ``/status`` 与管理日志里。
        """
        if not (self.enforce and self.daily_tokens > 0):
            return False, ""
        day = self._day(date.today())
        used = day["prompt"] + day["completion"]
        if used >= self.daily_tokens:
            logger.info(
                "budget: chat blocked by hard gate (%d/%d tokens)", used, self.daily_tokens
            )
            return True, "（今日 LLM 预算已用完，服务明日自动恢复。）"
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
