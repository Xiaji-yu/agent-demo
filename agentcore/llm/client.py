import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class _LLMCfg:
    def __init__(self):
        self.base_url = os.getenv("LLM_BASE_URL", "https://api.stepfun.com/v1").rstrip("/")
        self.api_key = os.getenv("LLM_API_KEY", "")
        self.model = os.getenv("LLM_MODEL", "step-1-flash")
        self.temperature = float(os.getenv("LLM_TEMPERATURE", "0.7"))
        self.max_tokens = int(os.getenv("LLM_MAX_TOKENS", "1024"))
        self.fallback_base_url = os.getenv("LLM_FALLBACK_BASE_URL", "").rstrip("/")
        self.fallback_api_key = os.getenv("LLM_FALLBACK_API_KEY", "")
        self.fallback_model = os.getenv("LLM_FALLBACK_MODEL", "")


_CFG = _LLMCfg()


class LLMClient:
    """直接从 .env 读取 LLM 配置，支持主备自动切换。"""

    def __init__(self):
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))

    def _payload(self, cfg: _LLMCfg, messages: list[dict], tools: list[dict] | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": cfg.model,
            "messages": messages,
            "temperature": cfg.temperature,
            "max_tokens": cfg.max_tokens,
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
        return resp.json()

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> dict:
        payload = self._payload(_CFG, messages, tools)
        try:
            return await self._post(_CFG, payload)
        except Exception as e:
            if _CFG.fallback_base_url and _CFG.fallback_api_key and _CFG.fallback_model:
                logger.warning("primary LLM failed (%s), switching to fallback", e)
                fb = _LLMCfg()
                fb.base_url = _CFG.fallback_base_url
                fb.api_key = _CFG.fallback_api_key
                fb.model = _CFG.fallback_model
                fb.temperature = _CFG.temperature
                fb.max_tokens = _CFG.max_tokens
                return await self._post(fb, self._payload(fb, messages, tools))
            raise

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
        return [d["embedding"] for d in data.get("data", [])]

    async def aclose(self):
        await self._client.aclose()
