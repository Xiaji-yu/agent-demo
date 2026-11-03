"""Web 设置写面的乐观锁（REVIEW 2026-11-02 全量评审第二轮 P5）。

两个管理员同时改同一个键时，后写者会静默把前者的值覆盖掉——页面看不出
任何异常，只是别人的改动凭空消失。修法：设置视图给每个键带上当前落盘值的
摘要（``rev``），写入时带回 ``base_rev``，服务端在**发确认码前**与**写锁内**
各比对一次，对不上就 409 ``stale_revision``。

本机 Windows 上写路由的第二段过不了 TOTP 落盘（``agentcore/totp.py:74`` 裸用
``os.O_NOFOLLOW``），因此正向两段写走的回归仍由 ``tests/test_web.py`` 的
``test_happy_path_two_phase`` 在 CI 承担；这里覆盖新增的判据本身，以及
「确认通过了但版本已过期」这条必须在本地也能验的拒写路径。
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from plugins.qq_agent_adapter import web

WRITE = f"{web.PREFIX}/api/settings/write"


def _mount(monkeypatch, tmp_path, env_text="AGENT_VISION=false\n"):
    envf = tmp_path / ".env"
    envf.write_text(env_text, encoding="utf-8")
    monkeypatch.setenv("AGENT_ENV_FILE", str(envf))
    monkeypatch.setenv("AGENT_WEB_BACKUP_DIR", str(tmp_path / "bk"))
    monkeypatch.setenv("AGENT_WEB_TOKEN", "t0ken")
    monkeypatch.setenv("AGENT_WEB_ALLOW_CIDRS", "10.0.0.0/8")
    monkeypatch.setenv("AGENT_WEB_WRITE", "1")
    # 数据面只认登录签发的会话凭证：上游 ``_auth`` 明确拒绝原始 web token
    # （否则持 token 者绕过 2FA 直连写面，登录门形同虚设）⇒ 必须先走
    # /api/login 换 session，再拿 session 打写路由。范式同 tests/test_web.py。
    web._sessions.clear()
    web._used_confirms.clear()
    app = FastAPI()
    assert web.mount_web(app) is True
    addr = ("10.1.2.3", 5000)
    raw = TestClient(app, headers={"Authorization": "Bearer t0ken"}, client=addr)
    session = _login(raw)
    return (
        TestClient(app, headers={"Authorization": f"Bearer {session}"}, client=addr),
        envf,
    )


def _login(c):
    """走真实登录门换会话凭证（空码先试，缺 2FA 再补当期滚动码）。"""
    r = c.post(f"{web.PREFIX}/api/login", json={"totp_code": ""})
    if r.status_code == 401 and r.json().get("detail") == "totp_required":
        r = c.post(f"{web.PREFIX}/api/login", json={"totp_code": _totp()})
    assert r.status_code == 200, (r.status_code, r.text)
    return r.json()["session"]


def _totp() -> str:
    """当前 5 分钟 2FA 滚动码（conftest 已把 secret 关进 tmp）。"""
    from agentcore import totp

    return totp.current_code(totp.load_or_create_secret())


class TestSettingsRev:
    """``_settings_rev``：落盘值的摘要，值变摘要必须变。"""

    def test_env_rev_changes_with_value(self, monkeypatch, tmp_path):
        envf = tmp_path / ".env"
        monkeypatch.setenv("AGENT_ENV_FILE", str(envf))
        envf.write_text("AGENT_VISION=false\n", encoding="utf-8")
        a = web._settings_rev("AGENT_VISION")
        assert a and a != "unknown"
        assert web._settings_rev("AGENT_VISION") == a, "同值必须同摘要"
        envf.write_text("AGENT_VISION=true\n", encoding="utf-8")
        assert web._settings_rev("AGENT_VISION") != a, "值变了摘要必须变，否则锁不住"

    def test_env_rev_distinguishes_absent(self, monkeypatch, tmp_path):
        envf = tmp_path / ".env"
        monkeypatch.setenv("AGENT_ENV_FILE", str(envf))
        envf.write_text("AGENT_VISION=false\n", encoding="utf-8")
        present = web._settings_rev("AGENT_VISION")
        absent = web._settings_rev("AGENT_WEB_WRITE")
        assert absent != present, "未设置与已设置必须不同版"

    def test_unreadable_file_is_unknown(self, monkeypatch, tmp_path):
        """读不到文件时不给版本号：任何 base_rev 都对不上，写入被拒。"""
        missing = tmp_path / "nope" / ".env"
        monkeypatch.setenv("AGENT_ENV_FILE", str(missing))
        assert web._settings_rev("AGENT_VISION") == "unknown"

    def test_yaml_rev_changes_with_value(self, monkeypatch, tmp_path):
        cfg = tmp_path / "config.yaml"
        monkeypatch.setenv("AGENT_CONFIG", str(cfg))
        cfg.write_text("agent:\n  max_iterations: 4\n", encoding="utf-8")
        a = web._settings_rev("yaml:agent.max_iterations")
        assert a and a != "unknown"
        cfg.write_text("agent:\n  max_iterations: 5\n", encoding="utf-8")
        assert web._settings_rev("yaml:agent.max_iterations") != a

    def test_yaml_absent_key_is_unknown(self, monkeypatch, tmp_path):
        """yaml 侧键缺失时路径下钻失败 ⇒ unknown（而不是伪装成一个版本号）。"""
        cfg = tmp_path / "config.yaml"
        monkeypatch.setenv("AGENT_CONFIG", str(cfg))
        cfg.write_text("agent:\n  max_iterations: 4\n", encoding="utf-8")
        assert web._settings_rev("yaml:agent.nope") == "unknown"


class TestStaleRevision:
    """``_stale_revision``：判据只有一条——带上了且对不上。"""

    def test_empty_base_rev_never_blocks(self, monkeypatch, tmp_path):
        """没带 base_rev（旧前端/脚本）照旧放行，不因升级打破调用方。"""
        envf = tmp_path / ".env"
        monkeypatch.setenv("AGENT_ENV_FILE", str(envf))
        envf.write_text("AGENT_VISION=false\n", encoding="utf-8")
        assert web._stale_revision("AGENT_VISION", "") is False

    def test_matching_rev_passes(self, monkeypatch, tmp_path):
        envf = tmp_path / ".env"
        monkeypatch.setenv("AGENT_ENV_FILE", str(envf))
        envf.write_text("AGENT_VISION=false\n", encoding="utf-8")
        rev = web._settings_rev("AGENT_VISION")
        assert web._stale_revision("AGENT_VISION", rev) is False

    def test_mismatching_rev_blocks(self, monkeypatch, tmp_path):
        envf = tmp_path / ".env"
        monkeypatch.setenv("AGENT_ENV_FILE", str(envf))
        envf.write_text("AGENT_VISION=false\n", encoding="utf-8")
        assert web._stale_revision("AGENT_VISION", "deadbeefdeadbeef") is True


class TestPayloadCarriesRev:
    """设置视图必须把 rev 发出去，否则前端无从带回。"""

    def test_env_items_carry_rev(self, monkeypatch, tmp_path):
        envf = tmp_path / ".env"
        monkeypatch.setenv("AGENT_ENV_FILE", str(envf))
        envf.write_text("AGENT_VISION=false\n", encoding="utf-8")
        payload = web._env_settings_payload()
        items = [i for g in payload["groups"] for i in g["items"]]
        assert items, "设置项不能为空"
        for it in items:
            assert it.get("rev"), it["key"]
            assert it["rev"] == web._settings_rev(it["key"]), it["key"]

    def test_yaml_items_carry_rev(self, monkeypatch, tmp_path):
        cfg = tmp_path / "config.yaml"
        monkeypatch.setenv("AGENT_CONFIG", str(cfg))
        cfg.write_text("agent:\n  max_iterations: 4\n", encoding="utf-8")
        payload = web._yaml_settings_payload()
        items = [i for g in payload["groups"] for i in g["items"]]
        assert items, "config.yaml 设置项不能为空"
        for it in items:
            assert it.get("rev"), it.get("path")
            assert it["rev"] == web._settings_rev("yaml:" + it["path"]), it["path"]


class TestStaleWriteRejected:
    """路由层：过期版本在**发确认码前**就被挡掉，且不落盘。"""

    def test_stale_base_rev_rejected_before_confirm(self, monkeypatch, tmp_path):
        c, envf = _mount(monkeypatch, tmp_path)
        before = envf.read_text(encoding="utf-8")
        r = c.post(
            WRITE,
            json={
                "key": "AGENT_VISION",
                "value": "true",
                "base_rev": "deadbeefdeadbeef",
            },
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"] == "stale_revision"
        # 没走到发确认码那一步，文件一个字都没动
        assert envf.read_text(encoding="utf-8") == before
        assert "need_confirm" not in r.json()

    def test_fresh_base_rev_still_gets_confirm(self, monkeypatch, tmp_path):
        c, envf = _mount(monkeypatch, tmp_path)
        r = c.post(
            WRITE,
            json={
                "key": "AGENT_VISION",
                "value": "true",
                "base_rev": web._settings_rev("AGENT_VISION"),
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["need_confirm"] is True
        assert envf.read_text(encoding="utf-8") == "AGENT_VISION=false\n"

    def test_no_base_rev_still_works(self, monkeypatch, tmp_path):
        """向后兼容：不带 base_rev 的老调用方不受影响。"""
        c, envf = _mount(monkeypatch, tmp_path)
        r = c.post(WRITE, json={"key": "AGENT_VISION", "value": "true"})
        assert r.status_code == 200, r.text
        assert r.json()["need_confirm"] is True


class TestStaleWriteRejectedInLock:
    """第二道（写锁内）：2FA 都过了，版本在这期间被改掉也必须拒。"""

    def _phase1(self, c, key="AGENT_VISION", value="true", base_rev=None):
        body = {"key": key, "value": value}
        if base_rev is not None:
            body["base_rev"] = base_rev
        r = c.post(WRITE, json=body)
        assert r.status_code == 200, r.text
        return r.json()

    def test_confirm_passed_but_value_changed_is_rejected(self, monkeypatch, tmp_path):
        c, envf = _mount(monkeypatch, tmp_path)
        rev = web._settings_rev("AGENT_VISION")
        j = self._phase1(c, base_rev=rev)
        # 另一个管理员在二次确认期间改掉了同一个键
        envf.write_text("AGENT_VISION=true\n", encoding="utf-8")
        monkeypatch.setattr(web, "_consume_confirm", lambda *a, **k: None)
        r = c.post(
            WRITE,
            json={
                "key": "AGENT_VISION",
                "value": "false",
                "base_rev": rev,
                "confirm_token": j["confirm_token"],
                "confirm_nonce": j["confirm_nonce"],
                "totp_code": "000000",
            },
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"] == "stale_revision"
        # 别人的值必须原样留着，不能被这次过期写入覆盖
        assert envf.read_text(encoding="utf-8") == "AGENT_VISION=true\n"

    def test_lock_recheck_closes_check_to_write_window(self, monkeypatch, tmp_path):
        """锁内复检关掉「校验 → 拿锁」之间的窗口。

        校验在锁外、落盘在锁内，中间隔着一次 ``await``——另一个管理员的写
        请求完全可能在这个窗口里拿到锁先落盘。单线程里这个窗口不可见，
        因此用一个按调用次数翻转的假 rev 精确复现：第一次（锁外）放行，
        第二次（锁内）判过期。
        """
        c, envf = _mount(monkeypatch, tmp_path)
        rev = web._settings_rev("AGENT_VISION")
        j = self._phase1(c, base_rev=rev)
        calls = {"n": 0}

        def _flip(key):
            calls["n"] += 1
            return rev if calls["n"] == 1 else "changed-by-other-admin"

        monkeypatch.setattr(web, "_settings_rev", _flip)
        monkeypatch.setattr(web, "_consume_confirm", lambda *a, **k: None)
        r = c.post(
            WRITE,
            json={
                "key": "AGENT_VISION",
                "value": "true",
                "base_rev": rev,
                "confirm_token": j["confirm_token"],
                "confirm_nonce": j["confirm_nonce"],
                "totp_code": "000000",
            },
        )
        assert r.status_code == 409, r.text
        assert r.json()["detail"] == "stale_revision"
        assert calls["n"] == 2, "锁外一次 + 锁内复检一次，各司其职"
        assert envf.read_text(encoding="utf-8") == "AGENT_VISION=false\n"

    def test_unchanged_value_writes_through(self, monkeypatch, tmp_path):
        """锁内校验不能误伤：版本没变就照旧放行到落盘那一步。"""
        c, envf = _mount(monkeypatch, tmp_path)
        rev = web._settings_rev("AGENT_VISION")
        j = self._phase1(c, base_rev=rev)
        seen = {}

        def _fake_write(spec, file_text):
            seen["spec"] = spec.key
            seen["text"] = file_text
            return web.cw.WriteResult(spec.key, "false", file_text, envf, "replace")

        monkeypatch.setattr(web.cw, "write_env_key", _fake_write)
        monkeypatch.setattr(web, "_consume_confirm", lambda *a, **k: None)
        r = c.post(
            WRITE,
            json={
                "key": "AGENT_VISION",
                "value": "true",
                "base_rev": rev,
                "confirm_token": j["confirm_token"],
                "confirm_nonce": j["confirm_nonce"],
                "totp_code": "000000",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True
        assert seen == {"spec": "AGENT_VISION", "text": "true"}
        assert r.json()["rev"] == web._settings_rev("AGENT_VISION")


class TestFrontendWiring:
    """前端必须把 rev 带上去，否则后端那道锁永远是空转。"""

    def test_page_passes_base_rev(self):
        page = web._PAGE
        assert "base_rev" in page
        assert "writeSetting(key, v, it.rev)" in page
        # 第二段（真正落盘那次）也必须带上，否则锁只挡在发确认码那一步
        assert 'base_rev: rev || ""' in page

    def test_stale_revision_message_surfaced(self):
        assert "stale_revision" in web._PAGE
