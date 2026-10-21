"""Search skill：支持博查（Bocha）/ Tavily，配置项来自 .env。"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@dataclass
class SearchConfig:
    provider: str = "bocha"
    api_key: str = ""
    base_url: str = ""
    max_results: int = 5
    lang: str = "zh-CN"


@dataclass
class _SearchState:
    cfg: SearchConfig
    client: httpx.AsyncClient


_state: _SearchState | None = None


def _env_max_results() -> int:
    """SEARCH_MAX_RESULTS 脏值告警回退 + 夹紧 [1, 20]（search_multi 同款口径）。"""
    raw = (os.getenv("SEARCH_MAX_RESULTS") or "").strip()
    try:
        value = int(raw) if raw else 5
    except ValueError:
        logger.warning("SEARCH_MAX_RESULTS=%r 不是整数，回退默认 5", raw)
        return 5
    return max(1, min(value, 20))


def search_enabled() -> bool:
    """``SEARCH_ENABLED``：调用时读取，关掉即时生效（不读 key、不发请求）。"""
    raw = (os.getenv("SEARCH_ENABLED") or "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _load_search_config() -> SearchConfig:
    provider = (os.getenv("SEARCH_PROVIDER") or "bocha").strip().lower()
    if provider not in {"bocha", "tavily"}:
        provider = "bocha"
    return SearchConfig(
        provider=provider,
        api_key=(os.getenv("SEARCH_API_KEY") or "").strip(),
        base_url=(os.getenv("SEARCH_BASE_URL") or "").strip().rstrip("/"),
        max_results=_env_max_results(),
        lang=(os.getenv("SEARCH_LANG") or "zh-CN").strip(),
    )


def _load_providers() -> list:
    """加载搜索源列表：优先 search-providers.json（多源），缺失/损坏回落 env 单配置。"""
    from agentcore.skills import search_providers as sp

    try:
        return sp.load().enabled()
    except FileNotFoundError:
        return _env_single_provider()
    except sp.SearchProviderConfigError:
        logger.warning("search-providers.json 损坏，回落 env 单配置", exc_info=True)
        return _env_single_provider()


def _env_single_provider() -> list:
    """env 单配置（SEARCH_PROVIDER/API_KEY/BASE_URL）作为一个源——兼容未迁移。"""
    from agentcore.skills.search_providers import SearchProvider

    cfg = _load_search_config()
    if not cfg.api_key:
        return []
    return [
        SearchProvider(
            id=cfg.provider,
            name=cfg.provider,
            format=cfg.provider,
            base_url=cfg.base_url,
            api_key=cfg.api_key,
        )
    ]


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
    if not search_enabled():
        return "搜索功能已关闭（SEARCH_ENABLED=0）。"
    providers = _load_providers()
    if not providers:
        return "搜索未配置：请在 .env 中设置 SEARCH_API_KEY。"
    cfg = _load_search_config()
    n = max_results if max_results is not None else cfg.max_results
    client = get_search_client().client
    last_err: Exception | None = None
    for provider in providers:
        try:
            return await _search_one(provider, client, query, n, cfg.lang)
        except Exception as e:  # 一个源失败 → 切下一个（fallback 容错）
            last_err = e
            logger.warning("搜索源 %s 失败，切下一个：%s", provider.id, e)
    return f"所有搜索源均失败（{len(providers)} 个）：{last_err}"


async def _search_one(
    provider, client: httpx.AsyncClient, query: str, max_results: int, lang: str
) -> str:
    """按 provider.format 分派；失败抛异常由 search_web 捕获切下一个源。"""
    if provider.format == "tavily":
        return await _search_tavily(provider, client, query, max_results, lang)
    if provider.format == "stepfun":
        return await _search_stepfun(provider, client, query, max_results, lang)
    return await _search_bocha(provider, client, query, max_results, lang)


async def _search_bocha(
    provider, client: httpx.AsyncClient, query: str, max_results: int, lang: str
) -> str:
    payload = {
        "query": query,
        "count": max_results,
        "lang": lang,
    }
    resp = await client.post(
        provider.base_url or "https://api.bocha.ai/v1/web-search",
        headers={
            "Authorization": f"Bearer {provider.api_key}",
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
    provider, client: httpx.AsyncClient, query: str, max_results: int, lang: str
) -> str:
    payload = {
        "api_key": provider.api_key,
        "query": query,
        "max_results": max_results,
        "search_lang": lang,
    }
    resp = await client.post(
        provider.base_url or "https://api.tavily.com/search",
        headers={"Content-Type": "application/json"},
        json=payload,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        return "搜索服务返回异常。"

    return _clip_results([_fmt_item(i) for i in _items_tavily(data)])


async def _search_stepfun(
    provider, client: httpx.AsyncClient, query: str, max_results: int, lang: str
) -> str:
    """阶跃 StepFun 网页搜索（POST /v1/search）：Bearer 鉴权，请求 {query, n}。"""
    payload = {
        "query": query,
        "n": max_results,
    }
    resp = await client.post(
        provider.base_url or "https://api.stepfun.com/v1/search",
        headers={
            "Authorization": f"Bearer {provider.api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        return "搜索服务返回异常。"

    return _clip_results([_fmt_item(i) for i in _items_stepfun(data)])


def _items_stepfun(data: dict) -> list[dict]:
    out = []
    for item in data.get("results") or []:
        out.append(
            {
                "title": str(item.get("title") or ""),
                "url": str(item.get("url") or ""),
                "snippet": str(item.get("snippet") or item.get("content") or ""),
                "date": _norm_date(item.get("time")),
            }
        )
    return out


def _items_bocha(data: dict) -> list[dict]:
    out = []
    for item in (data.get("data") or {}).get("web_pages") or []:
        out.append(
            {
                "title": str(item.get("name") or ""),
                "url": str(item.get("url") or ""),
                "snippet": str(item.get("summary") or item.get("snippet") or ""),
                "date": _norm_date(item.get("date")),
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
                "date": _norm_date(item.get("published_date")),
            }
        )
    return out


def _norm_date(raw) -> str:
    """发布日期归一为 YYYY-MM-DD；缺失/异常返回空串。

    时效性关键：模型内部知识有截止时间，生成的 query 常沿用旧年份（实测带
    「2025」搜回 2025 年的过时新闻）——结果里带上日期，模型才能核对并优先
    采用最新信息。
    """
    text = str(raw or "").strip()
    if len(text) >= 10 and text[:4].isdigit() and text[4] == "-":
        return text[:10]
    return ""


def _fmt_item(item: dict) -> str:
    date = str(item.get("date") or "")
    stamp = f"（{date}）" if date else ""
    return f"- {item.get('title')}{stamp}: {item.get('url')}\n  {item.get('snippet') or ''}"


async def search_items(query: str, max_results: int | None = None) -> list[dict]:
    """结构化搜索结果：[{title,url,snippet}]；未配置或全部失败返回 []。"""
    if not search_enabled():
        return []
    providers = _load_providers()
    if not providers:
        return []
    cfg = _load_search_config()
    try:
        n = int(max_results or cfg.max_results)
    except (TypeError, ValueError):
        n = cfg.max_results
    n = max(1, min(n, 20))
    client = get_search_client().client
    for provider in providers:
        try:
            return await _search_items_one(provider, client, query, n, cfg.lang)
        except Exception:
            logger.exception("search_items 源 %s 失败，切下一个", provider.id)
    return []


async def _search_items_one(
    provider, client: httpx.AsyncClient, query: str, max_results: int, lang: str
) -> list[dict]:
    if provider.format == "tavily":
        resp = await client.post(
            provider.base_url or "https://api.tavily.com/search",
            headers={"Content-Type": "application/json"},
            json={
                "api_key": provider.api_key,
                "query": query,
                "max_results": max_results,
                "search_lang": lang,
            },
        )
    elif provider.format == "stepfun":
        resp = await client.post(
            provider.base_url or "https://api.stepfun.com/v1/search",
            headers={
                "Authorization": f"Bearer {provider.api_key}",
                "Content-Type": "application/json",
            },
            json={"query": query, "n": max_results},
        )
    else:
        resp = await client.post(
            provider.base_url or "https://api.bocha.ai/v1/web-search",
            headers={
                "Authorization": f"Bearer {provider.api_key}",
                "Content-Type": "application/json",
            },
            json={"query": query, "count": max_results, "lang": lang},
        )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        return []
    if provider.format == "tavily":
        items = _items_tavily(data)
    elif provider.format == "stepfun":
        items = _items_stepfun(data)
    else:
        items = _items_bocha(data)
    return [i for i in items if i["title"] or i["url"]]


async def search_multi(
    queries: list[str], per_query: int = 3, max_total: int = 6
) -> str:
    """多查询并行搜索并去重合并——适合一次问多个方面的问题。"""
    if not search_enabled():
        return "搜索功能已关闭（SEARCH_ENABLED=0）。"
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
        description=(
            "联网搜索：输入查询词，返回搜索结果摘要与链接（含发布日期）。"
            "query 不要写死历史年份（除非用户明确问某一年），用「最新/近期」等"
            "相对表述；结果按发布日期核对时效，优先采用最新信息。"
        ),
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
