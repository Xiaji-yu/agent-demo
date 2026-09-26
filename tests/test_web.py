"""Web 只读总览：鉴权 fail-closed + IP 白名单 + 数据口径（无密钥、无正文）。

安全模型见 ``plugins/qq_agent_adapter/web.py`` 模块 docstring——这个测试文件的
重点就是**证明那道门是真的**：没 token 不挂载、错 token 401、IP 不在白名单 403、
返回体里没有 api_key/base_url。
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from plugins.qq_agent_adapter import web


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("AGENT_WEB_TOKEN", "AGENT_WEB_ALLOW_CIDRS"):
        monkeypatch.delenv(key, raising=False)


def _mount(monkeypatch, token="t0ken", cidrs=None):
    monkeypatch.setenv("AGENT_WEB_TOKEN", token)
    if cidrs is not None:
        monkeypatch.setenv("AGENT_WEB_ALLOW_CIDRS", cidrs)
    app = FastAPI()
    assert web.mount_web(app) is True
    return app


def _client(app, token="t0ken", host="testclient"):
    return TestClient(app, headers={"Authorization": f"Bearer {token}"}), host


class TestFailClosed:
    def test_not_mounted_without_token(self, monkeypatch):
        """没配 token → 一个路由都不挂（默认部署零暴露）。"""
        app = FastAPI()
        assert web.mount_web(app) is False
        paths = {r.path for r in app.routes}
        assert not any(p.startswith(web.PREFIX) for p in paths), paths

    def test_blank_token_counts_as_unset(self, monkeypatch):
        monkeypatch.setenv("AGENT_WEB_TOKEN", "   ")
        assert web.mount_web(FastAPI()) is False

    def test_mount_failure_does_not_raise(self, monkeypatch):
        """挂载异常只能记日志返回 False，绝不能把 bot 启动带崩。"""
        monkeypatch.setenv("AGENT_WEB_TOKEN", "t")

        class _BoomApp(FastAPI):
            def get(self, *a, **kw):
                raise RuntimeError("boom")

        assert web.mount_web(_BoomApp()) is False


class TestAuth:
    def test_missing_token_401(self, monkeypatch):
        app = _mount(monkeypatch)
        r = TestClient(app).get(f"{web.PREFIX}/api/overview")
        assert r.status_code == 401

    def test_wrong_token_401(self, monkeypatch):
        app = _mount(monkeypatch)
        r = TestClient(app, headers={"Authorization": "Bearer nope"}).get(
            f"{web.PREFIX}/api/overview"
        )
        assert r.status_code == 401

    def test_correct_token_200(self, monkeypatch):
        app = _mount(monkeypatch)
        r = TestClient(app, headers={"Authorization": "Bearer t0ken"}).get(
            f"{web.PREFIX}/api/overview"
        )
        assert r.status_code == 200
        assert r.json()["generated_at"] > 0

    def test_token_in_query_is_rejected(self, monkeypatch):
        """query 传 token 一律不认——query 会进反代/访问日志。"""
        app = _mount(monkeypatch)
        r = TestClient(app).get(f"{web.PREFIX}/api/overview?token=t0ken")
        assert r.status_code == 401

    def test_token_compared_in_constant_time(self):
        """时序侧信道在功能测试里观测不到，用源码护栏钉住 compare_digest。

        与仓库既有先例一致（tests/test_music_route.py 的
        test_handler_passes_matcher_send）：验的是文本，但是唯一可行的护栏。
        """
        from pathlib import Path

        src = Path("plugins/qq_agent_adapter/web.py").read_text(encoding="utf-8")
        assert "secrets.compare_digest" in src
        assert "supplied != token" not in src

    def test_html_page_is_public_but_api_is_locked(self, monkeypatch):
        """**页面公开、API 强制鉴权**——线上实测的教训：

        早期连页面也挂 _auth，未带 token 的访问拿到 401 JSON，用户永远看不到
        登录框（鸡生蛋）。页面只是表单 + JS、零机密，公开是正确形态；数据面
        （api/overview）仍然 401。这条用例钉住两侧，别再把页面锁回去。
        """
        app = _mount(monkeypatch)
        r = TestClient(app).get(f"{web.PREFIX}/")
        assert r.status_code == 200, "登录页必须可达，否则没人能输入 token"
        assert "agent-demo 总览" in r.text
        # 页面本身不得内嵌 token
        assert "t0ken" not in r.text
        # 数据面没 token 照样拒
        assert TestClient(app).get(f"{web.PREFIX}/api/overview").status_code == 401

    def test_page_reads_fragment_token_and_clears_it(self):
        """支持 #token=xxx（fragment 不发服务器、不进日志，比 ?token= 安全）。

        JS 在字符串常量里、pytest 执行不到，按仓库既有先例做源码护栏。
        """
        from pathlib import Path

        src = Path("plugins/qq_agent_adapter/web.py").read_text(encoding="utf-8")
        assert "location.hash.match(/token=" in src, "页面要能从 fragment 取 token"
        assert "replaceState" in src, "取到后必须把 token 从地址栏抹掉"

    def test_ip_allowlist_blocks_other_source(self, monkeypatch):
        app = _mount(monkeypatch, cidrs="127.0.0.1")
        c, host = _client(app)
        # TestClient 默认源地址是 "testclient"（非法 IP）→ 解析失败即拒
        assert c.get(f"{web.PREFIX}/api/overview").status_code == 403

    def test_ip_allowlist_allows_listed_source(self, monkeypatch):
        app = _mount(monkeypatch, cidrs="10.0.0.0/8")
        c = TestClient(
            app,
            headers={"Authorization": "Bearer t0ken"},
            client=("10.1.2.3", 5000),
        )
        assert c.get(f"{web.PREFIX}/api/overview").status_code == 200

    def test_no_allowlist_means_token_only(self, monkeypatch):
        """不配白名单时任何源地址都可过（token 是唯一门），别默认拒掉自己。"""
        app = _mount(monkeypatch)
        c = TestClient(app, headers={"Authorization": "Bearer t0ken"})
        assert c.get(f"{web.PREFIX}/api/overview").status_code == 200


class TestOverviewPayload:
    def test_shape_and_no_secrets(self, monkeypatch):
        app = _mount(monkeypatch)
        c = TestClient(app, headers={"Authorization": "Bearer t0ken"})
        d = c.get(f"{web.PREFIX}/api/overview").json()
        for key in (
            "generated_at",
            "bots",
            "llm",
            "budget",
            "memory_backend",
            "skills",
            "recent_events",
        ):
            assert key in d, f"缺少 {key}"
        blob = str(d)
        # 密钥/内网地址绝不进响应体（它们会被渲染进浏览器）
        assert "api_key" not in blob and "apikey" not in blob.lower()
        assert "192.168." not in blob and "10.0.0." not in blob

    def test_recent_events_surfaced(self, monkeypatch):
        from agentcore.diagnostics import clear as _clear
        from agentcore.diagnostics import record as _record

        _clear()
        try:
            _record("llm_fallback", primary_model="m1")
            app = _mount(monkeypatch)
            c = TestClient(app, headers={"Authorization": "Bearer t0ken"})
            d = c.get(f"{web.PREFIX}/api/overview").json()
            assert d["recent_events"][0]["kind"] == "llm_fallback"
            assert d["recent_events"][0]["primary_model"] == "m1"
        finally:
            _clear()

    def test_unreachable_sections_degrade_not_500(self, monkeypatch):
        """取不到数据的块置 null + 记 errors，绝不让整个页面 500。"""
        app = _mount(monkeypatch)
        c = TestClient(app, headers={"Authorization": "Bearer t0ken"})
        d = c.get(f"{web.PREFIX}/api/overview").json()
        # 测试环境没有 NoneBot driver：这些块应当是 null 且 errors 说明了原因
        assert d["budget"] is not None, "budget 不依赖 driver，应能取到"
        assert isinstance(d["errors"], list)
