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


# 嗅探头部字节数：够覆盖 RIFF/WAVE 的 12 字节与 ftyp 的偏移 4..8
_MAGIC_PROBE = 16

# 确定为"不是音频"的 content-type：错误页/JSON 报错/纯文本。
# 其余（含 application/octet-stream 与缺失）一律**交给自己嗅探字节**判断。
_NOT_AUDIO_TYPES = frozenset(
    {
        "application/json",
        "application/problem+json",
        "application/xml",
        "application/xhtml+xml",
    }
)


def _is_definitely_not_audio(ctype: str) -> bool:
    if not ctype:
        return False  # 缺失不否决，交给魔数
    if ctype.startswith("text/"):
        return True
    return ctype in _NOT_AUDIO_TYPES


def _looks_like_audio(head: bytes) -> bool:
    """按容器魔数判断是否为音频（比信任 content-type 更可靠）。

    覆盖网易云实际会返回的格式：MP3（ID3 标签或帧同步）、FLAC、OGG、
    WAV(RIFF/WAVE)、M4A/MP4(ftyp)、以及裸 ADTS AAC。
    """
    if len(head) < 4:
        return False
    if head[:3] == b"ID3":
        return True
    # MPEG 帧同步（MP3 / ADTS AAC）：11 个 1
    if head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return True
    if head[:4] == b"fLaC":
        return True
    if head[:4] == b"OggS":
        return True
    if head[:4] == b"RIFF" and len(head) >= 12 and head[8:12] == b"WAVE":
        return True
    if head[4:8] == b"ftyp":  # M4A / MP4
        return True
    if head[:4] == b"\x1aE\xdf\xa3":  # Matroska / WebM
        return True
    return False


def _host_allowed(host: str) -> bool:
    allowed = audio_hosts()
    return any(host == h or host.endswith(f".{h}") for h in allowed)


def _upgrade_to_https(url: str) -> str:
    """白名单主机上的 ``http://`` 就地升级为 ``https://``。

    NeteaseCloudMusicApi 返回的音频地址实测是 ``http://m702.music.126.net/...``
    （自建实例默认如此），而**链路只允许 https** → 每首歌都会在下载这一步失败
    （报"音频地址不可用"）。实测同一 path+query 换成 https 后 CDN 返回
    200 且 content-type/length 完全一致，故这里做**协议升级**，而不是放宽到
    允许明文传输（后者会让音频内容可被中间人篡改）。

    非白名单主机不改写：仍由 :func:`_validate` 拒绝，错误信息保持清晰。
    """
    if not url.startswith("http://"):
        return url
    upgraded = "https://" + url[len("http://") :]
    host = (urlsplit(upgraded).hostname or "").lower()
    if _host_allowed(host):
        return upgraded
    return url


def _validate(url: str) -> None:
    """同步部分校验（scheme/host 白名单）。IP 校验是异步的，见 ``_host_is_safe``。"""
    sp = urlsplit(url)
    if sp.scheme != "https":
        raise UnsafeURLError(
            f"只允许 https（收到 {sp.scheme or '无 scheme'}）；"
            "http 地址仅在域名白名单内会被自动升级为 https"
        )
    host = (sp.hostname or "").lower()
    if not host:
        raise UnsafeURLError("URL 缺少主机名")
    if not _host_allowed(host):
        raise UnsafeURLError(f"主机不在白名单：{host}")


async def fetch_audio(url: str, dest: Path) -> Path:
    """把音频下载到 ``dest``，通过校验后返回该路径。

    重定向**不自动跟随**：逐跳重新过 ``_validate`` + IP 校验，最多 3 跳。
    自动跟随会让"第一跳白名单内、第二跳跳去内网"这种绕过成立。
    注意：``_host_is_safe`` 的 IP 校验与实际 httpx 连接之间**存在 DNS rebinding
    时间窗口**（两次独立解析）。利用需控制白名单域名的权威 DNS，且仅影响
    「域名命中白名单但解析值被翻转」的场景——纵深上的已知缺口，暂以逐跳
    复检 + IP 禁段作为缓解。
    """
    # 而链路只允许 https（见 _upgrade_to_https 的说明）
    url = _upgrade_to_https(url)
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
                    # 重定向同样过一遍升级：CDN 可能把 https 跳回 http
                    current = _upgrade_to_https(str(httpx.URL(current).join(location)))
                    logger.info("音频下载重定向（第 %d 跳）", hop + 1)
                    _validate(current)
                    continue

                if resp.status_code != 200:
                    raise RuntimeError(f"音频下载失败：HTTP {resp.status_code}")

                # 内容校验分两步（实测定级）：
                # ① content-type 只用来**快速否掉确定是错误页**的类型——
                #    线上实测网易云 CDN 的不同节点会对同一首合法 MP3 返回
                #    `application/octet-stream`（换 UA/scheme 都不复现，是节点差异），
                #    所以不能像旧实现那样"非 audio/* 一律拒"。它只会让正常点歌
                #    间歇性失败。
                # ② 真正的判据是下载后的**魔数嗅探**（见 _looks_like_audio）——
                #    比信任响应头更可靠，也更安全（头是可以随便写的）。
                ctype = (
                    (resp.headers.get("content-type") or "")
                    .split(";")[0]
                    .strip()
                    .lower()
                )
                if _is_definitely_not_audio(ctype):
                    raise UnsafeURLError(f"content-type 明显非音频：{ctype}")

                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise UnsafeURLError(
                        f"音频超过上限：{int(declared)} > {max_bytes} 字节"
                    )

                written = 0
                head = b""
                try:
                    with open(dest, "wb") as fh:
                        async for chunk in resp.aiter_bytes(_CHUNK):
                            written += len(chunk)
                            if written > max_bytes:
                                raise UnsafeURLError(
                                    f"音频流超过上限：>{max_bytes} 字节（已写 {written}）"
                                )
                            if len(head) < _MAGIC_PROBE:
                                head += chunk[: _MAGIC_PROBE - len(head)]
                            fh.write(chunk)
                except Exception:
                    dest.unlink(missing_ok=True)
                    raise

            if written == 0:
                dest.unlink(missing_ok=True)
                raise RuntimeError("音频下载结果为空")
            if not _looks_like_audio(head):
                # 清掉刚写的非音频文件，避免脏缓存
                dest.unlink(missing_ok=True)
                raise UnsafeURLError(
                    f"下载内容不是已知音频格式（前 {len(head)} 字节：{head[:12]!r}）"
                )
            logger.info("音频下载完成：%d 字节 → %s", written, dest)
            return dest

    raise UnsafeURLError(f"重定向超过 {_MAX_REDIRECTS} 跳，放弃")
