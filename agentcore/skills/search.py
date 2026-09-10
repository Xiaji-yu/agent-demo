"""Search skill：支持博查（Bocha）/ Tavily，配置项来自 .env。"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@dataclass
class SearchConfig:
    provider: str = "bocha"
    api_key: str = ""
    max_results: int = 5
    lang: str = "zh-CN"


@dataclass
class _SearchState:
    cfg: SearchConfig
    client: httpx.AsyncClient


_state: _SearchState | None = None


def _load_search_config() -> SearchConfig:
    import os

    provider = (os.getenv("SEARCH_PROVIDER") or "bocha").strip().lower()
    if provider not in {"bocha", "tavily"}:
        provider = "bocha"
    return SearchConfig(
        provider=provider,
        api_key=(os.getenv("SEARCH_API_KEY") or "").strip(),
        max_results=int(os.getenv("SEARCH_MAX_RESULTS", "5")),
        lang=(os.getenv("SEARCH_LANG") or "zh-CN").strip(),
    )


def get_search_client() -> _SearchState:
    global _state
    if _state is None:
        _state = _SearchState(cfg=_load_search_config(), client=httpx.AsyncClient(timeout=15))
    return _state


async def search_web(query: str, max_results: int | None = None) -> str:
    state = get_search_client()
    cfg = state.cfg
    if not cfg.api_key:
        return "搜索未配置：请在 .env 中设置 SEARCH_API_KEY。"

    max_results = max_results if max_results is not None else cfg.max_results
    if cfg.provider == "tavily":
        return await _search_tavily(cfg, state.client, query, max_results)
    return await _search_bocha(cfg, state.client, query, max_results)


async def _search_bocha(cfg: SearchConfig, client: httpx.AsyncClient, query: str, max_results: int) -> str:
    payload = {
        "query": query,
        "count": max_results,
        "lang": cfg.lang,
    }
    resp = await client.post(
        "https://api.bocha.ai/v1/web-search",
        headers={
            "Authorization": f"Bearer {cfg.api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        return "搜索服务返回异常。"

    raw = []
    for item in (data.get("data") or {}).get("web_pages") or []:
        raw.append(
            f"- {item.get('name')}: {item.get('url')}\n  "
            f"{item.get('summary') or item.get('snippet') or ''}"
        )
    return _clip_results(raw)


async def _search_tavily(cfg: SearchConfig, client: httpx.AsyncClient, query: str, max_results: int) -> str:
    payload = {
        "api_key": cfg.api_key,
        "query": query,
        "max_results": max_results,
        "search_lang": cfg.lang,
    }
    resp = await client.post(
        "https://api.tavily.com/search",
        headers={"Content-Type": "application/json"},
        json=payload,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        return "搜索服务返回异常。"

    raw = []
    for item in data.get("results") or []:
        raw.append(
            f"- {item.get('title')}: {item.get('url')}\n  {item.get('content') or ''}"
        )
    return _clip_results(raw)


# 单条摘要与总结果的长度上限，防止搜索结果灌爆 LLM 上下文导致空回复
_MAX_ITEM_CHARS = 500
_MAX_TOTAL_CHARS = 8000
_TRUNC_NOTE = "\n…（结果过多/过长已截断）"


def _clip_results(raw: list[str]) -> str:
    """限制单条与总长；截断只作用于摘要部分，尽量保留完整 URL，截断处有提示。"""
    if not raw:
        return "未找到相关结果。"
    clipped = []
    total = 0
    truncated_any = False
    for line in raw:
        if len(line) > _MAX_ITEM_CHARS:
            line = _clip_one(line, _MAX_ITEM_CHARS)
            truncated_any = True
        if total + len(line) + 1 > _MAX_TOTAL_CHARS:
            truncated_any = True
            break
        clipped.append(line)
        total += len(line) + 1
    if not clipped:
        return "未找到相关结果。"
    body = "\n".join(clipped)
    if truncated_any:
        body += _TRUNC_NOTE
    return body


def _clip_one(line: str, cap: int) -> str:
    """单行超长时优先只截摘要（title: url\n  摘要 结构），避免切断 URL。"""
    if len(line) <= cap:
        return line
    marker = "\n  "
    idx = line.find(marker)
    if idx != -1 and idx < cap:
        head = line[: idx + len(marker)]
        rest_budget = cap - len(head)
        if rest_budget <= 0:
            return line[:cap] + "…"
        return head + line[idx + len(marker) : idx + len(marker) + rest_budget] + "…"
    return line[:cap] + "…"


def create_search_skill():
    """创建搜索 skill 的 manifest + handler。"""
    from agentcore.skills.manifest import SkillManifest

    state = get_search_client()
    manifest = SkillManifest(
        name="search_web",
        description="联网搜索：输入查询词，返回搜索结果摘要与链接。",
        type="tool",
        prompt="",
        parameters=[
            {"name": "query", "type": "string", "description": "搜索查询词"},
            {"name": "max_results", "type": "integer", "description": f"最大结果数，默认 {state.cfg.max_results}"},
        ],
        permission="public",
    )
    return manifest, search_web
