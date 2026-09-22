"""看板（dashboard）只读 API 客户端：给 bot 取「当前机器概览」用。

为什么要单独一个模块：``agentcore`` 守则是不 import nonebot，而"取数"本身与
平台无关（HTTP + JSON），放在这里既能被插件复用，也能脱离 NoneBot 单测。

数据来源与字段含义见 ``dashboard/docs/API.md``。这里只用两个接口：

- ``GET /api/overview``：一屏所需的全部瞬时指标（CPU/内存/功耗/网速/磁盘/
  温度/负载/最忙进程/服务摘要）。
- ``GET /api/series``：曲线数据（概览页的迷你折线图需要）。**只取要用的 key**
  ——不带 keys 会连 ``cpu0…cpuN`` 一起返回，白白放大载荷。

鉴权用看板的**只读令牌**（页面侧栏「账号 → 新建只读令牌」，或启动时用
``DASHBOARD_API_TOKEN`` 播种）：bot 不必存账号密码，也不会因改密码被踢。
令牌只能读，拿到也改不了任何东西——这正是给 bot 用的形态。

env（``load_from_env``）：

- ``AGENT_DASHBOARD_URL``：看板地址，默认 ``http://127.0.0.1:8282``（同机部署）。
- ``AGENT_DASHBOARD_TOKEN``：只读令牌（``dshk_…``）。留空 = 未配置，概览功能不可用。
- ``AGENT_DASHBOARD_TIMEOUT``：单次请求墙钟上限（秒），默认 8。

失败一律抛 :class:`DashboardError`（带中文原因），调用方据此给用户一句人话，
而不是把 httpx 的堆栈丢进聊天框。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:8282"
DEFAULT_TIMEOUT = 8.0

# 概览页只用这 5 条曲线（overview.js METRICS）；不要取全部 key
SERIES_KEYS = "cpu,mem_used,power,net_down,net_up"


class DashboardError(RuntimeError):
    """取数失败（配置/网络/鉴权/响应异常），``str(exc)`` 是可直接展示的中文原因。"""


def _parse_timeout(raw: str | None, default: float = DEFAULT_TIMEOUT) -> float:
    """非正/脏值回退默认（0 会让 httpx 变成"立即超时"，不是"不限时"）。"""
    text = (raw or "").strip()
    if not text:
        return default
    try:
        value = float(text)
    except ValueError:
        logger.warning("AGENT_DASHBOARD_TIMEOUT=%r 不是数字，回退 %.0fs", raw, default)
        return default
    if value <= 0:
        logger.warning(
            "AGENT_DASHBOARD_TIMEOUT=%r 非法（须 > 0），回退 %.0fs", raw, default
        )
        return default
    return value


def normalize_base_url(raw: str) -> str:
    """校验并归一化看板地址（去尾斜杠；只允许 http/https）。

    地址来自运维配置，不是用户输入，这里做 scheme 白名单只是**纵深防御**：
    免得 ``file:///etc/passwd`` 这类笔误被 httpx 当成合法目标（会拿到语焉不详的
    报错，而不是"地址不对"）。
    """
    url = (raw or "").strip().rstrip("/")
    if not url:
        raise DashboardError("未配置看板地址（AGENT_DASHBOARD_URL）")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise DashboardError(f"看板地址不合法（需 http(s)://主机[:端口]）：{url}")
    return url


@dataclass
class DashboardClient:
    """看板只读客户端。``snapshot()`` 一次拿齐概览图要用的全部数据。"""

    base_url: str
    token: str = ""
    timeout: float = DEFAULT_TIMEOUT
    _client: httpx.AsyncClient | None = field(default=None, repr=False, compare=False)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _http(self) -> httpx.AsyncClient:
        # 懒建：NoneBot 启动期就构造客户端会绑定到当时的 event loop，而测试里
        # 每个用例一个 loop；首次请求时再建，并且由调用方 aclose 释放。
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=min(3.0, self.timeout)),
                headers=self._headers(),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = self.base_url + path
        try:
            response = await self._http().get(url, params=params)
        except httpx.TimeoutException as exc:
            raise DashboardError(f"看板请求超时（>{self.timeout:g}s）") from exc
        except httpx.HTTPError as exc:
            raise DashboardError(
                f"连不上看板（{self.base_url}）：{type(exc).__name__}"
            ) from exc
        if response.status_code in (401, 403):
            raise DashboardError("看板令牌无效或已撤销（AGENT_DASHBOARD_TOKEN）")
        if response.status_code >= 400:
            raise DashboardError(f"看板返回 HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise DashboardError("看板返回的不是 JSON（地址指向了别的服务？）") from exc

    async def snapshot(self) -> dict[str, Any]:
        """取一次概览快照，返回可直接喂给 ``render_overview_png`` 的字典。

        形状：``{"overview": {...}, "series": {key: [[ts, value], ...]}, "ts": float,
        "window": float, "interval": float}``。曲线接口失败**不致命**——概览图
        只是少几条曲线，数值卡片照常可用，所以降级为空 series 而不是整单失败。
        """
        overview = await self._get_json("/api/overview")
        if not isinstance(overview, dict):
            raise DashboardError("看板 /api/overview 返回结构异常")
        series: dict[str, Any] = {}
        ts = overview.get("ts")
        window = overview.get("window")
        interval = overview.get("interval")
        try:
            payload = await self._get_json("/api/series", {"keys": SERIES_KEYS})
            if isinstance(payload, dict):
                if isinstance(payload.get("series"), dict):
                    series = payload["series"]
                ts = payload.get("ts", ts)
                window = payload.get("window", window)
                interval = payload.get("interval", interval)
        except DashboardError as exc:
            logger.warning("看板曲线取数失败，概览图将不含折线：%s", exc)
        return {
            "overview": overview,
            "series": series,
            "ts": ts,
            "window": window,
            "interval": interval,
        }


def load_from_env() -> DashboardClient:
    """按 env 构造客户端；未配令牌时 ``token`` 为空（调用方据此提示未配置）。"""
    base = normalize_base_url(os.getenv("AGENT_DASHBOARD_URL") or DEFAULT_BASE_URL)
    token = (os.getenv("AGENT_DASHBOARD_TOKEN") or "").strip()
    timeout = _parse_timeout(os.getenv("AGENT_DASHBOARD_TIMEOUT"))
    return DashboardClient(base_url=base, token=token, timeout=timeout)


def configured() -> bool:
    """是否配了只读令牌——没配就整个功能不响应（fail-closed，不猜、不裸奔）。"""
    return bool((os.getenv("AGENT_DASHBOARD_TOKEN") or "").strip())
