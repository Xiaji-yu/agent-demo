"""reboot（/reboot + web 重启）核心测试：白名单 argv、执行链回落、停机编排。

执行链定稿（2026-09-29，用户选定 A 为主 + C/B 兜底）：
C 外部命令（白名单 argv spawn）→ B re-exec（预校验后自替换）→ A 退出兜底
（supervisor 拉起）。本文件只测解析/策略/编排/门禁，**绝不真重启**——
perform_reboot 的 spawn/execv/_exit 全部注入替身。
"""

import asyncio
import os

import pytest


@pytest.fixture(scope="module")
def rb():
    """初始化 NoneBot 后加载 reboot 模块（模块级 on_command 需要 driver）。"""
    import nonebot

    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init(_env_file=None, superusers={"10000"})
    import plugins.qq_agent_adapter.reboot as mod

    return mod


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (
        "AGENT_REBOOT_CMD",
        "AGENT_REBOOT_DELAY",
        "AGENT_REBOOT_UNIT",
        "AGENT_REBOOT_ENTRY",
    ):
        monkeypatch.delenv(key, raising=False)
    yield


class TestParseRebootCmd:
    def test_valid_argv(self, rb):
        argv, reason = rb.parse_reboot_cmd('["systemctl","restart","agent-demo"]')
        assert reason == "ok" and argv == ["systemctl", "restart", "agent-demo"]

    def test_docker_and_supervisor_allowed(self, rb):
        for exe in ("docker", "supervisorctl", "service"):
            argv, reason = rb.parse_reboot_cmd(f'["{exe}","restart","agent-demo"]')
            assert reason == "ok", exe

    def test_reject_shell_and_unknown_exe(self, rb):
        """无 shell 铁律：sh/-c/bash/python 一律拒；白名单外二进制拒。"""
        for bad in (
            '["sh","-c","systemctl restart agent-demo"]',
            '["bash","-lc","reboot"]',
            '["python","-c","import os;os.execv(...)"]',
            '["/usr/bin/evil","restart"]',
            '["rm","-rf","/"]',
        ):
            argv, reason = rb.parse_reboot_cmd(bad)
            assert argv is None, bad
            assert reason.startswith("exec_not_allowed"), bad

    def test_reject_malformed(self, rb):
        cases = {
            "": "empty",
            "not json": "not_json",
            '"systemctl restart x"': "not_argv_list",
            "[]": "not_argv_list",
            '["systemctl"]': "not_argv_list",
            '["systemctl",""]': "empty_item",
            '["systemctl",123]': "empty_item",
            '["systemctl","restart\\nkill"]': "control_chars",
        }
        for raw, expected in cases.items():
            argv, reason = rb.parse_reboot_cmd(raw)
            assert argv is None and reason == expected, (raw, reason)

    def test_control_chars_rejected(self, rb):
        argv, reason = rb.parse_reboot_cmd('["systemctl","restart\\u0000x"]')
        assert argv is None and reason == "control_chars"

    def test_path_forms_rejected(self, rb):
        """可执行体必须恰好是白名单裸名——绝对路径/./ 相对路径一概拒。"""
        for raw in (
            '["/usr/bin/systemctl","restart","x"]',
            '["./systemctl","restart","x"]',
            '["../systemctl","restart","x"]',
        ):
            argv, reason = rb.parse_reboot_cmd(raw)
            assert argv is None and reason.startswith("exec_not_allowed"), raw


class TestRebootPlan:
    def test_external_when_cmd_configured(self, rb, monkeypatch):
        monkeypatch.setenv(rb.CMD_ENV, '["systemctl","restart","agent-demo"]')
        strategy, argv = rb.reboot_plan()
        assert strategy == "external" and argv[0] == "systemctl"

    def test_execv_default(self, rb):
        strategy, argv = rb.reboot_plan()
        assert strategy == "execv" and argv is None  # 未配外部命令且入口存在

    def test_exit_when_execv_target_missing(self, rb, monkeypatch):
        monkeypatch.setattr(rb, "_execv_target_ok", lambda: False)
        strategy, argv = rb.reboot_plan()
        assert strategy == "exit"

    def test_exit_when_entry_missing(self, rb, monkeypatch, tmp_path):
        monkeypatch.setenv(rb.ENTRY_ENV, str(tmp_path / "nope.py"))
        strategy, _ = rb.reboot_plan()
        assert strategy == "exit"


class TestPerformReboot:
    def test_external_spawn_then_exit(self, rb, monkeypatch):
        monkeypatch.setenv(rb.CMD_ENV, '["docker","restart","agent-demo"]')
        seen = {}
        monkeypatch.setattr(
            rb, "_spawn_detached", lambda argv: seen.setdefault("argv", argv)
        )
        exits = []
        monkeypatch.setattr(os, "_exit", lambda code: exits.append(code))
        assert rb.perform_reboot() == "external"
        assert seen["argv"][0] == "docker"
        assert exits == [0]

    def test_spawn_failure_falls_back_to_execv(self, rb, monkeypatch):
        monkeypatch.setenv(rb.CMD_ENV, '["systemctl","restart","x"]')
        monkeypatch.setattr(rb, "_execv_target_ok", lambda: True)

        def boom(_argv):
            raise FileNotFoundError("systemctl missing")

        monkeypatch.setattr(rb, "_spawn_detached", boom)
        execv_calls = []
        monkeypatch.setattr(os, "execv", lambda a, b: execv_calls.append((a, b)))
        exits = []
        monkeypatch.setattr(os, "_exit", lambda code: exits.append(code))
        assert rb.perform_reboot() == "execv"
        assert execv_calls and execv_calls[0][1][1].endswith("bot.py")
        assert exits == []  # execv 成功就不 exit

    def test_execv_failure_falls_back_to_exit(self, rb, monkeypatch):
        monkeypatch.setattr(rb, "_execv_target_ok", lambda: True)

        def boom(_a, _b):
            raise OSError("exec format error")

        monkeypatch.setattr(os, "execv", boom)
        exits = []
        monkeypatch.setattr(os, "_exit", lambda code: exits.append(code))
        assert rb.perform_reboot() == "exit"
        assert exits == [0]

    def test_no_supervisor_means_exit(self, rb, monkeypatch):
        """裸 nohup 场景退到 exit——README 必须警示，这里只锁策略选择。"""
        monkeypatch.setattr(rb, "_execv_target_ok", lambda: False)
        monkeypatch.delenv(rb.CMD_ENV, raising=False)
        exits = []
        monkeypatch.setattr(os, "_exit", lambda code: exits.append(code))
        assert rb.perform_reboot() == "exit"


class TestRebootAfterReply:
    @pytest.mark.asyncio
    async def test_shutdown_then_reboot_order(self, rb, monkeypatch):
        """编排：先 delay → shutdown_agent → perform_reboot，顺序不可换。"""
        order = []

        async def fake_sleep(_d):
            order.append("sleep")

        async def fake_shutdown():
            order.append("shutdown")

        monkeypatch.setattr(rb.asyncio, "sleep", fake_sleep)
        monkeypatch.setattr(rb, "_shutdown_everything", fake_shutdown)
        monkeypatch.setattr(rb, "perform_reboot", lambda **kw: order.append("reboot"))
        monkeypatch.setattr(rb, "reboot_delay", lambda: 0.0)
        await rb.reboot_after_reply(delay=0.0)
        assert order == ["sleep", "shutdown", "reboot"]

    @pytest.mark.asyncio
    async def test_shutdown_failure_still_reboots(self, rb, monkeypatch):
        order = []

        async def boom():
            raise RuntimeError("aclose failed")

        monkeypatch.setattr(rb, "_shutdown_everything", boom)
        monkeypatch.setattr(rb, "perform_reboot", lambda **kw: order.append("reboot"))
        await rb.reboot_after_reply(delay=0.0)
        assert order == ["reboot"]  # 重启优先级高于单个 closer 的洁癖

    def test_schedule_reboot_keeps_strong_ref(self, rb, monkeypatch):
        """挂起的重启任务必须被强引用持有，完成后自动移除。

        破坏点验证：去掉 _REBOOT_TASKS 强引用后，任务 park 时集合为空，断言失败。
        asyncio 对 Task 只持弱引用——不存起来就有被 GC 吞掉、重启序列中途消失的
        真实风险（与 REVIEW 线2 F6「通知改 create_task（持强引用）」同型）。
        park 点选 ``_shutdown_everything``（真正被 await 的位置）；
        ``perform_reboot`` 是同步函数，桩也必须是同步的（曾误用 async 桩，
        得到"coroutine never awaited"假完成——教训存档）。
        """
        gate = asyncio.Event()
        order = []

        async def _park_shutdown():
            await gate.wait()

        monkeypatch.setattr(rb, "_shutdown_everything", _park_shutdown)
        monkeypatch.setattr(rb, "perform_reboot", lambda **kw: order.append("reboot"))
        monkeypatch.setattr(rb, "reboot_delay", lambda: 0.0)
        assert len(rb._REBOOT_TASKS) == 0

        async def main():
            rb.schedule_reboot()
            for _ in range(5):
                await asyncio.sleep(0)  # 让任务走到 park 点
            assert len(rb._REBOOT_TASKS) == 1, "挂起任务必须被强引用持有"
            gate.set()
            for _ in range(5):
                await asyncio.sleep(0)  # 放行 → 走到 perform_reboot 并完成
            assert len(rb._REBOOT_TASKS) == 0, "完成后必须从集合移除"
            assert order == ["reboot"]

        asyncio.run(main())


class TestShutdownEverything:
    @pytest.mark.asyncio
    async def test_uses_lifecycle_shutdown_agent(self, rb, monkeypatch):
        """重启复用与 bot.py on_shutdown 同一套幂等收尾。"""
        seen = {}

        async def fake_shutdown_agent(**kwargs):
            seen.update(kwargs)
            seen["extra"] = list(kwargs.get("extra_closers") or [])

        import plugins.qq_agent_adapter.lifecycle as lifecycle

        monkeypatch.setattr(lifecycle, "shutdown_agent", fake_shutdown_agent)
        await rb._shutdown_everything()
        assert "debouncer" in seen and "memory" in seen
        assert len(seen["extra"]) == 2  # shared_llm + search


def _mk_send(sent):
    async def _send(msg=None, **kw):
        sent.append(str(msg))

    return _send


def _mk_finish(finished):
    from nonebot.exception import FinishedException

    async def _finish(msg=None, **kw):
        finished.append(str(msg))
        raise FinishedException()

    return _finish


class TestRebootCommandGate:
    """/reboot 命令门禁（L25 同标准）：superuser 直执行；黑名单静默；非管理员明拒。"""

    @staticmethod
    def _event(user_id="20002", text="/reboot"):
        class _Ev:
            def get_message(self):
                return text

            def get_user_id(self):
                return user_id

            group_id = None

            def get_session_id(self):
                return "x"

        return _Ev()

    @pytest.mark.asyncio
    async def test_superuser_executes_directly(self, rb, monkeypatch):
        """定稿：superuser 无二次确认，直接回复+挂重启序列。"""
        monkeypatch.setenv("SUPERUSERS", "10000")
        monkeypatch.setattr(rb, "reboot_plan", lambda: ("execv", None))
        scheduled = []
        monkeypatch.setattr(rb, "schedule_reboot", lambda: scheduled.append(1))
        sent, finished = [], []
        monkeypatch.setattr(rb.reboot_cmd, "send", _mk_send(sent))
        monkeypatch.setattr(rb.reboot_cmd, "finish", _mk_finish(finished))
        # handler 用 reboot 模块级绑定（与 admin 测试同口径），必须 patch rb.*
        monkeypatch.setattr(rb, "is_allowed", lambda ev: True)
        monkeypatch.setattr(rb, "is_superuser", lambda uid: True)
        await rb.handle_reboot(self._event(user_id="10000"))
        assert sent and "重启" in sent[0]
        assert scheduled, "superuser 必须直接挂上重启序列"

    @pytest.mark.asyncio
    async def test_non_superuser_rejected(self, rb, monkeypatch):
        monkeypatch.setenv("SUPERUSERS", "10000")

        def _boom():
            raise AssertionError("非管理员不该被调度重启")

        monkeypatch.setattr(rb, "schedule_reboot", _boom)
        finished = []
        monkeypatch.setattr(rb.reboot_cmd, "finish", _mk_finish(finished))
        from nonebot.exception import FinishedException

        monkeypatch.setattr(rb, "is_allowed", lambda ev: True)
        monkeypatch.setattr(rb, "is_superuser", lambda uid: False)
        with pytest.raises(FinishedException):
            await rb.handle_reboot(self._event(user_id="20002"))
        assert finished and "只有管理员" in finished[0]

    @pytest.mark.asyncio
    async def test_blocked_user_silent(self, rb, monkeypatch, caplog):
        """黑名单命中：静默（只记日志），不发任何回复、不重启。"""
        monkeypatch.setenv("SUPERUSERS", "10000")

        def _boom():
            raise AssertionError("黑名单不该被调度重启")

        monkeypatch.setattr(rb, "schedule_reboot", _boom)
        sent, finished = [], []
        monkeypatch.setattr(rb.reboot_cmd, "send", _mk_send(sent))
        monkeypatch.setattr(rb.reboot_cmd, "finish", _mk_finish(finished))
        from nonebot.exception import FinishedException

        from plugins.qq_agent_adapter import acl

        # 黑名单：is_allowed False → deny 内部 is_blocked True → 静默 finish()
        monkeypatch.setattr(rb, "is_allowed", lambda ev: False)
        monkeypatch.setattr(acl, "is_blocked", lambda ev: True)
        with pytest.raises(FinishedException):
            await rb.handle_reboot(self._event(user_id="99999"))
        assert sent == []
        assert all(msg is None or msg == "None" or msg == "" for msg in finished)
        assert any("黑名单" in r.message for r in caplog.records)


class TestRebootWebFace:
    """/api/reboot：同写门禁、两段确认、审计、绝不真重启。"""

    URL = "/agent-web/api/reboot"

    def _mount(self, rb, monkeypatch, tmp_path, write="1", cidrs="10.0.0.0/8"):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from plugins.qq_agent_adapter import web

        envf = tmp_path / ".env"
        envf.write_text("AGENT_VISION=false\n", encoding="utf-8")
        monkeypatch.setenv("AGENT_WEB_TOKEN", "t0ken")
        monkeypatch.setenv("AGENT_WEB_BACKUP_DIR", str(tmp_path / "bk"))
        if cidrs is None:
            monkeypatch.delenv("AGENT_WEB_ALLOW_CIDRS", raising=False)
        else:
            monkeypatch.setenv("AGENT_WEB_ALLOW_CIDRS", cidrs)
        if write is None:
            monkeypatch.delenv("AGENT_WEB_WRITE", raising=False)
        else:
            monkeypatch.setenv("AGENT_WEB_WRITE", write)
        # 铁律：测试绝不真重启——schedule_reboot 全程替身
        monkeypatch.setattr(rb, "schedule_reboot", lambda: None)
        app = FastAPI()
        assert web.mount_web(app) is True
        return (
            TestClient(
                app,
                headers={"Authorization": "Bearer t0ken"},
                client=("10.1.2.3", 5000),
            ),
            envf,
        )

    def test_gate_404_without_write(self, rb, monkeypatch, tmp_path):
        c, _ = self._mount(rb, monkeypatch, tmp_path, write=None)
        assert c.post(self.URL, json={}).status_code == 404
        c, _ = self._mount(rb, monkeypatch, tmp_path, cidrs=None)
        assert c.post(self.URL, json={}).status_code == 404

    def test_no_token_401(self, rb, monkeypatch, tmp_path):
        from fastapi.testclient import TestClient

        c, _ = self._mount(rb, monkeypatch, tmp_path)
        # 源地址在白名单内但无 Bearer → 401（IP 闸先于 token 闸）
        no_tok = TestClient(c.app, client=("10.1.2.3", 5000))
        assert no_tok.post(self.URL, json={}).status_code == 401

    def test_two_phase_and_audit(self, rb, monkeypatch, tmp_path):
        from agentcore.diagnostics import clear as _clear
        from agentcore.diagnostics import recent as _recent

        c, _ = self._mount(rb, monkeypatch, tmp_path)
        _clear()
        try:
            r1 = c.post(self.URL, json={"reason": "apply env"}).json()
            assert r1["need_confirm"] is True
            assert r1["strategy"] in {"external", "execv", "exit"}
            r2 = c.post(
                self.URL,
                json={
                    "confirm_token": r1["confirm_token"],
                    "confirm_nonce": r1["confirm_nonce"],
                    "reason": "apply env",
                },
            )
            assert r2.status_code == 200 and r2.json()["ok"] is True
            ev = [e for e in _recent(5) if e["kind"] == "reboot_requested"][0]
            assert ev["by"].startswith("web:")
            assert ev["reason"] == "apply env"
        finally:
            _clear()

    def test_reason_bound_to_token(self, rb, monkeypatch, tmp_path):
        """确认码绑定 reason：换 reason 重放必须 400。"""
        c, _ = self._mount(rb, monkeypatch, tmp_path)
        r1 = c.post(self.URL, json={"reason": "a"}).json()
        r2 = c.post(
            self.URL,
            json={
                "confirm_token": r1["confirm_token"],
                "confirm_nonce": r1["confirm_nonce"],
                "reason": "b",
            },
        )
        assert r2.status_code == 400

    def test_reuse_rejected(self, rb, monkeypatch, tmp_path):
        c, _ = self._mount(rb, monkeypatch, tmp_path)
        r1 = c.post(self.URL, json={"reason": "x"}).json()
        payload = {
            "confirm_nonce": r1["confirm_nonce"],
            "confirm_token": r1["confirm_token"],
            "reason": "x",
        }
        assert c.post(self.URL, json=payload).status_code == 200
        assert c.post(self.URL, json=payload).status_code == 400

    def test_non_object_body_422(self, rb, monkeypatch, tmp_path):
        c, _ = self._mount(rb, monkeypatch, tmp_path)
        r = c.post(self.URL, json=[1, 2])
        assert r.status_code == 422 and r.json()["detail"] == "not_an_object"

    def test_page_has_reboot_button(self, rb):
        from plugins.qq_agent_adapter import web

        assert "rebootBot" in web._PAGE
        assert "api/reboot" in web._PAGE
        assert 'onclick="rebootBot()"' in web._PAGE
