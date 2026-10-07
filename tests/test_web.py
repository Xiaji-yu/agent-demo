"""Web 只读总览：鉴权 fail-closed + IP 白名单 + 数据口径（无密钥、无正文）。

安全模型见 ``plugins/qq_agent_adapter/web.py`` 模块 docstring——这个测试文件的
重点就是**证明那道门是真的**：没 token 不挂载、错 token 401、IP 不在白名单 403、
返回体里没有 api_key/base_url。
"""

import re

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

    def test_confirm_token_reuse_rejected(self, monkeypatch, tmp_path):
        c, _ = self._mount_write(monkeypatch, tmp_path)
        r1 = self._post(c, {"key": "AGENT_VISION", "value": "true"}).json()
        payload = {
            "key": "AGENT_VISION",
            "value": "true",
            "confirm_token": r1["confirm_token"],
            "confirm_nonce": r1["confirm_nonce"],
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

    def test_writable_whitelist_is_exactly_seven_keys(self):
        """守卫：WRITABLE 白名单当前恰为 7 键（README/.env.example 同款数字）；
        误加敏感键时先撞这一条，再撞上面的参数化负面清单。"""
        from plugins.qq_agent_adapter.config_write import WRITABLE

        assert set(WRITABLE) == {
            "AGENT_VISION",
            "AGENT_GROUP_CONTEXT",
            "AGENT_GROUP_CONTEXT_LINES",
            "AGENT_GROUP_CONTEXT_TTL",
            "AGENT_WAKE_WORDS",
            "AGENT_BUDGET_DAILY_TOKENS",
            "AGENT_BUDGET_ENFORCE",
        }, set(WRITABLE)

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
                },
            )
            assert r2.status_code == 500 and "rolled_back" in r2.json()["detail"]
            assert envf.read_text(encoding="utf-8") == original
            from agentcore.diagnostics import recent as _recent

            assert any(e["kind"] == "config_write_rollback" for e in _recent(5))
        finally:
            _clear()

    def test_settings_payload_carries_write_enabled(self, monkeypatch, tmp_path):
        c, _ = self._mount_write(monkeypatch, tmp_path)
        d = c.get(f"{web.PREFIX}/api/settings").json()
        assert d["write_enabled"] is True
        c2, _ = self._mount_write(monkeypatch, tmp_path, write=None)
        d2 = c2.get(f"{web.PREFIX}/api/settings").json()
        assert d2["write_enabled"] is False

    def test_write_face_not_mounted_without_gate(self, monkeypatch, tmp_path):
        c, _ = self._mount_write(monkeypatch, tmp_path, write=None)
        paths = {getattr(r, "path", "") for r in c.app.routes}
        assert f"{web.PREFIX}/api/settings/write" not in paths
        assert f"{web.PREFIX}/api/settings" in paths  # 读面不受影响

    def test_write_panel_wired_in_page(self):
        assert "editKey" in web._PAGE
        assert "api/settings/write" in web._PAGE
        assert "write_enabled" in web._PAGE

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
            },
        )
        assert r2.status_code == 200
        assert envf.read_bytes() == b"AGENT_VISION=true\r\nOTHER=1\r\n"

    # ---- REVIEW-26fec4d..3ce6e0a M1–M10 修复回归 ----
    def test_m1_confirm_single_use_across_windows(self, monkeypatch, tmp_path):
        """M1：确认码「单次有效」不得被 30s 窗口边界绕开（跨窗重放=可复现的缺陷）。"""
        monkeypatch.delenv("AGENT_WEB_WRITE", raising=False)  # 不挂载，纯函数层
        monkeypatch.setenv("AGENT_WEB_TOKEN", "t0ken")
        nonce = "n1"
        token = web._confirm_token(nonce, "AGENT_VISION", "true", window=1000)
        now = 1000 * 30 + 5
        assert (
            web._consume_confirm(nonce, token, "AGENT_VISION", "true", now=now) is None
        )
        # 同窗口第二次：拒绝
        assert (
            web._consume_confirm(nonce, token, "AGENT_VISION", "true", now=now)
            == "confirm_reused"
        )
        # 跨窗口（宽限窗）：同一 nonce 已是「已消费」，必须拒绝
        nxt = 1001 * 30 + 5
        assert (
            web._consume_confirm(nonce, token, "AGENT_VISION", "true", now=nxt)
            == "confirm_reused"
        )
        # 别的 nonce（新挑战）不受影响
        token2 = web._confirm_token("n2", "AGENT_VISION", "true", window=1001)
        assert (
            web._consume_confirm("n2", token2, "AGENT_VISION", "true", now=nxt) is None
        )
        # 清理：窗口记账无界增长防护仍有效
        assert all(isinstance(k, str) for k in web._used_confirms)

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
            },
        )
        assert r.status_code == 400
        assert "confirm" in r.json()["detail"]

    def test_m4_symlink_env_409(self, monkeypatch, tmp_path):
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
                },
            )
            ev = [e for e in _recent(5) if e["kind"] == "config_write"][0]
            assert ev["source_ip"] == "10.1.2.3"
            assert ev["confirm_nonce"] == r1.json()["confirm_nonce"]
        finally:
            _clear()

    def test_m6_m7_page_js_fixed(self):
        """M6/M7 页面护栏：编辑二段必须带 nonce；下载不得再用 location.href。"""
        import re

        edit = re.search(r"async function editKey\(key.*?\n}", web._PAGE, re.DOTALL)
        assert edit, "editKey 不在页面里"
        body = edit.group(0)
        assert "confirm_nonce: res.data.confirm_nonce" in body
        assert 'location.href = "api/logs/download' not in web._PAGE
        assert "URL.createObjectURL" in web._PAGE
        assert "confirm_nonce" in web._PAGE
