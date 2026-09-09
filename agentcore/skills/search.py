"""Search skill：支持博查（Bocha）/ Tavily，配置项来自 .env。"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

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


_state: Optional[_SearchState] = None


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


async def search_web(query: str, max_results: Optional[int] = None) -> str:
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

    results = []
    for item in (data.get("data") or {}).get("web_pages") or []:
        results.append(f"- {item.get('name')}: {item.get('url')}\n  {item.get('summary') or item.get('snippet') or ''}")
    if not results:
        return "未找到相关结果。"
    return "\n".join(results)


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

    results = []
    for item in data.get("results") or []:
        results.append(f"- {item.get('title')}: {item.get('url')}\n  {item.get('content') or ''}")
    if not results:
        return "未找到相关结果。"
    return "\n".join(results)


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
