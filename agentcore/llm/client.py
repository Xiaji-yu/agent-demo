import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from agentcore.budget import record_chat_usage, record_embedding_usage

logger = logging.getLogger(__name__)


def _env_nonneg_float(name: str, default: float) -> float:
    """读一个**非负**浮点 env（0 合法）；脏值/负数告警后回退默认（不崩启动）。"""
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r 不是数字，回退 %.0f", name, raw, default)
        return default
    if value < 0:
        logger.warning("%s=%r 为负，回退 %.0f", name, raw, default)
        return default
    return value


def _env_flag(name: str, default: bool) -> bool:
    """读一个布尔 env；``0/false/no/off`` 为关，其余（含空值）为默认值。"""
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


class _LLMCfg:
    def __init__(self):
        self.base_url = os.getenv("LLM_BASE_URL", "https://api.stepfun.com/v1").rstrip(
            "/"
        )
        self.api_key = os.getenv("LLM_API_KEY", "")
        self.model = os.getenv("LLM_MODEL", "step-1-flash")
        # 空值/脏值回落默认而不是抛：本类是模块级单例（见文件末尾 _CFG），
        # 裸 int()/float() 遇到 "LLM_MAX_TOKENS="（.env 里留空是常见形态）
        # 会让 import 即 ValueError → bot 根本起不来（评审 M6，与 embedding/
        # pipeline 预算解析的 try/except 兜底同一模式）
        try:
            self.temperature = float(os.getenv("LLM_TEMPERATURE", "0.7"))
        except ValueError:
            self.temperature = 0.7
        try:
            self.max_tokens = int(os.getenv("LLM_MAX_TOKENS", "1024"))
        except ValueError:
            self.max_tokens = 1024
        self.fallback_base_url = os.getenv("LLM_FALLBACK_BASE_URL", "").rstrip("/")
        self.fallback_api_key = os.getenv("LLM_FALLBACK_API_KEY", "")
        self.fallback_model = os.getenv("LLM_FALLBACK_MODEL", "")


_CFG = _LLMCfg()


class LLMClient:
    """直接从 .env 读取 LLM 配置，支持主备自动切换。

    主备切换是**逐请求无状态**的（每次先打主模型，失败才临时切备用），所以
    "当前在用哪个"要由本类自己记（``_using_fallback``）：它只用于识别**恢复**
    这一边沿（主路径再次成功时通知一次），降级通知则按类型冷却周期发送。
    """

    def __init__(self, clock=time.monotonic):
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
        # 主备切换/恢复回调（由宿主注入，如私聊推送主人；agentcore 不认识 bot，
        # 与 embedding.on_error 同一注入模式）。事件是纯 dict（JSON 安全），
        # **不含** api_key/base_url 等配置秘密。
        self.on_fallback: Callable[[dict], Awaitable[None]] | None = None
        self._notify_enabled = _env_flag("LLM_FALLBACK_NOTIFY", True)
        # 同类事件的重复通知冷却（秒）：持续故障时每这么久最多提醒一次"还在
        # 降级"（调大到 86400 就近似"每次故障只提醒一次"）。
        self._notify_cooldown = _env_nonneg_float(
            "LLM_FALLBACK_NOTIFY_COOLDOWN", 1800.0
        )
        self._clock = clock
        # 按事件类型分别记冷却：切到备用后**恢复**通知要能立刻到主人（不能被子
        # 切换的冷却压掉），同时抖动时同一类型最多每冷却期一条。
        # 哨兵用「键不存在」而不是 0.0：time.monotonic() 是开机秒数，刚重启的
        # 机器 now < cooldown 会让 now-0.0 < cooldown 恒成立 → 吞掉首次通知
        # （AGENTS.md §5 的坑，embedding 侧同款修复）。
        self._last_notify: dict[str, float] = {}
        self._using_fallback = False

    def _payload(
        self,
        cfg: _LLMCfg,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": cfg.model,
            "messages": messages,
            "temperature": cfg.temperature,
            "max_tokens": int(max_tokens) if max_tokens else cfg.max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return payload

    async def _post(self, cfg: _LLMCfg, payload: dict[str, Any]) -> dict:
        resp = await self._client.post(
            f"{cfg.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {cfg.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if resp.status_code >= 400:
            logger.error(
                "LLM API %s error: %s body=%s",
                resp.status_code,
                resp.url,
                resp.text[:800],
            )
        resp.raise_for_status()
        data = resp.json()
        # M7 成本预算：OpenAI 风格 usage 按日累计（主备两条路径都会经过这里）
        record_chat_usage(data.get("usage"), model=cfg.model)
        return data

    async def _notify_fallback(self, kind: str, **extra) -> None:
        """触发 on_fallback 回调（同类事件冷却内只发一次；首次必发）。

        冷却判定与置位之间**没有 await**：asyncio 单线程下并发请求只会有一个
        通过判定，不会出现"N 个请求同时失败就发 N 条通知"。
        """
        if not self._notify_enabled or self.on_fallback is None:
            return
        now = self._clock()
        last = self._last_notify.get(kind)
        if last is not None and now - last < self._notify_cooldown:
            return
        self._last_notify[kind] = now
        event = {
            "kind": kind,
            "primary_model": _CFG.model,
            "fallback_model": _CFG.fallback_model,
            "cooldown": self._notify_cooldown,
            **extra,
        }
        try:
            await self.on_fallback(event)
        except Exception:
            # 通知失败绝不能影响对话本身（主人收不到 QQ 不是用户犯的错）
            logger.warning("llm on_fallback callback failed: %s", kind, exc_info=True)

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_tokens: int | None = None,
    ) -> dict:
        """一次对话补全。max_tokens 可覆盖默认值——推理型模型会把预算耗在
        reasoning 上，输出 JSON/长文本时需要更大的上限（见 RAG 蒸馏）。"""
        payload = self._payload(_CFG, messages, tools, max_tokens)
        try:
            data = await self._post(_CFG, payload)
        except Exception as e:
            if not (
                _CFG.fallback_base_url and _CFG.fallback_api_key and _CFG.fallback_model
            ):
                # 没配备份：原样抛，宿主照旧走「LLM 调用失败」（不伪装成切换）
                raise
            logger.warning("primary LLM failed (%s), switching to fallback", e)
            fb = _LLMCfg()
            fb.base_url = _CFG.fallback_base_url
            fb.api_key = _CFG.fallback_api_key
            fb.model = _CFG.fallback_model
            fb.temperature = _CFG.temperature
            fb.max_tokens = _CFG.max_tokens
            data = await self._post(fb, self._payload(fb, messages, tools, max_tokens))
            self._using_fallback = True
            # 同类事件按冷却限流：持续故障时每 _notify_cooldown 最多提醒一次
            # ——主人需要知道"还在降级"，而不是只在故障第一分钟知道过一次。
            # （不搞"整个 episode 只报一次"：那与推送文案承诺的周期提醒不符，
            #  且长时间 Silent  outage 比周期性提醒更难排查。）
            await self._notify_fallback("switched", error=type(e).__name__)
            return data
        if self._using_fallback:
            # 主模型回来了：报一次「已恢复」，并清掉降级标记
            self._using_fallback = False
            await self._notify_fallback("recovered")
        return data

    async def embeddings(self, texts: list[str]) -> list[list[float]]:
        cfg = _CFG
        resp = await self._client.post(
            f"{cfg.base_url}/embeddings",
            headers={
                "Authorization": f"Bearer {cfg.api_key}",
                "Content-Type": "application/json",
            },
            json={"model": os.getenv("LLM_EMBEDDING_MODEL", cfg.model), "input": texts},
        )
        resp.raise_for_status()
        data = resp.json()
        record_embedding_usage(data.get("usage"))
        return [d["embedding"] for d in data.get("data", [])]

    async def aclose(self):
        await self._client.aclose()
