"""MediaWiki 在线直查 skill（RAG-on-the-fly：不建本地向量库）。

移植自 dsh-web/prts_kb 项目里已验证的 wiki_lookup 层：问题 → 关键词 →
MediaWiki API 三层降级检索（精确标题批量命中 / 整句搜索 / 逐词合并计分）→
取回 wikitext → 模板渲染转 Markdown → 按预算挑小节截取 → 带来源 URL 返回。

与向量知识库（agentcore/rag）的关系：互为补充。本模块**不落库**——
零 embedding 成本、页面永远跟随站点最新版本；代价是每次查询多几秒网络延迟。
同步落库走 scripts/sync_wiki_subset.py（精选子集，kind="wiki"）。

安全边界：
- wiki 正文是不可信外部文本（提示注入载体）。技能名固定为 ``wiki_<site>``，
  引擎对它们按 _UNTRUSTED_TOOL_RESULTS 同款机制围栏（engine.py 经
  :func:`is_wiki_skill_name` 判定，站点表扩展后自动覆盖）。
- API 地址只来自本模块 SITES 配置（代码内置），不接受模型/用户传入 URL，
  无 SSRF 面。
- 礼仪：每站点全局限速（BWIKI 是共享平台，更客气）、429/5xx 尊重
  Retry-After 指数退避、自定义 User-Agent。

异步实现注意（与 dsh-web 同步版的差异）：限速等待用 ``await asyncio.sleep``，
绝不能用同步 ``time.sleep`` 阻塞事件循环；wikitext 模板渲染是 CPU 密集，
调用方必须经 ``asyncio.to_thread`` 执行。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import urllib.parse
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

try:  # 纯 Python 依赖；缺失时降级为正则剥模板（丢模板内容，但不崩）
    import wikitextparser as wtp
except ImportError:  # pragma: no cover - pyproject 已 pin，防御性兜底
    wtp = None

# ---------------------------------------------------------------- 站点表 ----
# 内置站点：加新 MediaWiki 站点 = 在这里加一项（api/base/name/min_interval），
# skill（wiki_<site>）与同步脚本（scripts/sync_wiki_subset.py）自动可用。
SITES: dict[str, dict] = {
    "prts": {
        "api": "https://prts.wiki/api.php",
        "base": "https://prts.wiki/w/",
        "name": "PRTS Wiki（明日方舟）",
        "min_interval": 0.6,  # 全局最小请求间隔（秒）
    },
    "blhx": {
        "api": "https://wiki.biligame.com/blhx/api.php",
        "base": "https://wiki.biligame.com/blhx/",
        "name": "BWIKI 碧蓝航线",
        "min_interval": 1.2,  # biligame 是多游戏共享平台，限速更客气
    },
}

WIKI_SITES_ENV = "AGENT_WIKI_SITES"
WIKI_BUDGET_ENV = "AGENT_WIKI_BUDGET"
DEFAULT_BUDGET = 9000  # 单次查询返回正文的最大字符数

UA = "qq-agent-wiki-lookup/1.0 (personal bot; polite; cache+ratelimit)"
TIMEOUT = 30.0
MAX_RETRY = 4
BACKOFF_BASE = 2.0  # 重试退避基数（秒），指数增长封顶 BACKOFF_MAX
BACKOFF_MAX = 30.0

_CACHE_TTL = 24 * 3600  # (站点,页面) 缓存 24h：同一问题短时间重复查询零请求
_CACHE_MAX = 256

# wikitext 渲染前的硬上限：MediaWiki 正文页远小于此；防御性限制输入规模，
# 避免异常大输入拖垮 to_thread（AGENTS.md §5：静态安全计算必须限制输入规模）
_MAX_WIKITEXT_CHARS = 2_000_000


def skill_name(site: str) -> str:
    """站点 id → 技能名（引擎围栏判定与注册共用此映射）。"""
    return f"wiki_{site}"


def is_wiki_skill_name(name: str) -> bool:
    """是否为本模块注册的 wiki 直查技能（含未启用的站点，拒绝要 fail-closed）。"""
    return name in {skill_name(s) for s in SITES}


def enabled_sites() -> list[str]:
    """读 ``AGENT_WIKI_SITES``（逗号分隔站点 id）：缺省启用全部内置站点；
    ``none``（不区分大小写）全部关闭；未知 id 告警忽略，绝不静默。"""
    raw = (os.getenv(WIKI_SITES_ENV) or "").strip()
    if not raw:
        return list(SITES)
    if raw.lower() == "none":
        return []
    out: list[str] = []
    for part in raw.split(","):
        sid = part.strip().lower()
        if not sid:
            continue
        if sid not in SITES:
            logger.warning(
                "%s: 未知站点 %r（内置：%s），忽略", WIKI_SITES_ENV, sid, sorted(SITES)
            )
            continue
        if sid not in out:
            out.append(sid)
    return out


def budget_chars() -> int:
    """单次查询返回正文的字符预算：脏值/非正值告警回退默认，夹紧 [1000, 30000]。"""
    raw = (os.getenv(WIKI_BUDGET_ENV) or "").strip()
    default = DEFAULT_BUDGET
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r 不是整数，回退默认 %d", WIKI_BUDGET_ENV, raw, default)
        return default
    if value <= 0:
        logger.warning(
            "%s=%s 非法（须 > 0），回退默认 %d", WIKI_BUDGET_ENV, value, default
        )
        return default
    return max(1000, min(value, 30000))


# --------------------------------------------------------- 连接池与限速 ----


@dataclass
class _WikiState:
    client: httpx.AsyncClient


_state: _WikiState | None = None


def get_wiki_client() -> httpx.AsyncClient:
    global _state
    if _state is None:
        _state = _WikiState(
            client=httpx.AsyncClient(
                timeout=httpx.Timeout(TIMEOUT, connect=10.0),
                headers={"User-Agent": UA},
            )
        )
    return _state.client


async def aclose_wiki_client() -> None:
    """停机回收常驻 httpx 连接池（与 aclose_search_client 同型；幂等）。"""
    global _state
    if _state is None:
        return
    state, _state = _state, None
    try:
        await state.client.aclose()
    except Exception:
        logger.exception("aclose wiki client failed")


_throttle_locks: dict[str, asyncio.Lock] = {}
_last_hit: dict[str, float] = {}


async def _throttle(site: str) -> None:
    """每站点全局限速：令牌间隔内的等待必须 sleep 在事件循环上，不许阻塞。"""
    interval = float(SITES[site].get("min_interval") or 0.0)
    lock = _throttle_locks.setdefault(site, asyncio.Lock())
    async with lock:
        wait = interval - (time.monotonic() - _last_hit.get(site, 0.0))
        if wait > 0:
            await asyncio.sleep(wait)
        _last_hit[site] = time.monotonic()


async def _api_get(site: str, params: dict) -> dict:
    """带限速 + 重试/退避的 MediaWiki API GET。最终失败抛 RuntimeError。"""
    client = get_wiki_client()
    url = SITES[site]["api"]
    backoff = BACKOFF_BASE
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRY + 1):
        await _throttle(site)
        try:
            resp = await client.get(url, params=params)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = backoff
                raw = (resp.headers.get("Retry-After") or "").strip()
                if raw.isdigit():
                    wait = max(0.0, float(raw))
                logger.warning(
                    "wiki %s: HTTP %d，重试 %d/%d 等 %.0fs",
                    site,
                    resp.status_code,
                    attempt,
                    MAX_RETRY,
                    wait,
                )
                await asyncio.sleep(wait)
                backoff = min(backoff * 2, BACKOFF_MAX)
                continue
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            last_exc = exc
            if attempt >= MAX_RETRY:
                break
            logger.warning(
                "wiki %s: 请求异常 %r，重试 %d/%d 等 %.0fs",
                site,
                exc,
                attempt,
                MAX_RETRY,
                backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX)
    raise RuntimeError(f"wiki {site} API 重试 {MAX_RETRY} 次仍失败：{last_exc!r}")


# ----------------------------------------------------------------- 缓存 ----
# (站点, 标题) -> (ts, markdown)。读写都是同步操作（无 await 夹在里面），
# 单事件循环内天然串行，无需加锁。

_page_cache: OrderedDict[tuple[str, str], tuple[float, str]] = OrderedDict()


def _cache_get(key: tuple[str, str]) -> str | None:
    item = _page_cache.get(key)
    if item is None:
        return None
    ts, md = item
    if time.time() - ts > _CACHE_TTL:
        del _page_cache[key]
        return None
    _page_cache.move_to_end(key)
    return md


def _cache_put(key: tuple[str, str], md: str) -> None:
    _page_cache[key] = (time.time(), md)
    _page_cache.move_to_end(key)
    while len(_page_cache) > _CACHE_MAX:
        _page_cache.popitem(last=False)


def clear_page_cache() -> None:
    """清空页面缓存（测试用；生产无需调用）。"""
    _page_cache.clear()


# ------------------------------------------------------ wikitext → Markdown ----
# 移植自 dsh-web/prts_kb.py，逐行保持行为一致（模板特判、链接/标题/标签清理）。

_MAGIC = {
    "FULLPAGENAME": None,
    "PAGENAME": None,
    "BASEPAGENAME": None,
    "SITENAME": None,
}


def clean_ws(s: str) -> str:
    s = s.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    return re.sub(r"\s+", " ", s).strip()


def _render_one(t, title: str) -> str:
    name = clean_ws(str(t.name))
    pos: list[str] = []
    named: list[tuple[str, str]] = []
    for a in t.arguments:
        if a.positional:
            pos.append(a.value)
        else:
            named.append((clean_ws(a.name), a.value))

    def P(i: int) -> str:
        return pos[i].strip() if i < len(pos) else ""

    # ---- 游戏 wiki 常用模板特判：提取人类可读的核心内容（PRTS 实测覆盖面）----
    lname = name.lower()
    if lname in (
        "color",
        "color2",
        "coloredlink",
        "术语",
        "term",
        "akterm",
        "修正",
        "note",
        "变动数值lite",
        "变动数值",
        "+",
        "*",
    ):
        return clean_ws(P(1) or P(0))
    if lname in ("材料消耗", "掉落详情"):
        a, b = P(0), P(1)
        return f"{a}×{b}" if a and b else a or b
    if lname == "cbox2":
        return clean_ws(P(1) or P(0))
    if lname in ("fa", "nbsp"):
        return ""
    if name in _MAGIC:
        v = _MAGIC[name]
        return v if v else (title or "")
    if name.startswith(":"):  # {{:页面/子页}} 嵌入 transclusion
        return f"(参见页面:{name[1:].strip()})"

    if named:
        lines = [f"【{name}】"]
        for k, v in named:
            lines.append(f"- {k}: {v.strip()}")
        for v in pos:
            lines.append(f"- {v.strip()}")
        return "\n".join(lines)
    if pos:
        return f"【{name}】" + "，".join(p.strip() for p in pos if p.strip())
    return f"【{name}】" if len(name) <= 12 else name


def _render_template_str(src: str, title: str) -> str:
    """迭代渲染最内层模板，直到没有 {{ }} 模板为止。CPU 密集：调用方须 to_thread。"""
    if wtp is None:
        # 降级：正则剥最内层模板（丢内容但不崩）；pyproject 已 pin，正常不走到这
        for _ in range(5):
            new = re.sub(r"\{\{([^{}]*)\}\}", "", src)
            if new == src:
                break
            src = new
        return src
    for _ in range(80):
        code = wtp.parse(src)
        tpls = code.templates
        if not tpls:
            break
        spans = [(t.span, t) for t in tpls]
        # 最内层：span 不包含其他模板的 span
        innermost = []
        for (s, e), t in spans:
            if any(s2 > s and e2 < e for (s2, e2), _ in spans):
                continue
            innermost.append(((s, e), t))
        if not innermost:
            innermost = spans
        # 从后往前替换，保持前面的 span 有效
        for (s, e), t in sorted(innermost, key=lambda x: -x[0][0]):
            src = src[:s] + _render_one(t, title) + src[e:]
    # 兜底：清掉残余大括号
    for _ in range(5):
        new = re.sub(r"\{\{([^{}]*)\}\}", r"\1", src)
        if new == src:
            break
        src = new
    return src


def render_wikitext(text: str, title: str) -> str:
    """wikitext → Markdown（同步、CPU 密集；调用方须 asyncio.to_thread 包装）。"""
    if not text:
        return ""
    if len(text) > _MAX_WIKITEXT_CHARS:
        logger.warning("wiki 页面 %s 过大（%d 字符），截断渲染", title, len(text))
        text = text[:_MAX_WIKITEXT_CHARS]
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)  # 注释
    text = re.sub(r"<section [^>]*>", "", text)
    text = re.sub(r"<references[^>]*/?>", "", text)
    text = text.replace("{{FULLPAGENAME}}", title or "")
    text = _render_template_str(text, title)

    # 链接 [[target|label]]（含后缀粘连，如 [[阿米娅]]s）
    def link_sub(m: re.Match) -> str:
        inner = m.group(1)
        if inner.startswith(":"):  # [[:分类:x]] 隐式链接
            inner = inner[1:]
        parts = inner.split("|")
        label = parts[1] if len(parts) > 1 else parts[0].split("#")[0]
        return label

    text = re.sub(
        r"\[\[([^\[\]|]+(?:\|[^\[\]|]+)?)\]\]([A-Za-z0-9]*)",
        lambda m: link_sub(m) + m.group(2),
        text,
    )
    text = re.sub(r"\[https?://\S+\s+([^\]]+)\]", r"\1", text)  # 外链
    text = re.sub(r"\[https?://\S+\]", "", text)

    # 字体样式
    text = re.sub(r"'''(.+?)'''", r"**\1**", text, flags=re.S)
    text = re.sub(r"''(.+?)''", r"*\1*", text, flags=re.S)

    # 标题: == x == -> ## x（页面标题占用 h1，故降一级）
    def head_sub(m: re.Match) -> str:
        lvl = len(m.group(1))
        return "\n" + "#" * min(lvl + 1, 6) + " " + m.group(2).strip() + "\n"

    text = re.sub(r"(?m)^(={2,6})\s*(.+?)\s*=+\s*$", head_sub, text)

    # 标签清理（保留内部文本）；wiki 表格原样保留（内容仍可检索）
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(
        r"</?(span|div|font|center|big|small|nowiki|poem|pre|group|tabber|gallery|ref)(\s[^>]*)?/?>",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(r"__(TOC|NOTOC|FORCETOC|NOEDITSECTION)__", "", text)

    for ent, ch in (
        ("&nbsp;", " "),
        ("&amp;", "&"),
        ("&lt;", "<"),
        ("&gt;", ">"),
        ("&quot;", '"'),
    ):
        text = text.replace(ent, ch)

    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return text.strip()


# ------------------------------------------------------------- 检索管线 ----

# 中文粗分词的停用词/疑问词分隔符（dsh-web 实测调优过的词表）
_SEPARATORS = [
    "是不是",
    "怎么样",
    "为什么",
    "做什么",
    "有哪些",
    "有没有",
    "是什么",
    "什么",
    "怎么",
    "如何",
    "哪里",
    "哪些",
    "多少",
    "需要",
    "想要",
    "告诉",
    "介绍",
    "请问",
    "一下",
    "可以",
    "吗",
    "呢",
    "吧",
    "啊",
    "的",
    "了",
    "是",
    "查",
    "看",
    "找",
    "我",
    "你",
    "请",
    "帮",
    "想",
    "要",
    "和",
    "与",
    "跟",
    "对",
    "在",
    "有",
    "及",
    "或",
    "里",
    "中",
]


def extract_keywords(question: str) -> str:
    """问题 → 搜索词兜底（无 LLM 改写时用）。"""
    s = question or ""
    for w in sorted(_SEPARATORS, key=len, reverse=True):
        s = s.replace(w, " ")
    chunks = [c.strip() for c in s.split() if len(c.strip()) >= 2]
    out = " ".join(dict.fromkeys(chunks)).strip()
    return (out or question or "")[:80]


async def wiki_search(site: str, keywords: str, limit: int = 5) -> list[dict]:
    """list=search，返回 [{title, snippet}]。"""
    data = await _api_get(
        site,
        {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "list": "search",
            "srsearch": keywords,
            "srlimit": str(limit),
            "srnamespace": "0",
            "srprop": "snippet",
        },
    )
    hits = ((data.get("query") or {}).get("search")) or []
    return [
        {"title": h["title"], "snippet": re.sub(r"<[^>]+>", "", h.get("snippet", ""))}
        for h in hits
    ]


# 资深编辑者的数据/sandbox 子页对问答是噪音（抓回来全是模板代码）。
# 第二个分支：末段**以 sandbox 结尾**的页面（如「假日威龙陈/天赋sandbox」，
# PRTS 搜索实测会出现——线上冒烟 2026-10-10），不要求紧贴斜杠。
_NOISE_RE = re.compile(
    r"/(?:spine|data|sandbox|styles\.css|styles\.js|token)$|(?:^|/)[^/]*sandbox$|/回响$",
    re.I,
)


async def find_pages(site: str, kw: str, top: int = 3) -> list[dict]:
    """三层降级检索：精确标题 → 整句 search → 逐词合并计分。

    老站 MySQL 全文检索对中文多词 AND 很烂，标题直查最可靠——所以精确命中
    永远排最前。返回 [{title, snippet}]，噪音子页被过滤。
    """
    results: list[dict] = []
    seen: set[str] = set()

    def push(title: str, snippet: str = "") -> None:
        if _NOISE_RE.search(title):
            return
        if title not in seen:
            seen.add(title)
            results.append({"title": title, "snippet": snippet})

    # --- 1. 精确标题（整句 + 分词块 + 滑窗子串，一次批量请求，上限 50 标题）---
    tokens = [t for t in kw.split() if len(t) >= 2]
    phrase = kw.replace(" ", "")
    candidates = [phrase, kw] + tokens
    for t in tokens:
        if len(t) >= 3:
            candidates += [
                t[i : i + n]
                for n in range(2, min(8, len(t)))
                for i in range(len(t) - n + 1)
            ]
    candidates = list(dict.fromkeys(c for c in candidates if len(c) >= 2))[:50]
    if candidates:
        try:
            data = await _api_get(
                site,
                {
                    "action": "query",
                    "format": "json",
                    "formatversion": "2",
                    "titles": "|".join(candidates),
                    "redirects": "1",
                },
            )
            prio = {t: i for i, t in enumerate(candidates)}
            found = [
                (prio.get(p["title"], 99), p["title"])
                for p in (data.get("query") or {}).get("pages") or []
                if "missing" not in p
            ]
            for _, title in sorted(found):
                push(title)
        except Exception:
            logger.warning("wiki %s: 精确标题批查失败，继续走搜索", site, exc_info=True)

    # --- 2. 整句搜索 ---
    for h in await wiki_search(site, kw, limit=top):
        push(h["title"], h["snippet"])

    # --- 3. 逐词搜索合并（按命中词数排序）---
    match_count: dict[str, int] = {}
    for t in tokens[:3]:
        for h in await wiki_search(site, t, limit=top):
            match_count[h["title"]] = match_count.get(h["title"], 0) + 1
    scored = sorted(match_count.items(), key=lambda x: (-x[1], x[0]))
    for title, _n in scored:
        push(title)

    return results[:top]


async def fetch_page_md(site: str, title: str) -> str:
    """取单页 wikitext → Markdown（带 LRU+TTL 缓存）。页面不存在抛 FileNotFoundError。"""
    key = (site, title)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    data = await _api_get(
        site,
        {
            "action": "query",
            "format": "json",
            "formatversion": "2",
            "prop": "revisions",
            "rvprop": "content",
            "rvslots": "main",
            "titles": title,
            "redirects": "1",
        },
    )
    pages = (data.get("query") or {}).get("pages") or []
    if not pages or "revisions" not in pages[0]:
        raise FileNotFoundError(f"页面不存在：{title}")
    content = pages[0]["revisions"][0]["slots"]["main"]["content"]
    md = await asyncio.to_thread(render_wikitext, content, title)
    _cache_put(key, md)
    return md


# ------------------------------------------------------ 按节截取（控长度）----

_HEAD_RE = re.compile(r"(?m)^(#{2,6})\s+(.+?)\s*$")


def _split_sections(md: str) -> list[tuple[str, str]]:
    """切成 [(heading, body)]。第一段 = 标题+导语（通常是信息框），无条件保留。"""
    m0 = _HEAD_RE.search(md)
    if not m0:
        return [("", md)]
    head = ("", md[: m0.start()]) if md[: m0.start()].strip() else ("", "")
    marks = [(m.start(), m.group(2)) for m in _HEAD_RE.finditer(md)]
    sections = [head] if head[1] else []
    for i, (pos, name) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(md)
        sections.append((name, md[pos:end]))
    return sections


def _tokens(text: str) -> set[str]:
    out = set(re.findall(r"[a-z0-9]+", text.lower()))
    for seg in re.findall(r"[\u4e00-\u9fff]+", text.lower()):
        out |= {seg[i : i + 2] for i in range(len(seg) - 1)} or {seg}
    return out


def trim_to_budget(md: str, keywords: str, budget: int = DEFAULT_BUDGET) -> str:
    """页面超长时：无条件保留开头信息框，其余小节按与关键词的重合度挑选。"""
    if budget <= 0 or len(md) <= budget:
        return md
    kw = _tokens(keywords)
    sections = _split_sections(md)
    head = sections[0]
    picked = [(0, head[1][:2800] + ("\n…(截断)" if len(head[1]) > 2800 else ""))]
    used = len(picked[0][1])

    scored = []
    for idx, (h, body) in enumerate(sections[1:], start=1):
        overlap = kw & _tokens(h + " " + body[:1500])
        if overlap:
            scored.append((idx, body, len(overlap)))
    scored.sort(key=lambda x: -x[2])
    for idx, body, _ in scored:
        if used >= budget:
            break
        cut = body[: max(800, budget - used)]
        picked.append((idx, cut + ("\n…(截断)" if len(cut) < len(body) else "")))
        used += len(cut)

    picked.sort(key=lambda x: x[0])
    return "\n\n".join(b for _, b in picked)


# ------------------------------------------------------------- 对外接口 ----


async def lookup(
    site: str,
    question: str,
    *,
    keywords: str | None = None,
    top: int = 3,
    budget: int | None = None,
) -> dict:
    """一步到位：问题 → {results, sources}；失败返回 ``{"error": ...}`` 不抛异常。

    results 元素：{title, url, text, snippet}，text 已清洗为 Markdown 并截取
    到 budget 内。sources 为去重后的来源 URL 列表。
    """
    if site not in SITES:
        return {"error": f"未知站点 {site!r}，可选：{sorted(SITES)}"}
    kw = (keywords or extract_keywords(question)).strip()
    if budget is None:
        budget = budget_chars()
    try:
        hits = await find_pages(site, kw, top=top + 2)
        if not hits:
            return {
                "site": site,
                "question": question,
                "keywords": kw,
                "results": [],
                "sources": [],
                "note": "搜索无结果。游戏名词建议用游戏内官方中文名再试一次。",
            }
        out: list[dict] = []
        urls: list[str] = []
        for h in hits[:top]:
            try:
                md = await fetch_page_md(site, h["title"])
            except FileNotFoundError:
                continue
            url = SITES[site]["base"] + urllib.parse.quote(h["title"], safe="")
            out.append(
                {
                    "title": h["title"],
                    "url": url,
                    "text": trim_to_budget(md, kw, budget),
                    "snippet": h.get("snippet", ""),
                }
            )
            urls.append(url)
        return {
            "site": site,
            "question": question,
            "keywords": kw,
            "results": out,
            "sources": urls,
        }
    except Exception as exc:
        logger.warning("wiki lookup %s failed: %s", site, exc)
        return {
            "site": site,
            "question": question,
            "keywords": kw,
            "error": f"{type(exc).__name__}: {exc}",
        }


def format_lookup_result(r: dict) -> str:
    """lookup 结果 → 给模型读的纯文本（最终还会被引擎整体围栏）。"""
    if r.get("error"):
        return f"wiki 查询失败：{r['error']}（可稍后重试或换关键词）"
    results = r.get("results") or []
    if not results:
        return str(r.get("note") or "搜索无结果。")
    lines: list[str] = []
    for res in results:
        lines.append(f"### {res['title']}")
        lines.append(f"来源：{res['url']}")
        if res.get("snippet"):
            lines.append(f"> 摘要：{res['snippet']}")
        lines.append(str(res.get("text") or "").strip())
        lines.append("")
    lines.append("（内容来自社区 wiki，可能过期或有误；回答请注明来源页面名。）")
    return "\n".join(lines).strip()


def _clamp_top(value) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return 3
    return max(1, min(v, 5))


def _make_site_handler(site: str) -> Callable[..., Awaitable[str]]:
    async def handler(question: str, keywords: str = "", top: int = 3) -> str:
        r = await lookup(
            site,
            question or "",
            keywords=(keywords or "").strip() or None,
            top=_clamp_top(top),
        )
        return format_lookup_result(r)

    handler.__name__ = skill_name(site)
    return handler


def register_wiki_skills(registry) -> list[str]:
    """按启用站点注册 wiki_<site> 技能，返回注册的技能名列表。

    站点 id 固化进技能名（wiki_prts / wiki_blhx…）：schema 描述里直接写站点
    中文名，「明日方舟问题 → 调哪个工具」的映射不交给模型猜。
    """
    from agentcore.skills.manifest import SkillManifest

    names: list[str] = []
    for site in enabled_sites():
        cfg = SITES[site]
        manifest = SkillManifest(
            name=skill_name(site),
            description=(
                f"{cfg['name']} 在线查询：输入问题或页面名，返回 wiki 页面正文与"
                "来源链接。查该游戏的干员/角色数值、养成材料、关卡数据、剧情等"
                "资料时优先用它（比联网搜索准）。"
            ),
            type="tool",
            prompt="",
            parameters=[
                {
                    "name": "question",
                    "type": "string",
                    "description": "要查询的问题或页面名；游戏名词用游戏内官方中文名",
                    "required": True,
                },
                {
                    "name": "keywords",
                    "type": "string",
                    "description": (
                        "可选：把问题改写成的精简搜索词（2~4 个关键词，"
                        "如「浊心斯卡蒂 天赋」），给了就跳过粗分词"
                    ),
                },
                {
                    "name": "top",
                    "type": "integer",
                    "description": "返回页面数（1~5，默认 3）",
                },
            ],
            permission="public",
        )
        registry.install(manifest, handler=_make_site_handler(site))
        names.append(manifest.name)
    return names
