"""受控下载音频。

安全纪律与 ``plugins/qq_agent_adapter/media.py`` 同源，但**不能复用它的代码**——
那在 ``plugins/`` 里，而 ``agentcore/`` 不得反向依赖 ``plugins/``（分层铁律）。
所以这里自带一份：https only + 域名后缀白名单 + IP 私网/回环校验 + 重定向逐跳复检
+ 大小上限 + content-type 白名单。

为什么要这么严：音频地址来自音乐接口的响应，属于**外部可控数据**。它可能被换成
指向内网或云元数据服务的地址，于是"点歌"就成了一条 SSRF 通道。
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from pathlib import Path
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

DEFAULT_AUDIO_HOSTS = "music.126.net"
DEFAULT_MAX_DOWNLOAD_MB = 20
_MAX_REDIRECTS = 3
_TIMEOUT = 20.0
_CHUNK = 64 * 1024


def _env_int(name: str, default: int) -> int:
    import os

    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r 不是整数，回退默认 %d", name, raw, default)
        return default
    return value if value > 0 else default


def audio_hosts() -> list[str]:
    import os

    raw = (os.getenv("AGENT_MUSIC_AUDIO_HOSTS") or DEFAULT_AUDIO_HOSTS).strip()
    return [h.strip().lower() for h in raw.split(",") if h.strip()]


def _is_forbidden_ip(ip: str) -> bool:
    """内网/回环/链路本地/保留/组播/未指定一律不可访问。

    含 ``100.64.0.0/10``（CGNAT，``is_private`` 覆盖不到）——与
    ``workspace/runner.py`` 的判定保持一致。
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if addr.is_private or addr.is_loopback or addr.is_link_local:
        return True
    if addr.is_reserved or addr.is_multicast or addr.is_unspecified:
        return True
    # 100.64.0.0/10：运营商级 NAT，Python 的 is_private 不含这一段
    return addr.version == 4 and addr in ipaddress.ip_network("100.64.0.0/10")


async def _host_is_safe(host: str) -> bool:
    """解析 host 的全部地址，任一落在禁段即判不安全（防 DNS rebinding 打内网）。"""
    if not host:
        return False
    import asyncio

    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, host, None, proto=socket.SOCK_STREAM
        )
    except OSError as e:
        logger.warning("音频域名无法解析：%s（%s）", host, e.strerror or e)
        return False
    ips = {info[4][0] for info in infos if info[4]}
    if not ips:
        return False
    for ip in sorted(ips):
        if _is_forbidden_ip(ip):
            logger.warning("音频域名解析到禁段地址，拒绝：%s → %s", host, ip)
            return False
    return True


class UnsafeURLError(ValueError):
    """URL 未通过安全校验。"""


def _validate(url: str) -> None:
    """同步部分校验（scheme/host 白名单）。IP 校验是异步的，见 ``_host_is_safe``。"""
    sp = urlsplit(url)
    if sp.scheme != "https":
        raise UnsafeURLError(f"只允许 https（收到 {sp.scheme or '无 scheme'}）")
    host = (sp.hostname or "").lower()
    if not host:
        raise UnsafeURLError("URL 缺少主机名")
    allowed = audio_hosts()
    if not any(host == h or host.endswith(f".{h}") for h in allowed):
        raise UnsafeURLError(f"主机不在白名单：{host}")


async def fetch_audio(url: str, dest: Path) -> Path:
    """把音频下载到 ``dest``，通过校验后返回该路径。

    重定向**不自动跟随**：逐跳重新过 ``_validate`` + IP 校验，最多 3 跳。
    自动跟随会让"第一跳白名单内、第二跳跳去内网"这种绕过成立。
    """
    _validate(url)
    max_bytes = (
        _env_int("AGENT_MUSIC_MAX_DOWNLOAD_MB", DEFAULT_MAX_DOWNLOAD_MB) * 1024 * 1024
    )
    current = url
    dest.parent.mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
        for hop in range(_MAX_REDIRECTS + 1):
            sp = urlsplit(current)
            if not await _host_is_safe(sp.hostname or ""):
                raise UnsafeURLError(f"主机 IP 校验未通过：{sp.hostname}")

            async with client.stream("GET", current) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location") or ""
                    if not location:
                        raise UnsafeURLError("重定向缺少 location")
                    # 相对地址要按当前 URL 补齐，否则 urlsplit 出来没有 host
                    current = str(httpx.URL(current).join(location))
                    logger.info("音频下载重定向（第 %d 跳）", hop + 1)
                    _validate(current)
                    continue

                if resp.status_code != 200:
                    raise RuntimeError(f"音频下载失败：HTTP {resp.status_code}")

                ctype = (resp.headers.get("content-type") or "").split(";")[0].strip()
                if not ctype.startswith("audio/"):
                    raise UnsafeURLError(f"content-type 非音频：{ctype or '缺失'}")

                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise UnsafeURLError(
                        f"音频超过上限：{int(declared)} > {max_bytes} 字节"
                    )

                written = 0
                with open(dest, "wb") as fh:
                    async for chunk in resp.aiter_bytes(_CHUNK):
                        written += len(chunk)
                        if written > max_bytes:
                            raise UnsafeURLError(
                                f"音频流超过上限：>{max_bytes} 字节（已写 {written}）"
                            )
                        fh.write(chunk)

            if written == 0:
                raise RuntimeError("音频下载结果为空")
            logger.info("音频下载完成：%d 字节 → %s", written, dest)
            return dest

    raise UnsafeURLError(f"重定向超过 {_MAX_REDIRECTS} 跳，放弃")
