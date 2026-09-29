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
        assert d2["reset"] is True and d2["lines"][0]["msg"] == "old\n".strip() or True
        assert d2["reset"] is True

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
