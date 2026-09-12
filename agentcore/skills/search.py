"""Search skill：支持博查（Bocha）/ Tavily，配置项来自 .env。"""

from __future__ import annotations

import asyncio
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
        _state = _SearchState(
            cfg=_load_search_config(), client=httpx.AsyncClient(timeout=15)
        )
    return _state


async def aclose_search_client() -> None:
    """关闭并置空常驻搜索 httpx 连接池（停机时调用；幂等，重复调用不抛错）。"""
    global _state
    if _state is None:
        return
    state, _state = _state, None
    try:
        await state.client.aclose()
    except Exception:
        logger.exception("aclose search client failed")


async def search_web(query: str, max_results: int | None = None) -> str:
    state = get_search_client()
    cfg = state.cfg
    if not cfg.api_key:
        return "搜索未配置：请在 .env 中设置 SEARCH_API_KEY。"

    max_results = max_results if max_results is not None else cfg.max_results
    if cfg.provider == "tavily":
        return await _search_tavily(cfg, state.client, query, max_results)
    return await _search_bocha(cfg, state.client, query, max_results)


async def _search_bocha(
    cfg: SearchConfig, client: httpx.AsyncClient, query: str, max_results: int
) -> str:
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

    return _clip_results([_fmt_item(i) for i in _items_bocha(data)])


async def _search_tavily(
    cfg: SearchConfig, client: httpx.AsyncClient, query: str, max_results: int
) -> str:
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

    return _clip_results([_fmt_item(i) for i in _items_tavily(data)])


def _items_bocha(data: dict) -> list[dict]:
    out = []
    for item in (data.get("data") or {}).get("web_pages") or []:
        out.append(
            {
                "title": str(item.get("name") or ""),
                "url": str(item.get("url") or ""),
                "snippet": str(item.get("summary") or item.get("snippet") or ""),
            }
        )
    return out


def _items_tavily(data: dict) -> list[dict]:
    out = []
    for item in data.get("results") or []:
        out.append(
            {
                "title": str(item.get("title") or ""),
                "url": str(item.get("url") or ""),
                "snippet": str(item.get("content") or ""),
            }
        )
    return out


def _fmt_item(item: dict) -> str:
    return f"- {item.get('title')}: {item.get('url')}\n  {item.get('snippet') or ''}"


async def search_items(query: str, max_results: int | None = None) -> list[dict]:
    """结构化搜索结果：[{title,url,snippet}]；未配置或失败返回 []。"""
    cfg = get_search_client().cfg
    if not cfg.api_key:
        return []
    client = get_search_client().client
    n = int(max_results or cfg.max_results)
    try:
        if cfg.provider == "tavily":
            resp = await client.post(
                "https://api.tavily.com/search",
                headers={"Content-Type": "application/json"},
                json={
                    "api_key": cfg.api_key,
                    "query": query,
                    "max_results": n,
                    "search_lang": cfg.lang,
                },
            )
        else:
            resp = await client.post(
                "https://api.bocha.ai/v1/web-search",
                headers={
                    "Authorization": f"Bearer {cfg.api_key}",
                    "Content-Type": "application/json",
                },
                json={"query": query, "count": n, "lang": cfg.lang},
            )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        logger.exception("search_items failed: %s", query[:60])
        return []
    if not isinstance(data, dict):
        return []
    items = _items_tavily(data) if cfg.provider == "tavily" else _items_bocha(data)
    return [i for i in items if i["title"] or i["url"]]


async def search_multi(
    queries: list[str], per_query: int = 3, max_total: int = 6
) -> str:
    """多查询并行搜索并去重合并——适合一次问多个方面的问题。"""
    cfg = get_search_client().cfg
    if not cfg.api_key:
        return "搜索未配置：请在 .env 中设置 SEARCH_API_KEY。"
    cleaned = [str(q).strip() for q in (queries or []) if str(q).strip()][:3]
    if not cleaned:
        return "请提供至少一个查询词。"
    per_query = max(1, min(int(per_query or 3), 5))
    max_total = max(1, min(int(max_total or 6), 10))

    results = await asyncio.gather(
        *[search_items(q, per_query) for q in cleaned], return_exceptions=True
    )
    merged: list[dict] = []
    seen: set[str] = set()
    for items in results:
        if isinstance(items, Exception) or not items:
            continue
        for item in items:
            key = (item.get("url") or item.get("title") or "").strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(item)
    if not merged:
        return "未找到相关结果。"
    header = f"多路搜索（{len(cleaned)} 个查询，去重后 {len(merged[:max_total])} 条）："
    return _clip_results([_fmt_item(i) for i in merged[:max_total]], header=header)


# 单条摘要与总结果的长度上限，防止搜索结果灌爆 LLM 上下文导致空回复
_MAX_ITEM_CHARS = 500
_MAX_TOTAL_CHARS = 8000
_TRUNC_NOTE = "\n…（结果过多/过长已截断）"


def _clip_results(raw: list[str], header: str = "") -> str:
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
    return f"{header}\n{body}" if header else body


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
            {
                "name": "max_results",
                "type": "integer",
                "description": f"最大结果数，默认 {state.cfg.max_results}",
            },
        ],
        permission="public",
    )
    return manifest, search_web
