"""网页抓取 skill：SSRF 防护 + 不可信内容围栏。

安全边界（LLM 只能给 URL，不能给命令）：
- 仅允许 http/https
- 解析后的**所有** IP 都必须是公网地址：拒绝回环/私网/链路本地/保留段，
  否则 `http://192.168.1.1/`、`http://127.0.0.1:6086/`（内网 WebDAV）会被直接抓走
- 不自动跟随重定向：逐跳重新校验（防止跳到内网）
- 限超时、限响应体大小
- 抓回的正文按「不可信数据」围栏后再交给模型（网页是典型注入载体）

残留风险（如实披露）：IP 校验（url_rejection_reason）与实际连接（httpx）是
两次独立的 DNS 解析，存在 TOCTOU 窗口——校验通过后、连接发起前 DNS 记录
切换即可绕过校验打到内网（与 media.py 相同的残留）。彻底方案为钉住已校验
IP（自定义 transport）后再连接，待后续处理。
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
import urllib.parse as up

import httpx

from agentcore.safety import fence_untrusted

# RFC6598 共享地址空间（100.64.0.0/10）
_CGNAT_SHARED = ipaddress.ip_network("100.64.0.0/10")

logger = logging.getLogger(__name__)

_ALLOWED_SCHEMES = {"http", "https"}
# 透明代理 / fake-IP 模式（Clash 等）会把外网域名解析到 198.18.0.0/15 保留段。
# 这类地址只在本机代理内可路由，不构成对内网的访问，因此默认放行；置空则严格模式。
_DEFAULT_ALLOW_RANGES = "198.18.0.0/15"
_TIMEOUT = httpx.Timeout(15.0, connect=8.0)
_MAX_BYTES = 2 * 1024 * 1024
_MAX_REDIRECTS = 3
_MAX_OUTPUT_CHARS = 4000


def _proxy_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    raw = os.environ.get("AGENT_FETCH_ALLOW_RANGES")
    if raw is None:
        raw = _DEFAULT_ALLOW_RANGES
    nets = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            logger.warning("ignore invalid AGENT_FETCH_ALLOW_RANGES entry: %r", part)
    return nets


def _ip_is_reachable(ip: str) -> bool:
    """公网可达（或本机代理段）返回 True；内网/回环/保留段返回 False。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for net in _proxy_networks():
        if addr.version == net.version and addr in net:
            return True
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
        # M（REVIEW-a604023..679c9b3）：100.64.0.0/10（RFC6598 / Tailscale 默认段）
        # 在 CPython 里既非 private 也非 reserved，必须显式拒绝
        or addr in _CGNAT_SHARED
    )


async def url_rejection_reason(url: str) -> str | None:
    """URL 不可抓取时返回原因；允许时返回 None。"""
    parsed = up.urlsplit(url or "")
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return f"仅支持 http/https 链接（收到 {parsed.scheme or '空 scheme'}）"
    host = parsed.hostname
    if not host:
        return "链接缺少域名"
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo,
            host,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as e:
        return f"域名解析失败：{e}"
    ips = {info[4][0] for info in infos}
    if not ips:
        return "域名解析不到地址"
    bad = sorted(ip for ip in ips if not _ip_is_reachable(ip))
    if bad:
        # 内网/回环一律拒绝：这是 SSRF 的主要入口
        return f"目标解析到内网/保留地址（{', '.join(bad)}），已拒绝抓取"
    return None


async def fetch_page_raw(url: str) -> tuple[str | None, str]:
    """抓取并抽取正文，返回 (正文, 最终URL)；失败返回 (None, 原因说明)。"""
    reason = await url_rejection_reason(url)
    if reason:
        logger.warning("fetch_url rejected: %s (%s)", url[:120], reason)
        return None, f"拒绝抓取：{reason}"

    import trafilatura

    current = url
    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT, follow_redirects=False
        ) as client:
            for _hop in range(_MAX_REDIRECTS + 1):
                async with client.stream(
                    "GET", current, headers={"User-Agent": "Mozilla/5.0 (agent-demo)"}
                ) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("location") or ""
                        current = str(httpx.URL(current).join(loc))
                        hop_reason = await url_rejection_reason(current)
                        if hop_reason:
                            return None, f"拒绝抓取：跳转目标不安全（{hop_reason}）"
                        continue
                    if resp.status_code >= 400:
                        return None, f"抓取失败：HTTP {resp.status_code}"
                    size = 0
                    chunks: list[bytes] = []
                    async for chunk in resp.aiter_bytes(65536):
                        size += len(chunk)
                        if size > _MAX_BYTES:
                            return None, "抓取失败：页面超过 2MB 上限"
                        chunks.append(chunk)
                    body = b"".join(chunks)
                    html = body.decode(resp.encoding or "utf-8", errors="replace")
                    break
            else:
                return None, "抓取失败：重定向次数过多"
    except httpx.HTTPError as e:
        return None, f"抓取失败：{type(e).__name__} {e}"

    try:
        text = await asyncio.to_thread(trafilatura.extract, html, include_links=True)
    except Exception:
        logger.exception("trafilatura extract failed")
        text = None
    text = (text or "").strip()
    if not text:
        return None, f"该页面没有提取到正文（{current}）"
    return text, current


async def fetch_page_text(url: str) -> str:
    """抓取网页并返回**带不可信围栏**的正文，供模型直接阅读。"""
    text, info = await fetch_page_raw(url)
    if text is None:
        return info
    if len(text) > _MAX_OUTPUT_CHARS:
        text = (
            text[:_MAX_OUTPUT_CHARS] + f"\n…（正文过长，仅前 {_MAX_OUTPUT_CHARS} 字）"
        )
    return fence_untrusted("网页内容", f"来源：{info}\n\n{text}", "外部网站抓取")


def register_web_fetch_skill(registry) -> None:
    @registry.register(
        "fetch_url",
        "抓取网页正文。仅当用户明确给出具体网址时使用；不要用来自行猜测网址或"
        "抓热榜门户（反爬会返回 503/429）。内网/本机地址会被拒绝。",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "用户提供的具体网址"}
            },
            "required": ["url"],
        },
        permission="public",
    )
    async def fetch_url_skill(url: str) -> str:
        """fetch_url 的实现入口。

        残留风险（如实披露）：IP 校验与实际连接是两次独立 DNS 解析，存在
        DNS rebinding 的 TOCTOU 窗口（与 media.py 相同的残留）；彻底方案
        为钉住已校验 IP 后再连接。
        """
        return await fetch_page_text(url)
