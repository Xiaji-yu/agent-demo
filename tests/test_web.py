"""Web 只读总览：鉴权 fail-closed + IP 白名单 + 数据口径（无密钥、无正文）。

安全模型见 ``plugins/qq_agent_adapter/web.py`` 模块 docstring——这个测试文件的
重点就是**证明那道门是真的**：没 token 不挂载、错 token 401、IP 不在白名单 403、
返回体里没有 api_key/base_url。
"""

import gzip
import hashlib
import re
import time

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


def _totp() -> str:
    """当前 5 分钟 2FA 滚动码（conftest 已把 secret 关进 tmp）。"""
    from agentcore import totp

    return totp.current_code(totp.load_or_create_secret())


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

    def test_page_literal_is_raw_string(self):
        """页面常量必须是 raw string —— 这不是风格问题。

        ``_PAGE`` 里是 CSS/JS，普通三引号会把 ``join("\\n")`` 的转义吃成**真实
        换行** → 整个 <script> 语法错误 → 页面白屏；而 pytest 只拿到一个字符串、
        完全看不到（实测踩过一次，只有浏览器/node --check 报错）。故钉住声明形式
        与那个必须活到浏览器的转义。
        """
        from pathlib import Path

        src = Path("plugins/qq_agent_adapter/web.py").read_text(encoding="utf-8")
        assert '_PAGE = r"""' in src, "必须用 raw string 声明"
        assert 'join("\\n")' in web._PAGE, "JS 转义必须原样到浏览器"

    def test_css_and_markup_class_names_stay_in_sync(self):
        """类名双向一致：用到的都有定义、定义的都被用到。

        没有浏览器可截图时，"类名打错 → 样式静默丢失"是唯一看不出来的失效模式；
        反向还能顺手抓出死代码（实测抓到过 btn--ghost / stat--wide 两个）。
        提取规则：纯字面量 class 属性取全部 token；含拼接的取首个 token（一定是
        字面量）；JS 里带 __ / -- 的引号字面量按类名处理。
        """
        import re

        src = web._PAGE
        css = re.search(r"<style>(.*?)</style>", src, re.DOTALL).group(1)
        body = src.split("</style>", 1)[1]
        declared = set(re.findall(r"\.([A-Za-z][\w-]*)", css))

        used = set()
        for raw in re.findall(r'class="([^"]*)"', body):
            toks = raw.split()
            if toks and re.fullmatch(r"[a-z][\w-]*", toks[0]):
                used.add(toks[0])
            if not re.search(r"['\"+(?|]", raw):
                used.update(t for t in toks if re.fullmatch(r"[a-z][\w-]*", t))
        for lit in re.findall(r'"([a-z][\w]*(?:__[\w-]+|--[\w-]+)+)"', body):
            used.add(lit)
        for base in (
            "bar",
            "dot",
            "chip",
            "panel",
            "row",
            "stat",
            "tbl",
            "sec",
            "ev",
            "errs",
            "hero",
            "toast",
        ):
            if re.search(rf'["\']{base}[ "\\\']', body):
                used.add(base)
        if "is-over" in body:
            used.add("is-over")

        assert not (used - declared), (
            f"这些类名没有 CSS 定义（样式会静默丢失）：{sorted(used - declared)}"
        )
        assert not (declared - used), (
            f"CSS 里这些类没有任何地方使用（死代码）：{sorted(declared - used)}"
        )

    def test_page_is_self_contained(self):
        """不引任何外部资源：局域网管理页可能在没有外网的机器上打开。"""
        import re

        assert "fonts.googleapis" not in web._PAGE
        assert not re.search(r"""(?:src|href)\s*=\s*["']https?://""", web._PAGE), (
            "不得引用外链资源（字体/JS/CSS 一律内置）"
        )

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


class TestSettingsView:
    """设置视图（/api/settings）：只读、同门禁、白名单键、无密钥、降级不 500。"""

    URL = f"{web.PREFIX}/api/settings"

    def _get(self, monkeypatch, **kw):
        app = _mount(monkeypatch, **kw)
        return TestClient(app, headers={"Authorization": "Bearer t0ken"}).get(self.URL)

    def test_endpoint_locked_same_as_overview(self, monkeypatch):
        """settings 与 overview 同一道门：无 token/错 token/query token 一律 401。"""
        app = _mount(monkeypatch)
        assert TestClient(app).get(self.URL).status_code == 401
        assert (
            TestClient(app, headers={"Authorization": "Bearer nope"})
            .get(self.URL)
            .status_code
            == 401
        )
        assert TestClient(app).get(f"{self.URL}?token=t0ken").status_code == 401

    def test_groups_present_and_effective_labeled(self, monkeypatch):
        d = self._get(monkeypatch).json()
        for key in (
            "llm",
            "runtime_flags",
            "budget",
            "memory_summary",
            "rag",
            "backup_archive",
        ):
            assert key in d, f"缺少 {key}"
            if d[key] is not None:
                assert d[key].get("effective") in ("live", "boot"), key
        assert isinstance(d["errors"], list)

    def test_runtime_flags_reflect_env(self, monkeypatch):
        monkeypatch.setenv("AGENT_WAKE_WORDS", "小助手,云崽")
        d = self._get(monkeypatch).json()
        f = d["runtime_flags"]
        assert f is not None, "运行开关只读 env，不依赖 driver"
        assert f["effective"] == "live"
        assert f["wake_words"] == ["小助手", "云崽"]
        assert set(f) == {"effective", "vision", "group_context", "wake_words"}

    def test_no_secrets_in_payload(self, monkeypatch):
        """密钥/base_url 绝不进响应体：键名与**真实环境变量值**双重断言。"""
        import os

        d = self._get(monkeypatch).json()
        blob = str(d)
        assert "base_url" not in blob.lower()
        assert "api_key" not in blob.lower() and "apikey" not in blob.lower()
        assert "sk-" not in blob
        for env_key in ("LLM_API_KEY", "EMBEDDING_API_KEY", "AGENT_WEB_TOKEN"):
            v = os.getenv(env_key)
            if v:
                assert v not in blob, f"{env_key} 的真实值泄漏进了设置响应"

    def test_model_config_block(self, monkeypatch, tmp_path):
        """模型页 M3/M4 数据。embedding 单源（endpoint_set/key_set）；搜索多源
        （providers 列表，admin_view 含完整 endpoint 不含 key，仅鉴权 web 面）。
        字段名与 secret 值不得泄漏 base_url/api_key 子串（endpoint URL 允许回显）。"""
        # 隔离 search-providers.json：指向不存在的 tmp 路径，强制回落 env 单配置
        monkeypatch.setenv(
            "AGENT_SEARCH_PROVIDERS_FILE",
            str(tmp_path / "no-search-providers.json"),
        )
        monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-3-small")
        monkeypatch.setenv("EMBEDDING_DIM", "2048")
        monkeypatch.setenv("EMBEDDING_BASE_URL", "https://api.example.com/v1")
        monkeypatch.setenv("EMBEDDING_API_KEY", "sk-emb-secret")
        monkeypatch.setenv("SEARCH_PROVIDER", "bocha")
        monkeypatch.setenv("SEARCH_API_KEY", "sk-search-secret")
        monkeypatch.setenv("SEARCH_BASE_URL", "https://search-proxy.example.com/v1")
        d = self._get(monkeypatch).json()
        mc = d["model_config"]
        assert mc is not None and mc["effective"] == "boot"
        emb = mc["embedding"]
        assert emb["model"] == "text-embedding-3-small"
        assert emb["dim"] == "2048"
        assert emb["endpoint_set"] is True and emb["key_set"] is True
        srch = mc["search"]
        assert srch["lang"] == "zh-CN"
        ps = srch["providers"]
        assert len(ps) == 1
        assert ps[0]["format"] == "bocha"
        assert ps[0]["key_set"] is True
        # admin_view 回显完整 endpoint（鉴权 web 面允许）
        assert ps[0]["endpoint"] == "https://search-proxy.example.com/v1"
        # 字段名与 secret 值不得泄漏 base_url/api_key 子串
        blob = str(d).lower()
        assert "base_url" not in blob and "api_key" not in blob
        assert "sk-emb-secret" not in str(d) and "sk-search-secret" not in str(d)
        assert "https://api.example.com" not in str(d)  # embedding 端点不回显

    def test_backup_archive_whitelist_only(self, monkeypatch, tmp_path):
        """config.yaml 备份/归档段只出白名单键——段里混进敏感值不得被整段倒出。"""
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "archive:\n  dir: data/archive\n  keep_days: 7\n"
            'backup:\n  dir: data/backups\n  keep: 7\n  cron: "30 3 * * *"\n'
            '  mirror_dir: ""\n  internal_token: topsecret\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("AGENT_CONFIG", str(cfg))
        d = self._get(monkeypatch).json()
        ba = d["backup_archive"]
        assert ba["effective"] == "boot"
        assert set(ba["archive"]) == {"dir", "keep_days"}
        assert set(ba["backup"]) == {"dir", "keep", "cron", "mirror_dir"}
        assert "topsecret" not in str(d)

    def test_memory_summary_whitelist_only(self, monkeypatch):
        """agent: 段同样只出白名单键（有 driver 用 engine.config，无则 null）。"""
        d = self._get(monkeypatch).json()
        ms = d["memory_summary"]
        if ms is None:
            assert any(s.startswith("memory_summary") for s in d["errors"])
        else:
            assert set(ms) == {"effective", *web._AGENT_CFG_KEYS}

    def test_memory_summary_helper_whitelist(self):
        """白名单逻辑是纯函数，不依赖 driver 可直接单测——段里混进的键不得带出。"""
        ms = web._memory_summary_from(
            {"extract_facts": False, "history_token_budget": 3000, "secret_thing": "x"}
        )
        assert ms["effective"] == "boot"
        assert ms["extract_facts"] is False
        assert ms["history_token_budget"] == 3000
        assert "secret_thing" not in ms
        assert set(ms) == {"effective", *web._AGENT_CFG_KEYS}

    def test_rag_shape_or_degrade(self, monkeypatch):
        d = self._get(monkeypatch).json()
        if d["rag"] is None:
            assert any(s.startswith("rag") for s in d["errors"])
        else:
            assert set(d["rag"]) == {
                "effective",
                "enabled",
                "top_k",
                "threshold",
                "digest_cron",
                "embedding",
            }

    def test_settings_tab_wired_in_page(self):
        """页面有设置入口且两个视图容器存在（防 tab 改没了没人发现）。"""
        assert 'id="tabSettings"' in web._PAGE
        assert 'id="settingsView"' in web._PAGE
        assert 'id="overviewView"' in web._PAGE
        assert "renderSettings" in web._PAGE
        assert "api/settings" in web._PAGE


class TestLogsView:
    """日志查看（/api/logs*）：文件白名单、tail 行对齐、增量游标、过滤、下载。

    内容口径（D4，2026-09-29 管理员决策）：**原始日志不脱敏**——matcher 的
    [msg]/[reply] 行含消息正文前 200 字，这是与"响应体不含消息正文"不变量之间
    显式批准的例外；有一条用例专门把"不脱敏"钉成回归。
    """

    URL = f"{web.PREFIX}/api/logs"

    @pytest.fixture
    def logdir(self, monkeypatch, tmp_path):
        """临时日志目录 + 已挂载的 app。"""
        d = tmp_path / "logs"
        d.mkdir()
        monkeypatch.setenv("AGENT_LOG_DIR", str(d))
        return d

    def _write(self, logdir, name, text):
        p = logdir / name
        p.write_text(text, encoding="utf-8")
        return p

    def _get(self, monkeypatch, query, **mount_kw):
        app = _mount(monkeypatch, **mount_kw)
        return TestClient(app, headers={"Authorization": "Bearer t0ken"}).get(
            self.URL + query
        )

    def test_files_list_whitelisted_only(self, logdir, monkeypatch):
        self._write(logdir, "agent.log", "x")
        self._write(logdir, "agent.log.2026-09-20", "y")
        self._write(logdir, "junk.txt", "z")
        self._write(logdir, "agent.log.evil", "w")
        app = _mount(monkeypatch)
        c = TestClient(app, headers={"Authorization": "Bearer t0ken"})
        d = c.get(f"{web.PREFIX}/api/logs/files").json()
        names = {f["name"] for f in d["files"]}
        assert names == {"agent.log", "agent.log.2026-09-20"}
        current = [f for f in d["files"] if f["name"] == "agent.log"]
        assert current and current[0]["current"] is True

    def test_traversal_rejected(self, logdir, monkeypatch):
        self._write(logdir, "agent.log", "x")
        for bad in ("../../etc/passwd", "/etc/passwd", "agent.log/../../x", ""):
            r = self._get(monkeypatch, f"?file={bad}")
            assert r.status_code == 200  # 路由不炸
            assert r.json()["lines"] == [] and r.json()["errors"], bad

    def test_download_traversal_rejected(self, logdir, monkeypatch):
        app = _mount(monkeypatch)
        r = TestClient(app, headers={"Authorization": "Bearer t0ken"}).get(
            f"{web.PREFIX}/api/logs/download?file=../../secrets"
        )
        assert r.status_code == 400

    def test_tail_and_structured_parse(self, logdir, monkeypatch):
        self._write(
            logdir,
            "agent.log",
            "2026-09-28 15:45:18,806 INFO plugins.qq_agent_adapter.matcher: [msg] hello\n"
            "2026-09-28 15:45:19,001 ERROR agentcore.llm.client: boom\n"
            "Traceback (most recent call last):\n"
            '  File "x.py", line 1\n'
            "ValueError: bad\n",
        )
        d = self._get(monkeypatch, "?file=agent.log").json()
        assert d["errors"] == [] and d["returned"] == 2
        first, second = d["lines"]
        assert first["level"] == "INFO" and "hello" in first["msg"]
        assert second["level"] == "ERROR"
        # 异常栈续行归并进上一条，不丢行
        assert "Traceback" in second["msg"] and "ValueError" in second["msg"]

    def test_tail_alignment_no_partial_line(self, logdir, monkeypatch):
        """窗口切在半行上：首残行必须丢弃，页面不出现半个时间戳。"""
        line1 = "2026-09-28 10:00:00,000 INFO m: " + "A" * 2000 + "\n"
        line2 = "2026-09-28 10:00:01,000 INFO m: short\n"
        self._write(logdir, "agent.log", line1 + line2)
        # bytes 恰好切在 line1 中间（>1024 下限，钳制不掩盖）
        d = self._get(monkeypatch, f"?file=agent.log&bytes={len(line1) - 5}").json()
        assert d["truncated"] is True
        assert all(x["ts"].startswith("2026-") for x in d["lines"])
        assert d["lines"][0]["msg"].startswith("short")
        # 行对齐记账：start_offset 必须落在首条完整行的行首（=line1 的长度），
        # 否则增量游标会从半行中间续读（解析器归并恰好兜住显示，偏移却已失真）
        assert d["start_offset"] == len(line1), d["start_offset"]

    def test_after_cursor_increment(self, logdir, monkeypatch):
        p = self._write(logdir, "agent.log", "2026-09-28 10:00:00,000 INFO m: one\n")
        d = self._get(monkeypatch, "?file=agent.log").json()
        assert d["returned"] == 1
        with open(p, "a", encoding="utf-8") as f:
            f.write("2026-09-28 10:00:01,000 INFO m: two\n")
        d2 = self._get(monkeypatch, f"?file=agent.log&after={d['end_offset']}").json()
        assert d2["returned"] == 1 and d2["lines"][0]["msg"] == "two"
        assert d2["reset"] is False

    def test_after_cursor_reset_on_rotation(self, logdir, monkeypatch):
        self._write(logdir, "agent.log", "2026-09-28 10:00:00,000 INFO m: old\n")
        d = self._get(monkeypatch, "?file=agent.log").json()
        # 模拟轮转：文件被换新且更小
        self._write(logdir, "agent.log", "2026-09-28 11:00:00,000 INFO m: new\n")
        d2 = self._get(
            monkeypatch, f"?file=agent.log&after={d['end_offset'] + 5000}"
        ).json()
        assert d2["reset"] is True
        assert d2["lines"][0]["msg"] == "new"  # 轮转后读到的是新文件内容

    def test_filters_matrix(self, logdir, monkeypatch):
        self._write(
            logdir,
            "agent.log",
            "2026-09-28 10:00:00,000 INFO plugins.qq_agent_adapter.matcher: [msg] alpha\n"
            "2026-09-28 10:00:01,000 WARNING agentcore.llm.client: beta\n"
            "2026-09-28 10:00:02,000 ERROR plugins.qq_agent_adapter.outbound: gamma\n",
        )
        q = "?file=agent.log&level=WARNING,ERROR"
        d = self._get(monkeypatch, q).json()
        assert [x["level"] for x in d["lines"]] == ["WARNING", "ERROR"]
        d = self._get(
            monkeypatch, "?file=agent.log&module=plugins.qq_agent_adapter"
        ).json()
        assert len(d["lines"]) == 2 and "agentcore.llm" not in {
            x["logger"] for x in d["lines"]
        }
        d = self._get(monkeypatch, "?file=agent.log&grep=gamma").json()
        assert len(d["lines"]) == 1 and d["lines"][0]["msg"].endswith("gamma")

    def test_grep_too_long_400(self, monkeypatch):
        r = self._get(monkeypatch, f"?file=agent.log&grep={'x' * 101}")
        assert r.status_code == 400

    def test_raw_logs_not_redacted(self, logdir, monkeypatch):
        """内容口径 C 的回归锚点：text= 载荷必须原样返回（不脱敏是显式决策）。"""
        secret_line = (
            "2026-09-28 15:45:18,806 INFO plugins.qq_agent_adapter.matcher: "
            "[msg] group:1 | user=2 | text=这是用户的原话内容\n"
        )
        self._write(logdir, "agent.log", secret_line)
        d = self._get(monkeypatch, "?file=agent.log").json()
        assert "这是用户的原话内容" in d["lines"][0]["msg"]

    def test_download_happy_and_audit(self, logdir, monkeypatch):
        from agentcore.diagnostics import clear as _clear
        from agentcore.diagnostics import recent as _recent

        self._write(logdir, "agent.log", "hello log\n")
        app = _mount(monkeypatch)
        _clear()
        try:
            r = TestClient(app, headers={"Authorization": "Bearer t0ken"}).get(
                f"{web.PREFIX}/api/logs/download?file=agent.log"
            )
            assert r.status_code == 200
            assert "agent.log" in r.headers.get("content-disposition", "")
            assert "hello log" in r.text
            events = _recent(5)
            assert events[0]["kind"] == "logs_downloaded"
            assert events[0]["file"] == "agent.log"
            assert "hello" not in str(events[0])  # 审计不含内容
        finally:
            _clear()

    def test_download_size_cap(self, logdir, monkeypatch):
        self._write(logdir, "agent.log", "x" * 100)
        monkeypatch.setattr(web, "_LOG_DOWNLOAD_MAX", 10)
        r = TestClient(
            _mount(monkeypatch), headers={"Authorization": "Bearer t0ken"}
        ).get(f"{web.PREFIX}/api/logs/download?file=agent.log")
        assert r.status_code == 400

    def test_logs_gate_same_as_settings(self, monkeypatch):
        app = _mount(monkeypatch)
        assert TestClient(app).get(self.URL).status_code == 401
        assert TestClient(app).get(self.URL + "/download").status_code == 401

    def test_logs_tab_wired_in_page(self):
        assert 'id="tabLogs"' in web._PAGE
        assert 'id="logsView"' in web._PAGE
        assert "renderLogs" in web._PAGE
        assert "api/logs" in web._PAGE

    def test_auto_refresh_appends_not_replaces(self):
        """线上反馈（2026-09-30）：勾选自动刷新后只剩 0–2 行。

        根因：增量轮询（after=游标）也走 innerHTML 整体替换，历史窗口被冲掉。
        护栏：增量必须 appendChild + DOM 上限；全量窗口仍 replaceChildren；
        日志行走 textContent（正文不可信，不拼 HTML）。
        """
        src = web._PAGE
        assert "appendChild" in src, "增量轮询必须追加行"
        assert "LOG_DOM_CAP" in src, "追加模式必须有 DOM 行数上限"
        assert "replaceChildren" in src, "全量窗口（手动刷新）仍走整体替换"
        assert "logph" in src, "空态占位需可被追加前清除"
        assert "div.textContent = x.ts" in src, "日志行必须用 textContent 构建"
        # REVIEW-de09478..workdir L5：轮转（reset）时增量必须退回整体替换，
        # 否则新文件尾窗接在旧文件行后（两文件 DOM 混排）
        assert "logState.after > 0 && !d.reset" in src, (
            "reset（轮转/截断）时禁止增量追加"
        )
        # REVIEW-de09478..workdir L5：在途互斥，防响应 >5s 时同批行追加两次。
        # 断言必须钉在**守卫语句本体**（`if (logInFlight) return;`）——旧断言
        # 只查 "logInFlight" 这个子串，而它同时出现在赋值/复位处：把整行守卫
        # 删掉测试依然绿（恒真断言）。钉语句后，删守卫 = 删子串 = 必然失败。
        assert "if (logInFlight) return;" in src, (
            "自动刷新轮询必须在发起前做在途互斥（守卫语句本体必须在位）"
        )
        # 守卫必须真的包住请求发起：赋值紧跟其后（否则 return 后不会再有人复位，
        # mutex 一旦误入 true 就永久卡死轮询——方向相反的恒真也要防）
        assert re.search(
            r"if \(logInFlight\) return;\s*\n\s*logInFlight = true;", src
        ), "互斥置位必须紧跟在守卫之后"


class TestSettingsSchema:
    """全部设置上 web：schema 完整性 + 凭据不出现在响应体。"""

    def _get(self, monkeypatch, tmp_path=None):
        if tmp_path is not None:
            cfg = tmp_path / "config.yaml"
            if not cfg.exists():
                cfg.write_text(
                    "agent:\n  max_iterations: 8  # 工具轮数\n", encoding="utf-8"
                )
            monkeypatch.setenv("AGENT_CONFIG", str(cfg))
        c, _ = TestSettingsWrite()._mount_write(monkeypatch, tmp_path or None)
        return c

    def test_payload_has_env_and_yaml_groups(self, monkeypatch, tmp_path):
        d = self._get(monkeypatch, tmp_path).get(f"{web.PREFIX}/api/settings").json()
        env = d["settings"]
        cfg = d["config"]
        assert env and env["total"] > 100 and env["writable"] > 50
        assert env["secret_count"] >= 10
        assert {g["name"] for g in env["groups"]} >= {"模型与推理", "权限与 ACL"}
        assert cfg and cfg["error"] is None and cfg["total"] >= 30
        paths = {i["key"] for g in cfg["groups"] for i in g["items"]}
        assert "yaml:agent.max_iterations" in paths

    def test_credentials_and_urls_never_returned(self, monkeypatch, tmp_path):
        """键名与值都不出：响应体不得含 api_key/base_url（AGENTS 不变量）。"""
        d = self._get(monkeypatch, tmp_path).get(f"{web.PREFIX}/api/settings").json()
        blob = str(d).lower()
        for bad in ("llm_api_key", "api_key", "base_url", "sk-"):
            assert bad not in blob, f"{bad} 出现在设置响应里"

    def test_live_and_boot_are_labelled(self, monkeypatch, tmp_path):
        d = self._get(monkeypatch, tmp_path).get(f"{web.PREFIX}/api/settings").json()
        items = {i["key"]: i for g in d["settings"]["groups"] for i in g["items"]}
        assert items["AGENT_VISION"]["live"] is True, "vision 是调用时读 env"
        assert items["LLM_TEMPERATURE"]["live"] is False, "LLM 配置启动期装配"
        assert items["LLM_TEMPERATURE"]["writable"] is True
        # 安全边界：可见但不可写
        assert items["SUPERUSERS"]["writable"] is False
        assert items["AGENT_WEB_WRITE"]["writable"] is False

    def test_acl_values_visible_but_credentials_not(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SUPERUSERS", '["9001"]')
        d = self._get(monkeypatch, tmp_path).get(f"{web.PREFIX}/api/settings").json()
        items = {i["key"]: i for g in d["settings"]["groups"] for i in g["items"]}
        assert items["SUPERUSERS"]["value"] == '["9001"]'
        assert items["SUPERUSERS"]["writable"] is False


class TestLLMProviders:
    """多 provider CRUD：key 只写不回显、两段确认、设主/删除/测试。"""

    PATH = f"{web.PREFIX}/api/settings/llm/providers"

    def _mount(self, monkeypatch, tmp_path):
        # 先种一个 base provider：避免"文件缺失时从真实 env 迁移"把用例数量
        # 变成不可预测（迁移本身由 test_llm_providers 覆盖）
        from agentcore.llm import providers as store

        store.save(
            store.ProviderConfig(
                active="base",
                providers=[
                    store.Provider(
                        "base", "Base", "openai", "http://base", "m-base", "k-base"
                    )
                ],
            )
        )
        # 共享客户端是进程单例：直接写文件后要让它重新装载（生产由启动期
        # ensure_migrated → get_shared_llm_client 保证同序）
        from agentcore.skills.registry import get_shared_llm_client

        get_shared_llm_client().reload_providers()
        return TestSettingsWrite()._mount_write(monkeypatch, tmp_path)

    def _post(self, c, body):
        return c.post(self.PATH, json=body)

    def _save(self, c, provider):
        r1 = self._post(c, {"action": "save", "provider": provider})
        assert r1.status_code == 200, r1.text
        data = r1.json()
        assert data.get("need_confirm") is True
        r2 = self._post(
            c,
            {
                "action": "save",
                "provider": provider,
                "confirm_token": data["confirm_token"],
                "confirm_nonce": data["confirm_nonce"],
                "totp_code": _totp(),
            },
        )
        return r2

    def _provider(self, **kw):
        base = {
            "name": "StepFun",
            "protocol": "openai",
            "endpoint": "https://api.stepfun.com/v1",
            "model": "step-3.7-flash",
            "api_key": "sk-test-secret",
        }
        base.update(kw)
        return base

    def test_save_and_key_write_only(self, monkeypatch, tmp_path):
        c, _envf = self._mount(monkeypatch, tmp_path)
        r = self._save(c, self._provider())
        assert r.status_code == 200 and r.json()["ok"] is True
        data = c.get(f"{web.PREFIX}/api/settings").json()
        ps = data["llm"]["providers"]
        assert len(ps) == 2, ps
        new = next(p for p in ps if p["name"] == "StepFun")
        assert new["key_set"] is True
        blob = str(data)
        assert "sk-test-secret" not in blob
        assert "api_key" not in blob.lower()
        # 文件里确有 key，且 0600
        from agentcore.llm import providers as store

        path = store.providers_path()
        assert "sk-test-secret" in path.read_text(encoding="utf-8")
        assert store.restore_mode(path) == 0o600

    def test_edit_without_key_keeps_it(self, monkeypatch, tmp_path):
        c, _envf = self._mount(monkeypatch, tmp_path)
        self._save(c, self._provider(id="main"))
        r = self._save(c, self._provider(id="main", name="Renamed", api_key=""))
        assert r.status_code == 200
        from agentcore.llm import providers as store

        cfg = store.load()
        main = cfg.by_id("main")
        assert main.name == "Renamed"
        assert main.api_key == "sk-test-secret", "留空 = 保持不变"

    def test_set_active(self, monkeypatch, tmp_path):
        c, _envf = self._mount(monkeypatch, tmp_path)
        self._save(c, self._provider(id="a", name="A", model="m-a"))
        self._save(c, self._provider(id="b", name="B", model="m-b", api_key="k-b"))
        r1 = self._post(c, {"action": "set_active", "id": "b"})
        d = r1.json()
        r2 = self._post(
            c,
            {
                "action": "set_active",
                "id": "b",
                "confirm_token": d["confirm_token"],
                "confirm_nonce": d["confirm_nonce"],
                "totp_code": _totp(),
            },
        )
        assert r2.status_code == 200 and r2.json()["active"] == "b"
        assert c.get(f"{web.PREFIX}/api/settings").json()["llm"]["active_id"] == "b"

    def test_delete_last_refused(self, monkeypatch, tmp_path):
        c, _envf = self._mount(monkeypatch, tmp_path)
        self._save(c, self._provider(id="only"))
        # 先删掉种子里的 base，再删 only → 409
        r1 = self._post(c, {"action": "delete", "id": "base"})
        d = r1.json()
        assert (
            self._post(
                c,
                {
                    "action": "delete",
                    "id": "base",
                    "confirm_token": d["confirm_token"],
                    "confirm_nonce": d["confirm_nonce"],
                    "totp_code": _totp(),
                },
            ).status_code
            == 200
        )
        r = self._post(c, {"action": "delete", "id": "only"})
        assert r.status_code == 409 and "last_provider" in r.json()["detail"]

    def test_test_action_uses_provider_probe(self, monkeypatch, tmp_path):
        c, _envf = self._mount(monkeypatch, tmp_path)
        self._save(c, self._provider(id="p1"))
        from plugins.qq_agent_adapter import model_switch

        seen = []

        async def fake(provider):
            seen.append(provider.id)
            return ""

        monkeypatch.setattr(model_switch, "provider_probe_detail", fake)
        r = self._post(c, {"action": "test", "id": "p1"})
        assert r.status_code == 200 and r.json()["ok"] is True
        assert seen == ["p1"]

    def test_endpoint_change_requires_new_key(self, monkeypatch, tmp_path):
        """H2：改端点但 key 留空 = 把旧 key 发到新端点，必须拒绝。"""
        c, _envf = self._mount(monkeypatch, tmp_path)
        self._save(c, self._provider(id="main"))
        r = self._post(
            c,
            {
                "action": "save",
                "provider": self._provider(
                    id="main", endpoint="https://evil.example/v1", api_key=""
                ),
            },
        )
        assert r.status_code == 409
        assert "provider_key_required_for_endpoint_change" in r.json()["detail"]

    def test_endpoint_change_with_new_key_allowed(self, monkeypatch, tmp_path):
        c, _envf = self._mount(monkeypatch, tmp_path)
        self._save(c, self._provider(id="main"))
        r = self._save(
            c,
            self._provider(
                id="main", endpoint="https://evil.example/v1", api_key="sk-new"
            ),
        )
        assert r.status_code == 200

    def test_protocol_change_requires_new_key(self, monkeypatch, tmp_path):
        c, _envf = self._mount(monkeypatch, tmp_path)
        self._save(c, self._provider(id="main"))
        r = self._post(
            c,
            {
                "action": "save",
                "provider": self._provider(id="main", protocol="anthropic", api_key=""),
            },
        )
        assert r.status_code == 409

    def test_test_action_is_audited(self, monkeypatch, tmp_path):
        c, _envf = self._mount(monkeypatch, tmp_path)
        self._save(c, self._provider(id="p1"))
        from agentcore.diagnostics import clear as _clear
        from agentcore.diagnostics import recent as _recent
        from plugins.qq_agent_adapter import model_switch

        async def fake(provider):
            return ""

        monkeypatch.setattr(model_switch, "provider_probe_detail", fake)
        _clear()
        try:
            r = self._post(c, {"action": "test", "id": "p1"})
            assert r.status_code == 200
            events = [e for e in _recent(5) if e["kind"] == "config_write"]
            assert any(e["key"] == "llm:test" for e in events), events
        finally:
            _clear()

    def test_llm_model_is_legacy_and_readonly_when_providers_exist(
        self, monkeypatch, tmp_path
    ):
        """M8：provider 模式下 LLM_MODEL 必须 409，且不回显值。"""
        c, _envf = self._mount(monkeypatch, tmp_path)
        data = c.get(f"{web.PREFIX}/api/settings").json()
        items = {i["key"]: i for cl in data["clusters"] for i in cl["items"]}
        assert items["LLM_MODEL"]["legacy"] is True
        assert items["LLM_MODEL"]["value"] is None
        r = TestSettingsWrite()._post(c, {"key": "LLM_MODEL", "value": "m-b"})
        assert r.status_code == 409
        assert r.json()["detail"] == "legacy_model_key"

    def test_llm_model_writable_without_providers(self, monkeypatch, tmp_path):
        c, _ = TestSettingsWrite()._mount_write(monkeypatch, tmp_path)
        r = TestSettingsWrite()._post(c, {"key": "LLM_MODEL", "value": "m-b"})
        assert r.status_code == 200 and r.json()["need_confirm"] is True

    def test_probe_failure_reported_without_changing_active(
        self, monkeypatch, tmp_path
    ):
        c, _envf = self._mount(monkeypatch, tmp_path)
        self._save(c, self._provider(id="p1"))
        from plugins.qq_agent_adapter import model_switch

        async def fake(provider):
            return "探测失败：HTTP 401"

        monkeypatch.setattr(model_switch, "provider_probe_detail", fake)
        r = self._post(c, {"action": "test", "id": "p1"})
        assert r.status_code == 200 and r.json()["ok"] is False
        assert "401" in r.json()["detail"]


class TestSearchProvidersWeb:
    """多搜索源 CRUD：key 只写不回显、两段确认、删除/排序/H2 凭据外带防护。"""

    PATH = f"{web.PREFIX}/api/settings/search/providers"

    def _mount(self, monkeypatch, tmp_path):
        # 隔离 search-providers.json 到 tmp，种一个 base 源（避免迁移不可预测）
        from agentcore.skills import search_providers as store

        monkeypatch.setenv(
            "AGENT_SEARCH_PROVIDERS_FILE", str(tmp_path / "search-providers.json")
        )
        store.save(
            store.SearchProviderConfig(
                providers=[store.SearchProvider("base", "Base", "bocha", "", "k-base")]
            )
        )
        return TestSettingsWrite()._mount_write(monkeypatch, tmp_path)

    def _post(self, c, body):
        return c.post(self.PATH, json=body)

    def _save(self, c, provider):
        r1 = self._post(c, {"action": "save", "provider": provider})
        assert r1.status_code == 200, r1.text
        data = r1.json()
        assert data.get("need_confirm") is True
        r2 = self._post(
            c,
            {
                "action": "save",
                "provider": provider,
                "confirm_token": data["confirm_token"],
                "confirm_nonce": data["confirm_nonce"],
                "totp_code": _totp(),
            },
        )
        assert r2.status_code == 200, r2.text
        return r2.json()

    def test_save_new_provider(self, monkeypatch, tmp_path):
        c, _ = self._mount(monkeypatch, tmp_path)
        data = self._save(c, {"name": "Tavily", "format": "tavily", "api_key": "sk-t"})
        assert data["ok"] is True
        ids = [p["id"] for p in data["providers"]]
        assert "base" in ids and len(ids) == 2
        assert all("api_key" not in p for p in data["providers"])  # key 不回显

    def test_save_edit_keeps_key(self, monkeypatch, tmp_path):
        """编辑但 key 留空 = 保留旧 key（不清空、不泄露）。"""
        c, _ = self._mount(monkeypatch, tmp_path)
        data = self._save(c, {"id": "base", "name": "Base2", "format": "bocha"})
        assert data["ok"] is True
        from agentcore.skills import search_providers as store

        assert store.load().by_id("base").api_key == "k-base"

    def test_endpoint_change_requires_key(self, monkeypatch, tmp_path):
        """H2：改端点但不提供 key = 拒（防旧 key 发到新端点）。"""
        c, _ = self._mount(monkeypatch, tmp_path)
        r = self._post(
            c,
            {
                "action": "save",
                "provider": {
                    "id": "base",
                    "name": "Base",
                    "format": "bocha",
                    "endpoint": "https://evil.example.com",
                },
            },
        )
        assert r.status_code == 409
        assert "provider_key_required" in r.json()["detail"]

    def test_delete_last_rejected(self, monkeypatch, tmp_path):
        """只剩一个源时删除被拒（last_provider 在确认前检查）。"""
        c, _ = self._mount(monkeypatch, tmp_path)
        r = self._post(c, {"action": "delete", "id": "base"})
        assert r.status_code == 409
        assert "last_provider" in r.json()["detail"]

    def test_reorder(self, monkeypatch, tmp_path):
        c, _ = self._mount(monkeypatch, tmp_path)
        self._save(c, {"name": "Tavily", "format": "tavily", "api_key": "sk-t"})
        r1 = self._post(c, {"action": "reorder", "ids": ["tavily", "base"]})
        assert r1.status_code == 200 and r1.json()["need_confirm"] is True
        r2 = self._post(
            c,
            {
                "action": "reorder",
                "ids": ["tavily", "base"],
                "confirm_token": r1.json()["confirm_token"],
                "confirm_nonce": r1.json()["confirm_nonce"],
                "totp_code": _totp(),
            },
        )
        assert r2.status_code == 200
        from agentcore.skills import search_providers as store

        assert [p.id for p in store.load().providers] == ["tavily", "base"]

    def test_probe_detail_hides_upstream_body(self, monkeypatch, tmp_path):
        """M5（REVIEW-1b16fa7..9c2d3c1）：test 连接只回状态码——不回显上游
        响应体（搜索 endpoint 完全 web 可配，回显体即半盲 SSRF 读原语；
        与 llm 侧 provider_probe_detail 的既定决策同口径）。"""
        import asyncio

        import httpx

        from agentcore.skills import search as s
        from agentcore.skills.search_providers import SearchProvider

        self._mount(monkeypatch, tmp_path)
        provider = SearchProvider(id="base", name="Base", format="bocha", api_key="k")

        async def fake_items_one(p, client, query, max_results, lang):
            raise httpx.HTTPStatusError(
                "401",
                request=httpx.Request("POST", "http://10.0.0.9/search"),
                response=httpx.Response(401, text='{"detail":"internal leak"}'),
            )

        monkeypatch.setattr(s, "_search_items_one", fake_items_one)
        out = asyncio.run(web._search_probe_detail(provider))
        assert "HTTP 401" in out
        assert "internal leak" not in out
        assert "10.0.0.9" not in out

    def test_settings_mark_degraded_on_corrupt_json(self, monkeypatch, tmp_path):
        """M9：search-providers.json 损坏时面板如实标记降级（运行态回落 env
        单源）——旧实现返回空列表装哑巴，管理员无从知道实际在跑什么。"""
        c, _ = self._mount(monkeypatch, tmp_path)
        (tmp_path / "search-providers.json").write_text("{bad json", encoding="utf-8")
        r = c.get(f"{web.PREFIX}/api/settings")
        assert r.status_code == 200
        search_cfg = r.json()["model_config"]["search"]
        assert search_cfg["degraded"] is True
        assert search_cfg["effective"] == "live"  # M6：providers 热读即时生效


class TestClusters:
    """设置簇：gate 排第一；gate 为关时同簇其余项前后端都拒写。"""

    def _mount(self, monkeypatch, tmp_path, env_text="AGENT_VISION=false\n"):
        envf = tmp_path / ".env"
        envf.write_text(env_text, encoding="utf-8")
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "agent:\n  extract_facts: false  # 事实抽取\n"
            "  memory_facts_top_k: 5\n"
            "  history_token_budget: 3000\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("AGENT_CONFIG", str(cfg))
        c, _ = TestSettingsWrite()._mount_write(monkeypatch, tmp_path)
        return c, envf, cfg

    def test_clusters_cover_schema_and_gate_first(self, monkeypatch, tmp_path):
        c, _envf, _cfg = self._mount(monkeypatch, tmp_path)
        d = c.get(f"{web.PREFIX}/api/settings").json()
        clusters = {x["id"]: x for x in d["clusters"]}
        assert len(clusters) == 17
        for cid, cl in clusters.items():
            if cl["gate_key"]:
                assert cl["items"][0]["key"] == cl["gate_key"], (
                    f"{cid} 的 gate 没排第一"
                )
        assert clusters["vision"]["gate_key"] == "AGENT_VISION"
        assert clusters["vision"]["gate_value"] is False
        assert clusters["memory"]["gate_key"] == "yaml:agent.extract_facts"
        assert clusters["memory"]["gate_value"] is False
        # 所有项都被某个簇覆盖
        total = sum(cl["total"] for cl in clusters.values())
        assert total == ((d["settings"]["total"] or 0) + (d["config"]["total"] or 0)), (
            "有设置项没归簇（或多算）"
        )

    def test_gate_off_rejects_sibling_env_key(self, monkeypatch, tmp_path):
        c, _envf, _cfg = self._mount(monkeypatch, tmp_path)
        r = TestSettingsWrite()._post(
            c, {"key": "AGENT_VISION_MAX_IMAGE_KB", "value": "1024"}
        )
        assert r.status_code == 409
        assert "cluster_disabled:AGENT_VISION" in r.json()["detail"]

    def test_gate_itself_still_writable(self, monkeypatch, tmp_path):
        c, _envf, _cfg = self._mount(monkeypatch, tmp_path)
        r = TestSettingsWrite()._post(c, {"key": "AGENT_VISION", "value": "true"})
        assert r.status_code == 200 and r.json()["need_confirm"] is True

    def test_gate_on_allows_sibling(self, monkeypatch, tmp_path):
        c, envf, _cfg = self._mount(monkeypatch, tmp_path)
        envf.write_text("AGENT_VISION=true\n", encoding="utf-8")
        r = TestSettingsWrite()._post(
            c, {"key": "AGENT_VISION_MAX_IMAGE_KB", "value": "1024"}
        )
        assert r.status_code == 200 and r.json()["need_confirm"] is True

    def test_yaml_gate_off_rejects_sibling(self, monkeypatch, tmp_path):
        c, _envf, _cfg = self._mount(monkeypatch, tmp_path)
        r = TestSettingsWrite()._post(
            c, {"key": "yaml:agent.memory_facts_top_k", "value": "8"}
        )
        assert r.status_code == 409
        assert "cluster_disabled:yaml:agent.extract_facts" in r.json()["detail"]

    def test_gate_parses_inline_comment(self, monkeypatch, tmp_path):
        """M7：`0  # 注释` 的实际生效值是 0，gate 不能当成开启。"""
        c, _envf, _cfg = self._mount(
            monkeypatch, tmp_path, env_text="AGENT_VISION=false  # 暂停\n"
        )
        r = TestSettingsWrite()._post(
            c, {"key": "AGENT_VISION_MAX_IMAGE_KB", "value": "1024"}
        )
        assert r.status_code == 409
        assert "cluster_disabled:AGENT_VISION" in r.json()["detail"]

    def test_gate_reads_file_not_stale_environ(self, monkeypatch, tmp_path):
        """gate 只看文件：进程里残留的旧值不能把已存盘的关闭状态放过去。"""
        c, _envf, _cfg = self._mount(monkeypatch, tmp_path)
        monkeypatch.setenv("AGENT_VISION", "true")  # 模拟旧进程 environ
        r = TestSettingsWrite()._post(
            c, {"key": "AGENT_VISION_MAX_IMAGE_KB", "value": "1024"}
        )
        assert r.status_code == 409


class TestSettingsWrite:
    """受控写入（D2-1）：门禁 / 白名单 / 两段确认 / 运行态同步 / 审计 / 回滚。"""

    WRITE = f"{web.PREFIX}/api/settings/write"
    PREVIEW = f"{web.PREFIX}/api/settings/preview"

    def _mount_write(self, monkeypatch, tmp_path, write="1", cidrs="10.0.0.0/8"):
        envf = tmp_path / ".env"
        envf.write_text("AGENT_VISION=false\n", encoding="utf-8")
        monkeypatch.setenv("AGENT_ENV_FILE", str(envf))
        monkeypatch.setenv("AGENT_WEB_BACKUP_DIR", str(tmp_path / "bk"))
        monkeypatch.setenv("AGENT_WEB_TOKEN", "t0ken")
        if cidrs is None:
            monkeypatch.delenv("AGENT_WEB_ALLOW_CIDRS", raising=False)
        else:
            monkeypatch.setenv("AGENT_WEB_ALLOW_CIDRS", cidrs)
        if write is None:
            monkeypatch.delenv("AGENT_WEB_WRITE", raising=False)
        else:
            monkeypatch.setenv("AGENT_WEB_WRITE", write)
        app = FastAPI()
        assert web.mount_web(app) is True
        # 源地址落在 CIDR 白名单内（TestClient 默认 "testclient" 解析不了 IP）
        c = TestClient(
            app, headers={"Authorization": "Bearer t0ken"}, client=("10.1.2.3", 5000)
        )
        return c, envf

    def _post(self, c, body):
        return c.post(self.WRITE, json=body)

    def test_gate_requires_both_write_and_cidrs(self, monkeypatch, tmp_path):
        # 无 AGENT_WEB_WRITE → 写路由不挂载
        c, _ = self._mount_write(monkeypatch, tmp_path, write=None)
        # 写路由是独立路径，未注册即 404
        assert (
            c.post(
                self.WRITE, json={"key": "AGENT_VISION", "value": "true"}
            ).status_code
            == 404
        )
        assert (
            c.post(
                self.PREVIEW, json={"key": "AGENT_VISION", "value": "true"}
            ).status_code
            == 404
        )
        # 有 AGENT_WEB_WRITE 但无 CIDR → 同样不挂载
        c, _ = self._mount_write(monkeypatch, tmp_path, cidrs=None)
        assert (
            c.post(
                self.WRITE, json={"key": "AGENT_VISION", "value": "true"}
            ).status_code
            == 404
        )

    def test_happy_path_two_phase(self, monkeypatch, tmp_path):
        class FakeBudget:
            daily_tokens = 0
            enforce = False

        import agentcore.budget as budget_mod

        fake = FakeBudget()
        monkeypatch.setattr(budget_mod, "get_budget", lambda: fake)
        c, envf = self._mount_write(monkeypatch, tmp_path)

        r1 = self._post(c, {"key": "AGENT_VISION", "value": "true"})
        assert r1.status_code == 200 and r1.json()["need_confirm"] is True
        assert r1.json()["old"] == "false" and r1.json()["new"] == "true"
        assert (
            envf.read_text(encoding="utf-8") == "AGENT_VISION=false\n"
        )  # 第一段不落盘

        token = r1.json()["confirm_token"]
        nonce = r1.json()["confirm_nonce"]
        r2 = self._post(
            c,
            {
                "key": "AGENT_VISION",
                "value": "true",
                "confirm_token": token,
                "confirm_nonce": nonce,
                "totp_code": _totp(),
            },
        )
        assert r2.status_code == 200 and r2.json()["ok"] is True
        assert r2.json()["effective"] == "live"
        assert envf.read_text(encoding="utf-8") == "AGENT_VISION=true\n"
        import os

        assert os.environ["AGENT_VISION"] == "true"
        # 备份文件存在
        bks = list((tmp_path / "bk").glob("*.bak"))
        assert len(bks) == 1
        # 审计事件
        from agentcore.diagnostics import recent as _recent

        assert any(
            e["kind"] == "config_write" and e["key"] == "AGENT_VISION"
            for e in _recent(5)
        )

    def test_search_embedding_credentials_now_writable(self):
        """凭据键（SEARCH_API_KEY / EMBEDDING_BASE_URL / EMBEDDING_API_KEY）从
        「走 SSH 不可写」放开为 web 可写（只写不回显），与 LLM provider 同源：
        在 WRITABLE 且仍 secret（值永不回显）。"""
        from plugins.qq_agent_adapter.config_write import WRITABLE
        from plugins.qq_agent_adapter.settings_spec import ENV_SPECS

        for key in ("SEARCH_API_KEY", "EMBEDDING_BASE_URL", "EMBEDDING_API_KEY"):
            assert key in WRITABLE, key
            spec = next(s for s in ENV_SPECS if s.key == key)
            assert spec.secret is True, key  # 仍不回显
            assert spec.writable is True, key

    def test_preview_never_echoes_secret_values(self, monkeypatch, tmp_path):
        """REVIEW-1b16fa7..9c2d3c1 H2 回归：preview 曾是零 2FA 的凭据外带通道
        ——secret 键只回 is_set 布尔，存量值（含非 sk- 前缀的 URL/key）永不进响应。"""
        c, envf = self._mount_write(monkeypatch, tmp_path)
        envf.write_text(
            "SEARCH_API_KEY=sk-live-secret-1\n"
            "SEARCH_BASE_URL=http://10.0.0.9:8080/v1\n"
            "EMBEDDING_BASE_URL=https://emb.internal.example.com/v1\n",
            encoding="utf-8",
        )
        for key, live in (
            ("SEARCH_API_KEY", "sk-live-secret-1"),
            ("SEARCH_BASE_URL", "http://10.0.0.9:8080/v1"),
            ("EMBEDDING_BASE_URL", "https://emb.internal.example.com/v1"),
        ):
            r = c.post(self.PREVIEW, json={"key": key, "value": "x"})
            assert r.status_code == 200, key
            assert live not in r.text, key
            j = r.json()
            assert j["current_file"] is True and j["current_runtime"] is True, key

    def test_write_first_stage_masks_non_sk_secret(self, monkeypatch, tmp_path):
        """写第一段的 old/new 对 secret 键一律掩码（旧口径只认 sk- 前缀，
        URL 类凭据原样漏进确认框 DOM）。"""
        c, envf = self._mount_write(monkeypatch, tmp_path)
        envf.write_text("SEARCH_BASE_URL=http://10.0.0.9:8080/v1\n", encoding="utf-8")
        r1 = self._post(
            c, {"key": "SEARCH_BASE_URL", "value": "https://new.example.com/v1"}
        )
        assert r1.status_code == 200 and r1.json()["need_confirm"] is True
        assert r1.json()["old"] == "***"
        assert r1.json()["new"] == "***"
        assert "10.0.0.9" not in r1.text
        assert "new.example.com" not in r1.text

    def test_write_search_embedding_credentials(self, monkeypatch, tmp_path):
        """两段确认写入这 3 个凭据键：落盘 + os.environ 同步 + boot 生效 +
        sk- 前缀 key 经 masked 完全掩码（确认框不回显真值）。"""
        import os

        c, envf = self._mount_write(monkeypatch, tmp_path)
        for key, value in (
            ("SEARCH_API_KEY", "sk-search-abc"),
            ("EMBEDDING_API_KEY", "sk-emb-abc"),
        ):
            r1 = self._post(c, {"key": key, "value": value})
            assert r1.status_code == 200 and r1.json()["need_confirm"] is True, key
            assert r1.json()["new"] == "***", key  # sk- 前缀 masked
            r2 = self._post(
                c,
                {
                    "key": key,
                    "value": value,
                    "confirm_token": r1.json()["confirm_token"],
                    "confirm_nonce": r1.json()["confirm_nonce"],
                    "totp_code": _totp(),
                },
            )
            assert r2.status_code == 200 and r2.json()["ok"] is True, key
            assert r2.json()["effective"] == "boot", key  # 改后重启生效
            assert os.environ[key] == value
        # URL 类（EMBEDDING_BASE_URL）：落盘 + os.environ 同步
        url = "https://emb.example.com/v1"
        r1 = self._post(c, {"key": "EMBEDDING_BASE_URL", "value": url})
        assert r1.status_code == 200 and r1.json()["need_confirm"] is True
        r2 = self._post(
            c,
            {
                "key": "EMBEDDING_BASE_URL",
                "value": url,
                "confirm_token": r1.json()["confirm_token"],
                "confirm_nonce": r1.json()["confirm_nonce"],
                "totp_code": _totp(),
            },
        )
        assert r2.status_code == 200 and r2.json()["ok"] is True
        assert os.environ["EMBEDDING_BASE_URL"] == url
        text = envf.read_text(encoding="utf-8")
        assert "SEARCH_API_KEY=sk-search-abc" in text
        assert "EMBEDDING_BASE_URL=https://emb.example.com/v1" in text
        assert "EMBEDDING_API_KEY=sk-emb-abc" in text

    def test_write_search_base_url(self, monkeypatch, tmp_path):
        """SEARCH_BASE_URL 可写：两段确认落盘 + os.environ 同步 + boot 生效。"""
        import os

        c, envf = self._mount_write(monkeypatch, tmp_path)
        url = "https://search-proxy.example.com/v1/web-search"
        r1 = self._post(c, {"key": "SEARCH_BASE_URL", "value": url})
        assert r1.status_code == 200 and r1.json()["need_confirm"] is True
        r2 = self._post(
            c,
            {
                "key": "SEARCH_BASE_URL",
                "value": url,
                "confirm_token": r1.json()["confirm_token"],
                "confirm_nonce": r1.json()["confirm_nonce"],
                "totp_code": _totp(),
            },
        )
        assert r2.status_code == 200 and r2.json()["ok"] is True
        assert r2.json()["effective"] == "boot"
        assert os.environ["SEARCH_BASE_URL"] == url
        assert f"SEARCH_BASE_URL={url}" in envf.read_text(encoding="utf-8")

    def test_confirm_token_reuse_rejected(self, monkeypatch, tmp_path):
        c, _ = self._mount_write(monkeypatch, tmp_path)
        r1 = self._post(c, {"key": "AGENT_VISION", "value": "true"}).json()
        payload = {
            "key": "AGENT_VISION",
            "value": "true",
            "confirm_token": r1["confirm_token"],
            "confirm_nonce": r1["confirm_nonce"],
            "totp_code": _totp(),
        }
        r2 = self._post(c, payload)
        assert r2.status_code == 200
        r3 = self._post(c, payload)
        assert r3.status_code == 400 and "reuse" in r3.json()["detail"]

    def test_token_bound_to_value(self, monkeypatch, tmp_path):
        """确认码绑定 key+值：拿 A 值的码去写 B 值必须被拒。"""
        c, envf = self._mount_write(monkeypatch, tmp_path)
        r1 = self._post(c, {"key": "AGENT_VISION", "value": "true"}).json()
        r2 = self._post(
            c,
            {
                "key": "AGENT_VISION",
                "value": "false",
                "confirm_token": r1["confirm_token"],
                "confirm_nonce": r1["confirm_nonce"],
                "totp_code": _totp(),
            },
        )
        assert r2.status_code == 400
        assert envf.read_text(encoding="utf-8") == "AGENT_VISION=false\n"  # 未被改

    def test_forbidden_key_403(self, monkeypatch, tmp_path):
        c, _ = self._mount_write(monkeypatch, tmp_path)
        r = self._post(c, {"key": "LLM_API_KEY", "value": "sk-x"})
        assert r.status_code == 403 and "key_not_writable" in r.json()["detail"]

    @pytest.mark.parametrize(
        "key",
        [
            "AGENT_PERMISSION_LEVEL",  # 权限级别：提权入口
            "SUPERUSERS",  # 管理员名单
            "AGENT_WEB_TOKEN",  # web 面凭据本身
            "AGENT_WEB_WRITE",  # 写面总开关（自我提权）
            "AGENT_WEB_ALLOW_CIDRS",  # 源 IP 白名单（自我扩面）
            "AGENT_ENV_FILE",  # 改目标文件=改任意键
            "BLOCKED_USERS",  # 黑名单
            "DATABASE_URL",  # 连接串/凭据
            "AGENT_SCHEDULER_TZ",  # 统一日键口径
        ],
    )
    def test_permission_keys_not_writable(self, monkeypatch, tmp_path, key):
        """L6（REVIEW-de09478..workdir）：权限/凭据/门禁类键**一律不可写**。

        web 写面是唯一可改 .env 的通道——AGENT_PERMISSION_LEVEL/SUPERUSERS/
        TOKEN 等键若可写，等于把权限体系的自助提权口开着。既有
        test_forbidden_key_403 只覆盖 LLM_API_KEY 一例，这里参数化补负面清单。
        """
        c, _ = self._mount_write(monkeypatch, tmp_path)
        r = self._post(c, {"key": key, "value": "high"})
        assert r.status_code == 403, f"{key} 不得可写"
        assert "key_not_writable" in r.json()["detail"]

    def test_retention_and_ssrf_keys_are_readonly(self):
        """L22：数据保留期与图片/音频域名白名单不可 web 写。"""
        from plugins.qq_agent_adapter.config_write import WRITABLE

        for key in (
            "AGENT_GROUP_STATS_RETENTION_DAYS",
            "AGENT_IMAGE_HOSTS",
            "AGENT_MUSIC_AUDIO_HOSTS",
        ):
            assert key not in WRITABLE, key

    def test_schema_covers_env_example_and_invariants(self):
        """schema 是单一事实源：.env.example 的每个键都必须在册。

        这条是防漂移的主闸：新增 env 却忘了登记，设置页就永远看不到它。
        """
        import re
        from pathlib import Path

        from plugins.qq_agent_adapter.config_write import WRITABLE
        from plugins.qq_agent_adapter.settings_spec import ENV_SPECS

        text = (Path(__file__).resolve().parents[1] / ".env.example").read_text(
            encoding="utf-8"
        )
        documented = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", text, flags=re.MULTILINE))
        assert documented == {s.key for s in ENV_SPECS}, documented ^ {
            s.key for s in ENV_SPECS
        }
        assert set(WRITABLE) == {s.key for s in ENV_SPECS if s.writable}
        # 凭据 / 安全边界 / 凭据载体一律不可写
        for key in (
            "LLM_API_KEY",
            "LLM_MODELS",  # 条目里可内联 key
            "AGENT_WEB_TOKEN",
            "AGENT_WEB_WRITE",
            "SUPERUSERS",
            "ALLOWED_GROUPS",
            "AGENT_PERMISSION_LEVEL",
            "DATABASE_URL",
            "AGENT_ENV_FILE",
        ):
            assert key not in WRITABLE, f"{key} 不得可写"
        # 原有 8 键必须仍在（回归：扩表不得挤掉已上线的写入面）
        for key in (
            "AGENT_VISION",
            "AGENT_GROUP_CONTEXT",
            "AGENT_GROUP_CONTEXT_LINES",
            "AGENT_GROUP_CONTEXT_TTL",
            "AGENT_WAKE_WORDS",
            "AGENT_BUDGET_DAILY_TOKENS",
            "AGENT_BUDGET_ENFORCE",
            "LLM_MODEL",
        ):
            assert key in WRITABLE, f"{key} 被误删"
        for spec in WRITABLE.values():
            assert spec.kind in {"bool", "int", "float", "csv", "str"}

    def test_write_panel_is_schema_driven(self):
        """页面不再手抄白名单：列表由 /api/settings 的 schema 渲染。"""
        assert "indexSettings" in web._PAGE
        # 簇导航 + 单簇 panel（一次只看一个簇，避免长列表）
        assert "initClusters" in web._PAGE
        assert "renderClusterPanel" in web._PAGE
        assert "selectCluster" in web._PAGE
        assert "filterSettings" in web._PAGE
        assert "const WRITABLE = [" not in web._PAGE, "旧的硬编码白名单应已移除"
        # 灰置分支必须真的存在（静态标记；JS 行为由临时实例截图复核）
        assert "簇已关闭" in web._PAGE and "is-locked" in web._PAGE
        assert "clusterBody" in web._PAGE and "cnav" in web._PAGE
        # LLM provider 面板挂在顶层「模型」标签（renderModelView → providerPanel），
        # 设置 llm 簇只留跳转指引（2026-10-20 从簇内搬出，防双入口漂移）
        assert 'c.id === "llm"' in web._PAGE and "前往模型" in web._PAGE
        assert 'c.id === "llm" ? providerPanel(lastSettings)' not in web._PAGE
        assert "function renderModelView" in web._PAGE
        assert "providerPanel(d)" in web._PAGE
        assert 'setView("model")' in web._PAGE.replace(
            "setView(\\'model\\')", 'setView("model")'
        )
        assert '"model", "Model", "modelView"' in web._PAGE
        # 未设置时预选默认值（否则温度默认 0.7 会显示成下拉第一项 0）
        assert 'raw !== "" ? raw : String(it.default' in web._PAGE

    def test_invalid_value_422(self, monkeypatch, tmp_path):
        c, _ = self._mount_write(monkeypatch, tmp_path)
        r = self._post(c, {"key": "AGENT_GROUP_CONTEXT_LINES", "value": "0"})
        assert r.status_code == 422
        r = self._post(
            c,
            {
                "key": "AGENT_WAKE_WORDS",
                "value": "a\nAGENT_WEB_TOKEN=pwned",
            },
        )
        assert r.status_code == 422 and "forbidden" in r.json()["detail"]

    def test_budget_keys_double_write(self, monkeypatch, tmp_path):
        class FakeBudget:
            daily_tokens = 0
            enforce = False

        import agentcore.budget as budget_mod

        fake = FakeBudget()
        monkeypatch.setattr(budget_mod, "get_budget", lambda: fake)
        c, envf = self._mount_write(monkeypatch, tmp_path)
        envf.write_text("AGENT_BUDGET_ENFORCE=false\n", encoding="utf-8")
        r1 = self._post(
            c, {"key": "AGENT_BUDGET_DAILY_TOKENS", "value": "50000"}
        ).json()
        r2 = self._post(
            c,
            {
                "key": "AGENT_BUDGET_DAILY_TOKENS",
                "value": "50000",
                "confirm_token": r1["confirm_token"],
                "confirm_nonce": r1["confirm_nonce"],
                "totp_code": _totp(),
            },
        )
        assert r2.status_code == 200
        assert fake.daily_tokens == 50000
        assert "AGENT_BUDGET_DAILY_TOKENS=50000" in envf.read_text(encoding="utf-8")

    def test_verify_failure_rolls_back(self, monkeypatch, tmp_path):
        """写后验证失败：文件必须被还原 + 留 rollback 审计。"""
        from agentcore.diagnostics import clear as _clear

        c, envf = self._mount_write(monkeypatch, tmp_path)
        original = envf.read_text(encoding="utf-8")
        r1 = self._post(c, {"key": "AGENT_VISION", "value": "true"}).json()
        _clear()
        try:
            monkeypatch.setattr(web.cw, "verify_env", lambda *a, **kw: False)
            r2 = self._post(
                c,
                {
                    "key": "AGENT_VISION",
                    "value": "true",
                    "confirm_token": r1["confirm_token"],
                    "confirm_nonce": r1["confirm_nonce"],
                    "totp_code": _totp(),
                },
            )
            assert r2.status_code == 500 and "rolled_back" in r2.json()["detail"]
            assert envf.read_text(encoding="utf-8") == original
            from agentcore.diagnostics import recent as _recent

            assert any(e["kind"] == "config_write_rollback" for e in _recent(5))
        finally:
            _clear()

    def test_yaml_write_roundtrip_and_boot_label(self, monkeypatch, tmp_path):
        """config.yaml 与 .env 共用两段确认；保存后标 boot 且注释保留。"""
        cfg = tmp_path / "config.yaml"
        cfg.write_text("agent:\n  max_iterations: 8  # 工具轮数\n", encoding="utf-8")
        monkeypatch.setenv("AGENT_CONFIG", str(cfg))
        c, _ = self._mount_write(monkeypatch, tmp_path)
        r1 = self._post(c, {"key": "yaml:agent.max_iterations", "value": "10"})
        assert r1.status_code == 200 and r1.json()["need_confirm"] is True
        assert r1.json()["effective"] == "boot"
        r2 = self._post(
            c,
            {
                "key": "yaml:agent.max_iterations",
                "value": "10",
                "confirm_token": r1.json()["confirm_token"],
                "confirm_nonce": r1.json()["confirm_nonce"],
                "totp_code": _totp(),
            },
        )
        assert r2.status_code == 200 and r2.json()["ok"] is True
        assert r2.json()["scope"] == "yaml" and r2.json()["effective"] == "boot"
        text = cfg.read_text(encoding="utf-8")
        assert "max_iterations: 10" in text and "# 工具轮数" in text

    def test_yaml_readonly_path_403(self, monkeypatch, tmp_path):
        c, _ = self._mount_write(monkeypatch, tmp_path)
        r = self._post(c, {"key": "yaml:archive.keep_days", "value": "1"})
        assert r.status_code == 403
        assert "yaml_key_not_writable" in r.json()["detail"]

    def test_yaml_bad_value_422(self, monkeypatch, tmp_path):
        c, _ = self._mount_write(monkeypatch, tmp_path)
        r = self._post(c, {"key": "yaml:agent.max_iterations", "value": "abc"})
        assert r.status_code == 422 and "expect_number" in r.json()["detail"]

    def test_settings_payload_carries_write_enabled(self, monkeypatch, tmp_path):
        c, _ = self._mount_write(monkeypatch, tmp_path)
        d = c.get(f"{web.PREFIX}/api/settings").json()
        assert d["write_enabled"] is True
        c2, _ = self._mount_write(monkeypatch, tmp_path, write=None)
        d2 = c2.get(f"{web.PREFIX}/api/settings").json()
        assert d2["write_enabled"] is False

    # ---- LLM_MODEL：运行期切模型（探测闸门 + 运行态同步）----

    def _mount_model(self, monkeypatch, tmp_path, probe_detail=""):
        """挂载写面，桩掉探测与共享 LLMClient。

        返回 ``(client, envf, fake_llm, probes)``：probes 记录探测次数——
        第一段（发确认码）不许探测，第二段（落盘）必须探测一次。
        """
        import agentcore.skills.registry  # noqa: F401  # 单跑时确保已导入（patch 目标）
        from plugins.qq_agent_adapter import model_switch

        class FakeLLM:
            def __init__(self):
                self.active = None

            def set_active(self, entry):
                self.active = entry

            @staticmethod
            def resolve_entry(entry):
                # 裸名对回池条目：单测里池为空，原样返回即可
                return entry

        fake = FakeLLM()
        # 不能按字符串路径 patch：agentcore.skills 包把单例导出为同名属性
        # ``registry``，monkeypatch 的 derive_importpath 会解析到那个实例上
        import sys

        monkeypatch.setattr(
            sys.modules["agentcore.skills.registry"],
            "get_shared_llm_client",
            lambda: fake,
        )
        probes: list = []

        async def fake_probe(entry):
            probes.append(entry)
            return probe_detail

        monkeypatch.setattr(model_switch, "probe_failure_detail", fake_probe)
        c, envf = self._mount_write(monkeypatch, tmp_path)
        return c, envf, fake, probes

    def test_llm_model_write_hot_applies(self, monkeypatch, tmp_path):
        """写 LLM_MODEL：落盘 + 同步共享客户端的活跃位（重启后仍一致）。"""
        c, envf, fake, probes = self._mount_model(monkeypatch, tmp_path)
        r1 = self._post(c, {"key": "LLM_MODEL", "value": "step-3"})
        assert r1.status_code == 200 and r1.json()["need_confirm"] is True
        assert probes == [], "第一段只发确认码，不该探测"
        r2 = self._post(
            c,
            {
                "key": "LLM_MODEL",
                "value": "step-3",
                "confirm_token": r1.json()["confirm_token"],
                "confirm_nonce": r1.json()["confirm_nonce"],
                "totp_code": _totp(),
            },
        )
        assert r2.status_code == 200 and r2.json()["ok"] is True
        assert "LLM_MODEL=step-3" in envf.read_text(encoding="utf-8")
        assert fake.active is not None and fake.active.model == "step-3"
        assert len(probes) == 1, "第二段落盘前必须探测一次"

    def test_llm_model_rejects_key_in_value(self, monkeypatch, tmp_path):
        """活跃位不接受 key：值会进审计与回执，key 只属于 LLM_MODELS 池条目。"""
        c, _envf, _fake, probes = self._mount_model(monkeypatch, tmp_path)
        r = self._post(
            c, {"key": "LLM_MODEL", "value": "step-3@https://api.x.com/v1|sk-leak"}
        )
        assert r.status_code == 422 and "bad_model_spec" in r.json()["detail"]
        assert probes == []

    def test_llm_model_probe_failure_blocks_write(self, monkeypatch, tmp_path):
        """探测不过就不落盘：写坏一个模型名会让之后每轮对话都失败。"""
        c, envf, _fake, probes = self._mount_model(
            monkeypatch, tmp_path, probe_detail="探测失败：HTTP 404 model not found"
        )
        original = envf.read_text(encoding="utf-8")
        r1 = self._post(c, {"key": "LLM_MODEL", "value": "typo-model"}).json()
        r2 = self._post(
            c,
            {
                "key": "LLM_MODEL",
                "value": "typo-model",
                "confirm_token": r1["confirm_token"],
                "confirm_nonce": r1["confirm_nonce"],
                "totp_code": _totp(),
            },
        )
        assert r2.status_code == 409 and "404" in r2.json()["detail"]
        assert envf.read_text(encoding="utf-8") == original, "探测失败不得落盘"
        assert len(probes) == 1

    def test_llm_model_probe_not_run_without_valid_totp(self, monkeypatch, tmp_path):
        """REVIEW-1b16fa7..9c2d3c1 H3 回归：探测在 2FA 之后——无有效 TOTP 码时
        一次探测都不许发生（旧实现探测在 _consume_confirm 之前，持 web token 即可
        让 bot 携主 key POST 任意 model@http://attacker 端点）。"""
        c, _envf, _fake, probes = self._mount_model(monkeypatch, tmp_path)
        r1 = self._post(
            c, {"key": "LLM_MODEL", "value": "m@http://attacker.tld/v1"}
        ).json()
        r2 = self._post(
            c,
            {
                "key": "LLM_MODEL",
                "value": "m@http://attacker.tld/v1",
                "confirm_token": r1["confirm_token"],
                "confirm_nonce": r1["confirm_nonce"],
                "totp_code": "000000",
            },
        )
        assert r2.status_code == 400 and r2.json()["detail"] == "totp_invalid"
        assert probes == [], "无有效 2FA 码不许探测"

    def test_llm_model_probe_failure_refunds_nonce(self, monkeypatch, tmp_path):
        """探测失败退还 nonce：管理员改名后可用同一 challenge 重试
        （原「探测在前，失败时码还有效」的诉求以退还确认凭证实现）。"""
        c, _envf, _fake, probes = self._mount_model(
            monkeypatch, tmp_path, probe_detail="探测失败：HTTP 404"
        )
        r1 = self._post(c, {"key": "LLM_MODEL", "value": "typo-model"}).json()
        body = {
            "key": "LLM_MODEL",
            "value": "typo-model",
            "confirm_token": r1["confirm_token"],
            "confirm_nonce": r1["confirm_nonce"],
            "totp_code": _totp(),
        }
        r2 = self._post(c, body)
        assert r2.status_code == 409
        assert len(probes) == 1
        r3 = self._post(c, body)  # 同一 nonce 重试：探测仍失败，但不得 confirm_reused
        assert r3.status_code == 409
        assert "confirm_reused" not in r3.text
        assert len(probes) == 2

    def test_write_face_not_mounted_without_gate(self, monkeypatch, tmp_path):
        c, _ = self._mount_write(monkeypatch, tmp_path, write=None)
        paths = {getattr(r, "path", "") for r in c.app.routes}
        assert f"{web.PREFIX}/api/settings/write" not in paths
        assert f"{web.PREFIX}/api/settings" in paths  # 读面不受影响

    def test_write_panel_wired_in_page(self):
        assert "editKey" in web._PAGE
        assert "api/settings/write" in web._PAGE
        assert "write_enabled" in web._PAGE
        assert "rebootBot" in web._PAGE

    def test_duplicate_key_lines_409(self, monkeypatch, tmp_path):
        """.env 同键重复：合法请求撞上脏文件 → 409 让管理员手工处理，而非 500。

        线上探针实锤（2026-09-29 审查）：裸异常直接穿透路由。
        """
        c, envf = self._mount_write(monkeypatch, tmp_path)
        envf.write_text(
            "AGENT_VISION=false\nOTHER=1\nAGENT_VISION=false\n", encoding="utf-8"
        )
        original = envf.read_text(encoding="utf-8")
        r1 = self._post(c, {"key": "AGENT_VISION", "value": "true"})
        assert r1.status_code == 200  # 挑战段不读写盘，正常发码
        r2 = self._post(
            c,
            {
                "key": "AGENT_VISION",
                "value": "true",
                "confirm_token": r1.json()["confirm_token"],
                "confirm_nonce": r1.json()["confirm_nonce"],
                "totp_code": _totp(),
            },
        )
        assert r2.status_code == 409 and "duplicate" in r2.json()["detail"]
        assert envf.read_text(encoding="utf-8") == original  # 未被改动

    def test_missing_env_file_clear_500(self, monkeypatch, tmp_path):
        """.env 不存在：两段都给可行动的 env_file_missing，而非裸 FileNotFoundError。"""
        c, envf = self._mount_write(monkeypatch, tmp_path)
        envf.unlink()
        r1 = self._post(c, {"key": "AGENT_VISION", "value": "true"})
        assert r1.status_code == 200  # 挑战段旧值展示降级为"未设置"
        assert r1.json()["old"] is None
        r2 = self._post(
            c,
            {
                "key": "AGENT_VISION",
                "value": "true",
                "confirm_token": r1.json()["confirm_token"],
                "confirm_nonce": r1.json()["confirm_nonce"],
                "totp_code": _totp(),
            },
        )
        assert r2.status_code == 500 and r2.json()["detail"] == "env_file_missing"

    def test_crlf_env_file_bytes_preserved(self, monkeypatch, tmp_path):
        """CRLF .env：只改目标行，全文件其余字节（含 \\r\\n）原样保留。

        审查探针实锤（2026-09-29）：文本模式通用换行翻译会把整个文件静默转 LF。
        纯函数层测得到 patch，测不到 read/IO 翻译——必须在 HTTP 全路径上钉住。
        """
        c, envf = self._mount_write(monkeypatch, tmp_path)
        envf.write_bytes(b"AGENT_VISION=false\r\nOTHER=1\r\n")
        r1 = self._post(c, {"key": "AGENT_VISION", "value": "true"})
        r2 = self._post(
            c,
            {
                "key": "AGENT_VISION",
                "value": "true",
                "confirm_token": r1.json()["confirm_token"],
                "confirm_nonce": r1.json()["confirm_nonce"],
                "totp_code": _totp(),
            },
        )
        assert r2.status_code == 200
        assert envf.read_bytes() == b"AGENT_VISION=true\r\nOTHER=1\r\n"

    # ---- REVIEW-26fec4d..3ce6e0a M1–M10 修复回归 ----
    def test_m1_confirm_single_use(self, monkeypatch, tmp_path):
        """确认 challenge 单次有效：同一 nonce 第二次到达 = confirm_reused。

        2FA 后 challenge 不绑时间窗口（时间由 TOTP 码管），这里锁 nonce 防重放。
        """
        monkeypatch.setenv("AGENT_2FA_SECRET_FILE", str(tmp_path / "2fa.json"))
        from agentcore import totp

        totp_code = totp.current_code(totp.load_or_create_secret())
        nonce = "n1"
        token = web._confirm_token(nonce, "AGENT_VISION", "true")
        assert (
            web._consume_confirm(nonce, token, "AGENT_VISION", "true", totp_code)
            is None
        )
        # 同一 nonce 第二次：拒绝（重放）
        assert (
            web._consume_confirm(nonce, token, "AGENT_VISION", "true", totp_code)
            == "confirm_reused"
        )
        # 别的 nonce（新挑战）不受影响
        token2 = web._confirm_token("n2", "AGENT_VISION", "true")
        assert (
            web._consume_confirm("n2", token2, "AGENT_VISION", "true", totp_code)
            is None
        )
        assert all(isinstance(k, str) for k in web._used_confirms)

    # ---- REVIEW-1b16fa7..9c2d3c1 H4：challenge 时效 + 失败限速 ----
    def test_h4_challenge_expires_after_window(self, monkeypatch, tmp_path):
        """challenge 30s 时效：过期的 (nonce, token) 即使配正确 TOTP 码也拒绝。

        旧实现 challenge 无任何时间成分——持 token 者固定一个 challenge 就能
        无限换 6 位码爆破（±1 窗 = 3 有效码/10⁶）。"""
        monkeypatch.setenv("AGENT_2FA_SECRET_FILE", str(tmp_path / "2fa.json"))
        from agentcore import totp

        totp_code = totp.current_code(totp.load_or_create_secret())
        nonce = "exp1"
        stale = web._confirm_token(
            nonce, "AGENT_VISION", "true", ts=int(time.time()) - 60
        )
        assert (
            web._consume_confirm(nonce, stale, "AGENT_VISION", "true", totp_code)
            == "confirm_invalid_or_expired"
        )
        # 窗口内刚签发的正常消费
        fresh = web._confirm_token(nonce, "AGENT_VISION", "true")
        assert (
            web._consume_confirm(nonce, fresh, "AGENT_VISION", "true", totp_code)
            is None
        )

    def test_h4_failed_totp_locks_after_limit(self, monkeypatch, tmp_path):
        """同源 IP 连续失败 5 次即锁：第 6 次连正确码也 too_many_attempts。

        在线爆破面收口；成功后该 IP 计数清零（管理员手滑输错不背锅）。"""
        monkeypatch.setenv("AGENT_2FA_SECRET_FILE", str(tmp_path / "2fa.json"))
        from agentcore import totp

        good = totp.current_code(totp.load_or_create_secret())
        # 专用 IP：_mount_write 的 TestClient 默认源是 10.1.2.3，别把它的
        # 失败计数桶污染了（跨用例共享 _confirm_fails 模块态）
        ip = "10.77.77.77"
        for i in range(web._CONFIRM_FAIL_LIMIT):
            nonce = f"f{i}"
            token = web._confirm_token(nonce, "AGENT_VISION", "true")
            assert (
                web._consume_confirm(nonce, token, "AGENT_VISION", "true", "000000", ip)
                == "totp_invalid"
            )
        nonce = "locked"
        token = web._confirm_token(nonce, "AGENT_VISION", "true")
        assert (
            web._consume_confirm(nonce, token, "AGENT_VISION", "true", good, ip)
            == "too_many_attempts"
        )
        # 别的源 IP 不受这把锁影响
        token2 = web._confirm_token("other-ip", "AGENT_VISION", "true")
        assert (
            web._consume_confirm(
                "other-ip", token2, "AGENT_VISION", "true", good, "10.9.9.9"
            )
            is None
        )
        # 成功后计数清零：连错 4 次不该锁（第 5 次才锁）
        ip2 = "10.4.5.6"
        for i in range(4):
            nonce = f"c{i}"
            token = web._confirm_token(nonce, "AGENT_VISION", "true")
            assert (
                web._consume_confirm(
                    nonce, token, "AGENT_VISION", "true", "000000", ip2
                )
                == "totp_invalid"
            )
        token_ok = web._confirm_token("ok", "AGENT_VISION", "true")
        assert (
            web._consume_confirm("ok", token_ok, "AGENT_VISION", "true", good, ip2)
            is None
        )
        for i in range(4):
            nonce = f"d{i}"
            token = web._confirm_token(nonce, "AGENT_VISION", "true")
            assert (
                web._consume_confirm(
                    nonce, token, "AGENT_VISION", "true", "000000", ip2
                )
                == "totp_invalid"
            )

    def test_m5_non_ascii_bearer_401(self, monkeypatch, tmp_path):
        """M5：Authorization 头含非 ASCII（latin-1 可编码）必须 401，而不是 500。"""
        app = _mount(monkeypatch)
        # bytes 头：httpx 的 str header 按 ASCII 编码，发不出非 ASCII 字节；
        # 服务端按 latin-1 解码后 issued 到 compare_digest 的正是非 ASCII str
        r = TestClient(app, headers={"Authorization": b"Bearer caf\xe9"}).get(
            f"{web.PREFIX}/api/overview"
        )
        assert r.status_code == 401, r.text[:120]

    def test_m5_non_ascii_confirm_400(self, monkeypatch, tmp_path):
        """M5：confirm_token 非 ASCII 必须 400（旧实现 compare_digest TypeError→500）。"""
        c, _ = self._mount_write(monkeypatch, tmp_path)
        r = self._post(
            c,
            {
                "key": "AGENT_VISION",
                "value": "true",
                "confirm_token": " caf\u00e9 "[:5],
                "confirm_nonce": "abcd1234",
                "totp_code": _totp(),
            },
        )
        assert r.status_code == 400
        assert "confirm" in r.json()["detail"]

    def test_m4_symlink_env_409(self, monkeypatch, tmp_path, symlinks_supported):
        """M4：AGENT_ENV_FILE 为 symlink 时写面 409 拒写（旧实现静默替换链接）。"""
        c, envf = self._mount_write(monkeypatch, tmp_path)
        real = tmp_path / "real.env"
        real.write_text("AGENT_VISION=false\n", encoding="utf-8")
        envf.unlink()
        envf.symlink_to(real)
        r1 = self._post(c, {"key": "AGENT_VISION", "value": "true"})
        r2 = self._post(
            c,
            {
                "key": "AGENT_VISION",
                "value": "true",
                "confirm_token": r1.json()["confirm_token"],
                "confirm_nonce": r1.json()["confirm_nonce"],
                "totp_code": _totp(),
            },
        )
        assert r2.status_code == 409 and "symlink" in r2.json()["detail"]
        assert envf.is_symlink()  # 链接本体未被替换
        assert "false" in real.read_text(encoding="utf-8")  # 真实目标未被改

    def test_m10_audit_records_source_ip(self, monkeypatch, tmp_path):
        """M10：config_write 审计必须带 source_ip（定稿 who 的落实）。"""
        from agentcore.diagnostics import clear as _clear
        from agentcore.diagnostics import recent as _recent

        c, _ = self._mount_write(monkeypatch, tmp_path)
        _clear()
        try:
            r1 = self._post(c, {"key": "AGENT_VISION", "value": "true"})
            self._post(
                c,
                {
                    "key": "AGENT_VISION",
                    "value": "true",
                    "confirm_token": r1.json()["confirm_token"],
                    "confirm_nonce": r1.json()["confirm_nonce"],
                    "totp_code": _totp(),
                },
            )
            ev = [e for e in _recent(5) if e["kind"] == "config_write"][0]
            assert ev["source_ip"] == "10.1.2.3"
            assert ev["confirm_nonce"] == r1.json()["confirm_nonce"]
        finally:
            _clear()

    def test_no_native_dialogs(self):
        """二次确认全部走页内模态框，不再用浏览器原生 confirm/prompt。"""
        assert "!confirm(" not in web._PAGE
        assert "prompt(" not in web._PAGE
        assert "function confirmDialog" in web._PAGE
        assert "confirmDialog(" in web._PAGE

    def test_m6_m7_page_js_fixed(self):
        """M6/M7 页面护栏：编辑二段必须带 nonce；下载不得再用 location.href。"""
        import re

        edit = re.search(r"function editKey\(key.*?\n}", web._PAGE, re.DOTALL)
        assert edit, "editKey 不在页面里"
        # 二段确认现在在 writeSetting（editKey 只负责弹表单）
        write = re.search(
            r"async function writeSetting\(key, value\).*?\n}", web._PAGE, re.DOTALL
        )
        assert write, "writeSetting 不在页面里"
        assert "confirm_nonce: res.data.confirm_nonce" in write.group(0)
        assert 'location.href = "api/logs/download' not in web._PAGE
        assert "URL.createObjectURL" in web._PAGE
        assert "confirm_nonce" in web._PAGE


class TestGroupStatsView:
    """群发言统计只读面：门禁 / 结构 / 无正文。

    来源：群发言榜+词云 AC 计划 §5-F。与其它 api 面同门禁（Bearer + CIDR）；
    响应只含聚合数据（条数/名片快照/词频/小时直方图）。
    """

    def _seed(self):
        import asyncio

        from agentcore.groupstats import ranking
        from agentcore.groupstats.store import InMemoryStatsStore

        store = InMemoryStatsStore()
        start, end = ranking.previous_week()
        asyncio.run(
            store.upsert_daily(
                [
                    {
                        "group_id": "456",
                        "user_id": f"u{i}",
                        "day": start,
                        "nickname": f"群友{i}",
                        "msgs": 10 - i,
                        "chars": 100,
                        "hours": {19: 5 - i},
                    }
                    for i in range(6)
                ]
            )
        )
        asyncio.run(
            store.upsert_words(
                [
                    {"group_id": "456", "day": start, "word": "服务器", "count": 9},
                    {"group_id": "456", "day": start, "word": "显卡", "count": 4},
                ]
            )
        )
        # 本周（进行中）：同群 + 一个只在本周出现的新群
        cur_start, _cur_end = ranking.current_week()
        asyncio.run(
            store.upsert_daily(
                [
                    {
                        "group_id": "456",
                        "user_id": "cu1",
                        "day": cur_start,
                        "nickname": "本周群友",
                        "msgs": 3,
                        "chars": 30,
                        "hours": {20: 3},
                    },
                    {
                        "group_id": "789",
                        "user_id": "nu1",
                        "day": cur_start,
                        "nickname": "新群群友",
                        "msgs": 2,
                        "chars": 20,
                        "hours": {21: 2},
                    },
                ]
            )
        )
        asyncio.run(
            store.upsert_words(
                [{"group_id": "456", "day": cur_start, "word": "本周词", "count": 2}]
            )
        )
        return store

    def test_endpoint_requires_token(self, monkeypatch):
        app = FastAPI()
        _mount(monkeypatch)
        assert web.mount_web(app) is True
        r = TestClient(app).get(f"{web.PREFIX}/api/group-stats")
        assert r.status_code == 401

    def test_endpoint_wrong_token_401(self, monkeypatch):
        app = FastAPI()
        _mount(monkeypatch)
        assert web.mount_web(app) is True
        r = TestClient(app, headers={"Authorization": "Bearer nope"}).get(
            f"{web.PREFIX}/api/group-stats"
        )
        assert r.status_code == 401

    def test_disabled_by_default(self, monkeypatch):
        app = FastAPI()
        _mount(monkeypatch)
        assert web.mount_web(app) is True
        c, _ = _client(app)
        r = c.get(f"{web.PREFIX}/api/group-stats")
        assert r.status_code == 200
        d = r.json()
        assert d["enabled"] is False
        assert d["groups"] == []

    def test_enabled_with_store(self, monkeypatch):
        app = FastAPI()
        _mount(monkeypatch)
        assert web.mount_web(app) is True
        monkeypatch.setenv("AGENT_GROUP_STATS", "1")
        store = self._seed()
        import types

        monkeypatch.setattr(
            "nonebot.get_driver",
            lambda: types.SimpleNamespace(_agent_group_stats_store=store),
        )
        c, _ = _client(app)
        r = c.get(f"{web.PREFIX}/api/group-stats")
        assert r.status_code == 200
        d = r.json()
        assert d["enabled"] is True
        assert d["window"]["start"] and d["window"]["end"]
        assert len(d["groups"]) == 1
        g = d["groups"][0]
        assert g["group_id"] == "456"
        assert g["window_total"] == sum(range(5, 11))  # 10+9+…+5
        assert len(g["top"]) == 5  # top_n 默认 5（AGENT_GROUP_STATS_TOP_N）
        assert g["top"][0]["nickname"] == "群友0"
        assert g["top"][0]["msgs"] == 10
        assert g["words"][0] == {"word": "服务器", "count": 9}
        assert g["hours"] == {"19": 15}  # 5+4+3+2+1+0
        # 上周 + 本周（进行中）同时返回
        wins = {w["key"]: w for w in d["windows"]}
        assert set(wins) == {"last", "current"}
        assert wins["last"]["start"] == d["window"]["start"]
        last = {x["group_id"]: x for x in wins["last"]["groups"]}
        cur = {x["group_id"]: x for x in wins["current"]["groups"]}
        assert last["456"]["msgs"] == sum(range(5, 11))
        assert cur["456"]["msgs"] == 3
        assert "789" in cur and "789" not in last, "只在本周出现的群也要显示"
        assert "本周" in wins["current"]["label"]

    def test_response_has_no_secrets_or_message_text(self, monkeypatch):
        """web 面不变量：响应体不得含 api_key/base_url/消息正文。"""
        app = FastAPI()
        _mount(monkeypatch)
        assert web.mount_web(app) is True
        monkeypatch.setenv("AGENT_GROUP_STATS", "1")
        store = self._seed()
        import types

        monkeypatch.setattr(
            "nonebot.get_driver",
            lambda: types.SimpleNamespace(_agent_group_stats_store=store),
        )
        c, _ = _client(app)
        body = c.get(f"{web.PREFIX}/api/group-stats").text
        for forbidden in ("api_key", "base_url", "LLM_API_KEY"):
            assert forbidden not in body
        # 只应有聚合词，不应出现完整句子（词频表的词都是切分过的短词）
        assert "服务器" in body

    def test_page_has_stats_tab(self):
        """页面必须有群统计标签与渲染函数（前端接线回归锁）。"""
        assert 'id="tabStats"' in web._PAGE
        assert 'id="statsView"' in web._PAGE
        assert "api/group-stats" in web._PAGE
        assert "function renderStats" in web._PAGE
        assert "statsWindowBody" in web._PAGE
        assert "本周（进行中）" in web._PAGE


class TestPersonaWeb:
    """人格导入/编辑/删除（web 写面）：两段确认 + 审计 + 覆盖备份 + default 拒删。

    变异锚点：去掉 _NAME_RE 预校验、去掉覆盖备份、去掉 default 拒删、
    去掉 _consume_confirm，都必须有用例红。
    """

    SAVE = f"{web.PREFIX}/api/personas/save"
    DELETE = f"{web.PREFIX}/api/personas/delete"
    LIST = f"{web.PREFIX}/api/personas"

    @staticmethod
    def _pm(monkeypatch, tmp_path):
        from types import SimpleNamespace

        import nonebot

        from agentcore.memory.store import InMemoryMemoryStore
        from agentcore.personas import PersonaManager

        pm = PersonaManager(tmp_path / "personas")
        memory = InMemoryMemoryStore()
        # 测试环境 nonebot 未 init（get_driver 抛 ValueError）：注入假 driver，
        # 走与生产相同的 getattr(driver, "_agent_*") 路径
        monkeypatch.setattr(
            nonebot,
            "_driver",
            SimpleNamespace(_agent_persona_manager=pm, _agent_memory=memory),
        )
        return pm

    def _mount(self, monkeypatch, tmp_path, **kw):
        return TestSettingsWrite()._mount_write(monkeypatch, tmp_path, **kw)

    def _save(self, c, **kw):
        body = {"name": "tester", "description": "测试人格", "body": "你是测试员。"}
        body.update(kw)
        r1 = c.post(self.SAVE, json=body)
        assert r1.status_code == 200, r1.text
        data = r1.json()
        assert data.get("need_confirm") is True
        r2 = c.post(
            self.SAVE,
            json={
                **body,
                "confirm_token": data["confirm_token"],
                "confirm_nonce": data["confirm_nonce"],
                "totp_code": _totp(),
            },
        )
        return r2

    # ---- 只读面 ----

    def test_list_and_detail(self, monkeypatch, tmp_path):
        pm = self._pm(monkeypatch, tmp_path)
        pm.save("tester", description="测试人格", body="你是测试员。")
        c, _ = self._mount(monkeypatch, tmp_path, write=None)  # 只读 mount 也可见
        d = c.get(self.LIST).json()
        assert [p["name"] for p in d["personas"]] == ["tester"]
        assert d["personas"][0]["description"] == "测试人格"
        assert d["write_enabled"] is False  # 写面未开：列表照常，写入入口关闭
        assert d["dir"] == str(pm.dir)
        detail = c.get(f"{self.LIST}/detail/tester").json()
        assert detail["body"] == "你是测试员。"
        assert c.get(f"{self.LIST}/detail/nope").status_code == 404
        # 非法名（路径字符/空格）走 get 的 _NAME_RE → 404，不碰盘
        assert c.get(f"{self.LIST}/detail/bad%20name").status_code == 404

    def test_list_without_manager_degrades(self, monkeypatch, tmp_path):
        """driver 上没有 persona manager：空列表 + write_enabled，绝不 500。"""
        c, _ = self._mount(monkeypatch, tmp_path)
        d = c.get(self.LIST).json()
        assert d["personas"] == [] and d["dir"] is None

    # ---- 写面 ----

    def test_save_two_phase_lands_file_and_audits(self, monkeypatch, tmp_path):
        from agentcore.diagnostics import clear as _clear
        from agentcore.diagnostics import recent as _recent

        pm = self._pm(monkeypatch, tmp_path)
        c, _ = self._mount(monkeypatch, tmp_path)
        _clear()
        try:
            r = self._save(c)
            assert r.status_code == 200, r.text
            assert r.json()["ok"] is True
            path = pm.dir / "tester.md"
            assert path.is_file()
            text = path.read_text(encoding="utf-8")
            assert "name: tester" in text and "你是测试员。" in text
            # 审计留痕（不含正文）
            ev = [e for e in _recent() if e.get("mode") == "persona-save"]
            assert ev and ev[-1]["key"] == "persona:save:tester"
            assert "你是测试员" not in str(ev)
        finally:
            _clear()

    def test_save_invalid_name_422_without_challenge(self, monkeypatch, tmp_path):
        self._pm(monkeypatch, tmp_path)
        c, _ = self._mount(monkeypatch, tmp_path)
        r = c.post(self.SAVE, json={"name": "../evil", "body": "x"})
        assert r.status_code == 422  # 预校验直接拒，不发挑战码
        assert "need_confirm" not in r.json()

    def test_save_oversized_body_422(self, monkeypatch, tmp_path):
        self._pm(monkeypatch, tmp_path)
        c, _ = self._mount(monkeypatch, tmp_path)
        r = c.post(self.SAVE, json={"name": "big", "body": "x" * 20001})
        assert r.status_code == 422

    def test_save_overwrite_backs_up_old_file(self, monkeypatch, tmp_path):
        pm = self._pm(monkeypatch, tmp_path)
        c, _ = self._mount(monkeypatch, tmp_path)
        assert self._save(c, body="第一版").status_code == 200
        r = self._save(c, body="第二版")
        assert r.status_code == 200, r.text
        assert r.json()["backup"] is not None
        assert (pm.dir / "tester.md").read_text(encoding="utf-8").find("第二版") > 0
        # 备份必须落 web 备份根（AGENT_WEB_BACKUP_DIR=tmp/bk）下的 personas/，
        # 不能落进人格目录本身（e2e 实测抓到 .bak 落进 PERSONAS_DIR）
        backups = list((tmp_path / "bk" / "personas").glob("*.bak"))
        assert len(backups) == 1 and "第一版" in backups[0].read_text(encoding="utf-8")
        assert list(pm.dir.glob("*.bak")) == []  # 人格目录里不许有备份残留

    def test_save_overwrite_backup_is_0600_unique_and_rotated(
        self, monkeypatch, tmp_path
    ):
        """上轮遗留 M1（REVIEW-2ed7c63..1b16fa7）三重不一致收口：
        ① 0644 → 0600 一步到位；② 秒级同名互覆 → 唯一名；③ 无滚动 → 保留 N 份。
        """
        import stat as stat_mod

        pm = self._pm(monkeypatch, tmp_path)
        c, _ = self._mount(monkeypatch, tmp_path)
        self._save(c, body="v0")
        names = set()
        for i in range(1, 9):  # 连续 8 次覆盖：每次都该留下 0600 的唯一备份
            r = self._save(c, body=f"v{i}")
            assert r.status_code == 200, r.text
            names.add(r.json()["backup"])
        assert len(names) == 8, "每次覆盖的备份名必须唯一（同秒互覆已修）"
        bdir = tmp_path / "bk" / "personas"
        left = sorted(bdir.glob("*.bak"))
        assert len(left) == 5, f"滚动保留 5 份，实得 {len(left)}"
        for p in left:
            assert stat_mod.S_IMODE(p.stat().st_mode) == 0o600, p
        # 人格正文本体同样 0600（save 的 tmp 窗口是上轮遗留 M2）
        assert stat_mod.S_IMODE((pm.dir / "tester.md").stat().st_mode) == 0o600

    def test_save_confirm_is_single_use(self, monkeypatch, tmp_path):
        self._pm(monkeypatch, tmp_path)
        c, _ = self._mount(monkeypatch, tmp_path)
        r1 = c.post(self.SAVE, json={"name": "tester", "body": "b"})
        nonce, token = r1.json()["confirm_nonce"], r1.json()["confirm_token"]
        body = {
            "name": "tester",
            "body": "b",
            "confirm_token": token,
            "confirm_nonce": nonce,
            "totp_code": _totp(),
        }
        assert c.post(self.SAVE, json=body).status_code == 200
        assert c.post(self.SAVE, json=body).status_code == 400  # 重放被拒

    def test_save_confirm_binds_value(self, monkeypatch, tmp_path):
        """挑战后改正文再确认：HMAC 绑定最终文本，改内容 = 码失效。"""
        self._pm(monkeypatch, tmp_path)
        c, _ = self._mount(monkeypatch, tmp_path)
        r1 = c.post(self.SAVE, json={"name": "tester", "body": "原始正文"})
        r2 = c.post(
            self.SAVE,
            json={
                "name": "tester",
                "body": "偷换后的正文",
                "confirm_token": r1.json()["confirm_token"],
                "confirm_nonce": r1.json()["confirm_nonce"],
                "totp_code": _totp(),
            },
        )
        assert r2.status_code == 400

    def test_delete_two_phase(self, monkeypatch, tmp_path):
        pm = self._pm(monkeypatch, tmp_path)
        c, _ = self._mount(monkeypatch, tmp_path)
        assert self._save(c).status_code == 200
        r1 = c.post(self.DELETE, json={"name": "tester"})
        assert r1.status_code == 200 and r1.json()["need_confirm"] is True
        r2 = c.post(
            self.DELETE,
            json={
                "name": "tester",
                "confirm_token": r1.json()["confirm_token"],
                "confirm_nonce": r1.json()["confirm_nonce"],
                "totp_code": _totp(),
            },
        )
        assert r2.status_code == 200, r2.text
        assert not (pm.dir / "tester.md").exists()
        assert [p["name"] for p in c.get(self.LIST).json()["personas"]] == []

    def test_delete_default_rejected(self, monkeypatch, tmp_path):
        self._pm(monkeypatch, tmp_path)
        c, _ = self._mount(monkeypatch, tmp_path)
        assert self._save(c, default=True).status_code == 200
        r = c.post(self.DELETE, json={"name": "tester"})
        assert r.status_code == 409 and r.json()["detail"] == "persona_default"

    def test_delete_missing_404(self, monkeypatch, tmp_path):
        self._pm(monkeypatch, tmp_path)
        c, _ = self._mount(monkeypatch, tmp_path)
        assert c.post(self.DELETE, json={"name": "nope"}).status_code == 404

    def test_write_gate_404(self, monkeypatch, tmp_path):
        """写面未挂载：save/delete 路由不存在（404），列表照常。"""
        self._pm(monkeypatch, tmp_path)
        c, _ = self._mount(monkeypatch, tmp_path, write=None)
        assert (
            c.post(self.SAVE, json={"name": "tester", "body": "b"}).status_code == 404
        )
        assert c.post(self.DELETE, json={"name": "tester"}).status_code == 404
        assert c.get(self.LIST).status_code == 200

    def test_save_without_manager_409(self, monkeypatch, tmp_path):
        c, _ = self._mount(monkeypatch, tmp_path)
        r = c.post(self.SAVE, json={"name": "tester", "body": "b"})
        assert r.status_code == 409
        assert r.json()["detail"] == "persona_manager_not_initialized"

    def test_page_has_persona_tab(self):
        """页面必须有人格标签与导入/编辑/删除函数（前端接线回归锁）。"""
        assert 'id="tabPersona"' in web._PAGE
        assert 'id="personaView"' in web._PAGE
        assert "api/personas" in web._PAGE
        assert "function renderPersonaView" in web._PAGE
        assert "function personaSave" in web._PAGE
        assert "function personaEdit" in web._PAGE
        assert "function personaDelete" in web._PAGE
        assert "function personaFillFromFile" in web._PAGE
        # 表单只建一次 + 列表局部刷新：5s 轮询整体重写 innerHTML 会把用户
        # 正在填的表单清空（2026-10-20 实测反馈，行为由 e2e 复核）
        assert "el.dataset.built" in web._PAGE
        assert "function renderPersonaList" in web._PAGE
        assert 'id="personaLists"' in web._PAGE


class TestPersonaGroupWeb:
    """群人格 web 管理（2026-10-20）：两段确认 + 审计 + 校验 + 写面门禁。

    变异锚点：去掉 ASCII 数字校验（全角数字应 422）、去掉 persona 存在性校验、
    去掉两段确认，都必须有用例红。
    """

    PATH = f"{web.PREFIX}/api/personas/group"
    LIST = f"{web.PREFIX}/api/personas"

    @staticmethod
    def _setup(monkeypatch, tmp_path):
        pm = TestPersonaWeb._pm(monkeypatch, tmp_path)
        pm.save("group_x", description="群人格")
        import nonebot

        return pm, nonebot._driver._agent_memory

    def _mount(self, monkeypatch, tmp_path, **kw):
        return TestSettingsWrite()._mount_write(monkeypatch, tmp_path, **kw)

    def _save_group(self, c, group_id="999", persona="group_x"):
        body = {"action": "save", "group_id": group_id, "persona": persona}
        r1 = c.post(self.PATH, json=body)
        assert r1.status_code == 200, r1.text
        assert r1.json().get("need_confirm") is True
        return c.post(
            self.PATH,
            json={
                **body,
                "confirm_token": r1.json()["confirm_token"],
                "confirm_nonce": r1.json()["confirm_nonce"],
                "totp_code": _totp(),
            },
        )

    def test_list_includes_groups(self, monkeypatch, tmp_path):
        import asyncio

        pm, memory = self._setup(monkeypatch, tmp_path)
        asyncio.run(memory.set_group_persona("999", "group_x"))
        c, _ = self._mount(monkeypatch, tmp_path)
        d = c.get(self.LIST).json()
        assert d["group_personas"] == [{"group_id": "999", "persona": "group_x"}]
        assert isinstance(d["known_groups"], list)

    def test_save_two_phase_and_live(self, monkeypatch, tmp_path):
        import asyncio

        from agentcore.diagnostics import clear as _clear
        from agentcore.diagnostics import recent as _recent

        pm, memory = self._setup(monkeypatch, tmp_path)
        c, _ = self._mount(monkeypatch, tmp_path)
        _clear()
        r = self._save_group(c)
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
        # live 生效：写完 store 里立即可读（无需重启）
        assert asyncio.run(memory.get_group_persona("999")) == "group_x"
        ev = [e for e in _recent() if e.get("mode") == "persona-group"]
        assert ev and ev[-1]["key"] == "persona:group:999"
        _clear()

    def test_invalid_group_id(self, monkeypatch, tmp_path):
        self._setup(monkeypatch, tmp_path)
        c, _ = self._mount(monkeypatch, tmp_path)
        for gid in ("abc", "１２３", "", "-1", "1 2"):
            r = c.post(
                self.PATH,
                json={"action": "save", "group_id": gid, "persona": "group_x"},
            )
            assert r.status_code == 422, gid

    def test_unknown_persona_rejected(self, monkeypatch, tmp_path):
        self._setup(monkeypatch, tmp_path)
        c, _ = self._mount(monkeypatch, tmp_path)
        r = c.post(
            self.PATH,
            json={"action": "save", "group_id": "999", "persona": "nope"},
        )
        assert r.status_code == 422 and r.json()["detail"] == "persona_not_found"

    def test_delete_two_phase_and_missing(self, monkeypatch, tmp_path):
        import asyncio

        pm, memory = self._setup(monkeypatch, tmp_path)
        c, _ = self._mount(monkeypatch, tmp_path)
        asyncio.run(memory.set_group_persona("999", "group_x"))
        r1 = c.post(self.PATH, json={"action": "delete", "group_id": "999"})
        assert r1.json()["need_confirm"] is True
        r2 = c.post(
            self.PATH,
            json={
                "action": "delete",
                "group_id": "999",
                "confirm_token": r1.json()["confirm_token"],
                "confirm_nonce": r1.json()["confirm_nonce"],
                "totp_code": _totp(),
            },
        )
        assert r2.status_code == 200
        assert asyncio.run(memory.get_group_persona("999")) is None
        # 再删一次：挑战通过但记录已不存在 → 404
        r3 = c.post(self.PATH, json={"action": "delete", "group_id": "999"})
        r4 = c.post(
            self.PATH,
            json={
                "action": "delete",
                "group_id": "999",
                "confirm_token": r3.json()["confirm_token"],
                "confirm_nonce": r3.json()["confirm_nonce"],
                "totp_code": _totp(),
            },
        )
        assert r4.status_code == 404

    def test_write_gate_404(self, monkeypatch, tmp_path):
        self._setup(monkeypatch, tmp_path)
        c, _ = self._mount(monkeypatch, tmp_path, write=None)
        r = c.post(
            self.PATH,
            json={"action": "save", "group_id": "999", "persona": "group_x"},
        )
        assert r.status_code == 404
        # 群人格配置在只读模式下仍可见（列表接口）
        assert c.get(self.LIST).status_code == 200

    def test_page_has_group_persona_ui(self):
        assert 'id="personaGroups"' in web._PAGE
        assert "function renderPersonaGroups" in web._PAGE
        assert "function personaGroupSave" in web._PAGE
        assert "function personaGroupDelete" in web._PAGE
        assert 'id="psGroupList"' in web._PAGE


class TestBackupAPI:
    """备份面：只读列表 + 显式动作（校验/删除/下载/立即备份）。

    重点与其余 web 用例一致：门禁、白名单（不许 ``../``）、2FA 单次消费、
    最新一份保护、响应不含路径/密钥。
    """

    BACKUP = f"{web.PREFIX}/api/backup"
    RUN = f"{web.PREFIX}/api/backup/run"
    VERIFY = f"{web.PREFIX}/api/backup/verify"
    DELETE = f"{web.PREFIX}/api/backup/delete"
    DOWNLOAD = f"{web.PREFIX}/api/backup/download"

    def _mount_write(self, monkeypatch, tmp_path, write="1", cidrs="10.0.0.0/8"):
        import asyncio

        d = tmp_path / "backups"
        d.mkdir()
        for day in ("22", "23"):
            p = d / f"agent-demo-2026-10-{day}.sql.gz"
            p.write_bytes(gzip.compress(b"-- fake dump\n"))
            p.with_name(p.name + ".sha256").write_text(
                hashlib.sha256(p.read_bytes()).hexdigest(), encoding="utf-8"
            )
        monkeypatch.setenv("AGENT_BACKUP_DIR", str(d))
        monkeypatch.setenv("AGENT_BACKUP_KEEP", "7")
        # conftest 会把 .env 注入的 DATABASE_URL 剥掉；立即备份需要它存在
        # （backup_database 已被 mock，不会真连库）。
        monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")
        monkeypatch.setenv("AGENT_WEB_TOKEN", "t0ken")
        monkeypatch.setenv("AGENT_WEB_ALLOW_CIDRS", cidrs)
        if write is None:
            monkeypatch.delenv("AGENT_WEB_WRITE", raising=False)
        else:
            monkeypatch.setenv("AGENT_WEB_WRITE", write)
        # 模块级 job/锁在测试间会串：job 清空，Lock 重建（避免绑定旧事件循环）。
        web._backup_job.update(
            running=False, started_at=0.0, last=None, last_error=None
        )
        web._backup_job.pop("_task", None)
        monkeypatch.setattr(web, "_backup_op_lock", asyncio.Lock())
        monkeypatch.setattr(web, "_verify_lock", asyncio.Lock())
        app = FastAPI()
        assert web.mount_web(app) is True
        c = TestClient(
            app, headers={"Authorization": "Bearer t0ken"}, client=("10.1.2.3", 5000)
        )
        return c, d

    def _post(self, c, url, body):
        return c.post(url, json=body)

    def _fake_backup(self, d):
        async def fake(db_url, out_dir, *, keep=7, mirror_dir=None):
            return {
                "path": str(d / "agent-demo-2026-10-24.sql.gz"),
                "strategy": "fake",
                "bytes": 10,
                "pruned": [],
                "mirrored": False,
            }

        return fake

    def test_status_requires_auth_and_hides_paths(self, monkeypatch, tmp_path):
        c, d = self._mount_write(monkeypatch, tmp_path)
        anon = TestClient(c.app, client=("10.1.2.3", 5000))
        assert anon.get(self.BACKUP).status_code == 401
        r = c.get(self.BACKUP)
        assert r.status_code == 200
        data = r.json()
        names = [i["name"] for i in data["items"]]
        assert names == [
            "agent-demo-2026-10-23.sql.gz",
            "agent-demo-2026-10-22.sql.gz",
        ], "列表必须按 mtime 最新在前"
        assert data["latest"]["name"] == "agent-demo-2026-10-23.sql.gz"
        assert data["write_enabled"] is True
        assert data["items"][0]["has_sha256"] is True
        assert data["disk"]["free"] > 0
        assert "path" not in data["items"][0], "响应不得带绝对路径"
        assert "DATABASE_URL" not in r.text

    def test_write_gate_hides_actions_but_download_works(self, monkeypatch, tmp_path):
        c, _ = self._mount_write(monkeypatch, tmp_path, write=None)
        assert self._post(c, self.RUN, {}).status_code == 404, "写面未开启时路由不挂载"
        assert (
            self._post(
                c, self.DELETE, {"name": "agent-demo-2026-10-22.sql.gz"}
            ).status_code
            == 404
        )
        assert c.get(self.BACKUP).status_code == 200
        # 下载是只读面 + 独立 2FA，不依赖 AGENT_WEB_WRITE
        r = self._post(c, self.DOWNLOAD, {"name": "agent-demo-2026-10-22.sql.gz"})
        assert r.status_code == 200 and r.json()["need_confirm"] is True

    def test_run_two_step_completes_and_audits(self, monkeypatch, tmp_path):
        c, d = self._mount_write(monkeypatch, tmp_path)
        import agentcore.backup as backup_mod

        monkeypatch.setattr(backup_mod, "backup_database", self._fake_backup(d))
        r1 = self._post(c, self.RUN, {})
        assert r1.status_code == 200 and r1.json()["need_confirm"] is True
        r2 = self._post(
            c,
            self.RUN,
            {
                "confirm_token": r1.json()["confirm_token"],
                "confirm_nonce": r1.json()["confirm_nonce"],
                "totp_code": _totp(),
            },
        )
        assert r2.status_code == 200, r2.text
        data = c.get(self.BACKUP).json()
        for _ in range(40):
            if not data["job"]["running"]:
                break
            time.sleep(0.05)
            data = c.get(self.BACKUP).json()
        assert data["job"]["running"] is False
        assert data["job"]["last"]["name"] == "agent-demo-2026-10-24.sql.gz"
        assert data["job"]["last_error"] is None
        assert any(e["kind"] == "backup_run" for e in web._recent_events(50))

    def test_run_rejects_bad_totp_and_replay(self, monkeypatch, tmp_path):
        c, d = self._mount_write(monkeypatch, tmp_path)
        import agentcore.backup as backup_mod
        from agentcore import totp as totp_mod

        monkeypatch.setattr(backup_mod, "backup_database", self._fake_backup(d))
        r1 = self._post(c, self.RUN, {})
        body = {
            "confirm_token": r1.json()["confirm_token"],
            "confirm_nonce": r1.json()["confirm_nonce"],
            "totp_code": "123456",
        }
        real_verify = totp_mod.verify
        monkeypatch.setattr(totp_mod, "verify", lambda *a, **k: False)
        assert self._post(c, self.RUN, body).status_code == 400
        monkeypatch.setattr(totp_mod, "verify", real_verify)
        body["totp_code"] = _totp()
        assert self._post(c, self.RUN, body).status_code == 200
        replay = self._post(c, self.RUN, body)
        assert replay.status_code == 400
        assert replay.json()["detail"] == "confirm_reused"

    def test_delete_protects_latest_and_removes_sidecar(self, monkeypatch, tmp_path):
        c, d = self._mount_write(monkeypatch, tmp_path)
        latest = "agent-demo-2026-10-23.sql.gz"
        r1 = self._post(c, self.DELETE, {"name": latest})
        assert r1.status_code == 200 and r1.json()["need_confirm"] is True
        r2 = self._post(
            c,
            self.DELETE,
            {
                "name": latest,
                "confirm_token": r1.json()["confirm_token"],
                "confirm_nonce": r1.json()["confirm_nonce"],
                "totp_code": _totp(),
            },
        )
        assert r2.status_code == 409
        assert r2.json()["detail"] == "latest_protected"

        old = "agent-demo-2026-10-22.sql.gz"
        r3 = self._post(c, self.DELETE, {"name": old})
        r4 = self._post(
            c,
            self.DELETE,
            {
                "name": old,
                "confirm_token": r3.json()["confirm_token"],
                "confirm_nonce": r3.json()["confirm_nonce"],
                "totp_code": _totp(),
            },
        )
        assert r4.status_code == 200, r4.text
        assert not (d / old).exists()
        assert not (d / f"{old}.sha256").exists(), "sidecar 必须一起删"
        assert any(e["kind"] == "backup_delete" for e in web._recent_events(50))

    def test_verify_good_and_bad(self, monkeypatch, tmp_path):
        c, d = self._mount_write(monkeypatch, tmp_path)
        good = self._post(c, self.VERIFY, {"name": "agent-demo-2026-10-22.sql.gz"})
        assert good.status_code == 200
        assert good.json()["ok"] is True
        assert good.json()["checksum"] == "verified"

        bad = d / "agent-demo-2026-10-21.sql.gz"
        bad.write_bytes(b"not gzip")
        bad.with_name(bad.name + ".sha256").write_text(
            hashlib.sha256(b"not gzip").hexdigest(), encoding="utf-8"
        )
        r = self._post(c, self.VERIFY, {"name": bad.name})
        assert r.status_code == 200
        assert r.json()["ok"] is False

    def test_download_two_step_headers_and_body(self, monkeypatch, tmp_path):
        c, d = self._mount_write(monkeypatch, tmp_path)
        name = "agent-demo-2026-10-22.sql.gz"
        r1 = self._post(c, self.DOWNLOAD, {"name": name})
        assert r1.status_code == 200
        assert r1.json()["need_confirm"] is True
        assert r1.json()["bytes"] == (d / name).stat().st_size
        r2 = self._post(
            c,
            self.DOWNLOAD,
            {
                "name": name,
                "confirm_token": r1.json()["confirm_token"],
                "confirm_nonce": r1.json()["confirm_nonce"],
                "totp_code": _totp(),
            },
        )
        assert r2.status_code == 200, r2.text
        assert r2.headers["content-type"].startswith("application/gzip")
        assert name in r2.headers["content-disposition"]
        assert r2.headers["cache-control"] == "no-store"
        assert r2.content == (d / name).read_bytes()
        assert any(e["kind"] == "backup_download" for e in web._recent_events(50))

    def test_download_rejects_traversal(self, monkeypatch, tmp_path):
        c, _ = self._mount_write(monkeypatch, tmp_path)
        for bad in (
            "../.env",
            "/etc/passwd",
            "agent-demo-2026-10-22.sql.gz.sha256",
            "..",
            "",
        ):
            r = self._post(c, self.DOWNLOAD, {"name": bad})
            assert r.status_code == 404, f"{bad!r} 不应被接受"

    def test_download_replayed_or_wrong_totp(self, monkeypatch, tmp_path):
        c, _ = self._mount_write(monkeypatch, tmp_path)
        from agentcore import totp as totp_mod

        name = "agent-demo-2026-10-22.sql.gz"
        r1 = self._post(c, self.DOWNLOAD, {"name": name})
        body = {
            "name": name,
            "confirm_token": r1.json()["confirm_token"],
            "confirm_nonce": r1.json()["confirm_nonce"],
            "totp_code": "123456",
        }
        real_verify = totp_mod.verify
        monkeypatch.setattr(totp_mod, "verify", lambda *a, **k: False)
        assert self._post(c, self.DOWNLOAD, body).status_code == 400
        monkeypatch.setattr(totp_mod, "verify", real_verify)
        body["totp_code"] = _totp()
        assert self._post(c, self.DOWNLOAD, body).status_code == 200
        assert self._post(c, self.DOWNLOAD, body).status_code == 400

    def test_page_has_backup_ui(self):
        for marker in (
            'id="tabBackup"',
            'id="backupView"',
            "function renderBackup",
            "function backupRun",
            "function backupVerify",
            "function backupDelete",
            "function backupDownload",
            "2FA 确认：下载备份",
        ):
            assert marker in web._PAGE, marker
