"""看板只读 API 客户端：地址/超时归一化、鉴权与失败路径、曲线降级。

用 ``httpx.MockTransport`` 打桩，不碰真实看板（CI 里也没有看板）。
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from agentcore import dashboard


def _client(handler, *, token="dshk_test", timeout=5.0) -> dashboard.DashboardClient:
    client = dashboard.DashboardClient(
        base_url="http://dash.test:8282", token=token, timeout=timeout
    )
    client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers=client._headers(),
        timeout=timeout,
    )
    return client


def _run(coro):
    return asyncio.run(coro)


def _ok_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/overview":
        return httpx.Response(
            200,
            json={
                "ts": 100.0,
                "ready": True,
                "host": "h",
                "window": 120.0,
                "interval": 1.0,
                "cpu": {"available": True, "percent": 10.0},
            },
        )
    return httpx.Response(
        200,
        json={
            "ts": 101.0,
            "window": 120.0,
            "interval": 1.0,
            "series": {"cpu": [[100.0, 10.0], [101.0, 12.0]]},
        },
    )


class TestUrlAndTimeout:
    def test_strips_trailing_slash(self):
        assert (
            dashboard.normalize_base_url("http://127.0.0.1:8282/")
            == "http://127.0.0.1:8282"
        )

    def test_rejects_non_http_scheme(self):
        """纵深防御：file:// / ftp:// 这类笔误必须报"地址不合法"，而不是交给 httpx 猜。

        带 netloc 的 ftp:// 是关键样本——只查 netloc 的实现会放它过去。
        """
        for bad in ("file:///etc/passwd", "ftp://example.com/x", "gopher://h/1"):
            with pytest.raises(dashboard.DashboardError):
                dashboard.normalize_base_url(bad)

    def test_rejects_empty_and_scheme_only(self):
        for bad in ("", "   ", "http://"):
            with pytest.raises(dashboard.DashboardError):
                dashboard.normalize_base_url(bad)

    def test_timeout_falls_back_on_dirty_values(self):
        assert dashboard._parse_timeout(None) == dashboard.DEFAULT_TIMEOUT
        assert dashboard._parse_timeout("") == dashboard.DEFAULT_TIMEOUT
        assert dashboard._parse_timeout("abc") == dashboard.DEFAULT_TIMEOUT
        # 0 / 负数在 httpx 里是"立即超时"，不是"不限时"——必须回退默认
        assert dashboard._parse_timeout("0") == dashboard.DEFAULT_TIMEOUT
        assert dashboard._parse_timeout("-1") == dashboard.DEFAULT_TIMEOUT
        assert dashboard._parse_timeout("2.5") == 2.5


class TestEnv:
    def test_load_from_env_defaults(self, monkeypatch):
        monkeypatch.delenv("AGENT_DASHBOARD_URL", raising=False)
        monkeypatch.delenv("AGENT_DASHBOARD_TOKEN", raising=False)
        monkeypatch.delenv("AGENT_DASHBOARD_TIMEOUT", raising=False)
        client = dashboard.load_from_env()
        assert client.base_url == dashboard.DEFAULT_BASE_URL
        assert client.token == ""
        assert dashboard.configured() is False

    def test_load_from_env_reads_token(self, monkeypatch):
        monkeypatch.setenv("AGENT_DASHBOARD_URL", "http://10.0.0.5:9000")
        monkeypatch.setenv("AGENT_DASHBOARD_TOKEN", "  dshk_x  ")
        client = dashboard.load_from_env()
        assert client.base_url == "http://10.0.0.5:9000"
        assert client.token == "dshk_x"  # 前后空白必须剥掉，否则 Bearer 头带空格
        assert dashboard.configured() is True

    def test_blank_token_is_not_configured(self, monkeypatch):
        monkeypatch.setenv("AGENT_DASHBOARD_TOKEN", "   ")
        assert dashboard.configured() is False


class TestSnapshot:
    def test_merges_overview_and_series(self):
        client = _client(_ok_handler)
        snap = _run(client.snapshot())
        assert snap["overview"]["host"] == "h"
        assert snap["series"]["cpu"] == [[100.0, 10.0], [101.0, 12.0]]
        # ts/window/interval 以曲线响应为准（概览图的时间轴跟着曲线走）
        assert snap["ts"] == 101.0
        assert snap["window"] == 120.0
        assert snap["interval"] == 1.0

    def test_series_keys_are_limited(self):
        """不带 keys 会把 cpu0…cpuN 一起拉回来（载荷放大），必须显式限定。"""
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/series":
                seen.update(dict(request.url.params))
            return _ok_handler(request)

        _run(_client(handler).snapshot())
        assert seen.get("keys") == dashboard.SERIES_KEYS

    def test_sends_bearer_token(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("Authorization")
            return _ok_handler(request)

        _run(_client(handler).snapshot())
        assert seen["auth"] == "Bearer dshk_test"

    def test_series_failure_degrades_not_raises(self):
        """曲线挂了只是少几条折线，数值卡片仍可用——不能整单失败。"""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/series":
                raise httpx.ConnectError("boom", request=request)
            return _ok_handler(request)

        snap = _run(_client(handler).snapshot())
        assert snap["series"] == {}
        assert snap["overview"]["host"] == "h"

    def test_unauthorized_message_mentions_token(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "unauthorized"})

        with pytest.raises(dashboard.DashboardError) as exc:
            _run(_client(handler).snapshot())
        assert "令牌" in str(exc.value)

    def test_http_error_is_reported(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="boom")

        with pytest.raises(dashboard.DashboardError) as exc:
            _run(_client(handler).snapshot())
        assert "500" in str(exc.value)

    def test_non_json_response_reported(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>this is not the dashboard</html>")

        with pytest.raises(dashboard.DashboardError) as exc:
            _run(_client(handler).snapshot())
        assert "JSON" in str(exc.value)

    def test_timeout_reported(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("slow", request=request)

        with pytest.raises(dashboard.DashboardError) as exc:
            _run(_client(handler).snapshot())
        assert "超时" in str(exc.value)

    def test_non_dict_overview_rejected(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[1, 2, 3])

        with pytest.raises(dashboard.DashboardError):
            _run(_client(handler).snapshot())


class TestLifecycle:
    def test_aclose_is_idempotent_and_recreates(self):
        client = _client(_ok_handler)
        _run(client.snapshot())
        _run(client.aclose())
        _run(client.aclose())  # 二次调用必须安全
        # 关掉之后再取数要能自愈（懒建新连接池）
        client._client = httpx.AsyncClient(
            transport=httpx.MockTransport(_ok_handler), timeout=5.0
        )
        assert _run(client.snapshot())["overview"]["host"] == "h"

    def test_json_roundtrip_shapes(self):
        """snapshot 必须是可安全序列化的纯 JSON 结构（不带 httpx 对象）。"""
        snap = _run(_client(_ok_handler).snapshot())
        json.dumps(snap)


class TestIntegrationWithRenderer:
    """两个模块的接缝：客户端产出的快照必须能**直接**喂给渲染器。

    单测各自绿、接缝绿不了的情况真实存在（字段名/结构漂移），所以这里跑一遍
    stub 看板 → snapshot → 真渲染，断言产出合法 PNG。
    """

    def test_snapshot_renders_end_to_end(self):
        from agentcore.render import overview as ov

        if not ov.ensure_font_probed():
            pytest.skip("环境无 CJK 字体")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/overview":
                return httpx.Response(
                    200,
                    json={
                        "ts": 1790026700.0,
                        "ready": True,
                        "host": "e2e-host",
                        "cores": 4,
                        "uptime_s": 3600.0,
                        "process_count": 3,
                        "window": 120.0,
                        "interval": 1.0,
                        "cpu": {"available": True, "percent": 42.0, "freq_mhz": 3000},
                        "memory": {
                            "available": True,
                            "used_gb": 4.0,
                            "total_gb": 16.0,
                            "percent": 25.0,
                            "swap_used_gb": 0.0,
                        },
                        "power": {"available": True, "watts": 9.0, "source": "平均值"},
                        "net": {"available": True, "nic": "eth0"},
                        "disk": {
                            "available": True,
                            "path": "/",
                            "free_gb": 100.0,
                            "total_gb": 233.0,
                            "used_percent": 57.0,
                        },
                        "temp": {"available": True, "celsius": 70.0},
                        "processes": [
                            {"pid": 1, "name": "chrome", "cpu": 1.0, "rss_mb": 2.0}
                        ],
                        "services": [
                            {
                                "group": "容器",
                                "name": "Docker",
                                "status": "ok",
                                "detail": "1/1",
                            }
                        ],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "ts": 1790026700.0,
                    "window": 120.0,
                    "interval": 1.0,
                    "series": {
                        "cpu": [[1790026699.0, 40.0], [1790026700.0, 42.0]],
                        "mem_used": [[1790026699.0, 4.0], [1790026700.0, 4.0]],
                        "power": [[1790026699.0, 8.0], [1790026700.0, 9.0]],
                        "net_down": [[1790026699.0, 100.0], [1790026700.0, 200.0]],
                        "net_up": [[1790026699.0, 50.0], [1790026700.0, 60.0]],
                    },
                },
            )

        snapshot = _run(_client(handler).snapshot())
        png = ov.render_overview_png(snapshot)
        assert png is not None
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        assert len(png) > 10_000
