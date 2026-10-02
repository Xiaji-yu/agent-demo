"""reboot（/reboot + web 重启）核心测试：白名单 argv、执行链回落、停机编排。

执行链定稿（2026-09-29，用户选定 A 为主 + C/B 兜底）：
C 外部命令（白名单 argv spawn）→ B re-exec（预校验后自替换）→ A 退出兜底
（supervisor 拉起）。本文件只测解析/策略/编排/门禁，**绝不真重启**——
perform_reboot 的 spawn/execv/_exit 全部注入替身。
"""

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

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

        def fake_spawn(argv):
            # 替身返回 None = 跳过观察窗直接退出（真实实现返回 Popen）
            seen["argv"] = argv
            return None

        monkeypatch.setattr(rb, "_spawn_detached", fake_spawn)
        exits = []
        monkeypatch.setattr(os, "_exit", lambda code: exits.append(code))
        assert rb.perform_reboot() == "external"
        assert seen["argv"][0] == "docker"
        assert exits == [0]

    def test_external_child_alive_exits(self, rb, monkeypatch):
        """C 步观察窗内子进程仍活着（正常重启中）→ 照常退出让位。"""
        monkeypatch.setenv(rb.CMD_ENV, '["systemctl","restart","agent-demo"]')

        class _Alive:
            def poll(self):
                return None

        monkeypatch.setattr(rb, "_spawn_detached", lambda argv: _Alive())
        exits = []
        monkeypatch.setattr(os, "_exit", lambda code: exits.append(code))
        assert rb.perform_reboot(observe=0) == "external"
        assert exits == [0]

    def test_external_child_failed_falls_back_to_execv(self, rb, monkeypatch):
        """REVIEW-3ce6e0a..de09478 M3：子命令秒退非零（unit 名拼错/docker 未起）
        不能再退出等一个起不来的 supervisor——必须回退 re-exec。"""
        monkeypatch.setenv(rb.CMD_ENV, '["systemctl","restart","typo-unit"]')

        class _Failed:
            def __init__(self):
                self.calls = 0

            def poll(self):
                self.calls += 1
                return 1  # 立刻非零退出

        child = _Failed()
        monkeypatch.setattr(rb, "_spawn_detached", lambda argv: child)
        monkeypatch.setattr(rb, "_execv_target_ok", lambda: True)
        execv_calls = []
        monkeypatch.setattr(os, "execv", lambda a, b: execv_calls.append((a, b)))
        exits = []
        monkeypatch.setattr(os, "_exit", lambda code: exits.append(code))
        assert rb.perform_reboot(observe=0) == "execv"
        assert child.calls >= 1
        assert execv_calls and exits == []

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


class TestRebootDelayGuard:
    """REVIEW-3ce6e0a..de09478 L7：inf/nan 会让 asyncio.sleep 永不返回（永不
    重启且无告警）——lifecycle flush timeout 的 L6 同型护栏。"""

    def test_non_finite_falls_back_to_default(self, rb, monkeypatch):
        monkeypatch.setattr(rb, "DEFAULT_DELAY", 2.0)
        for bad in ("inf", "-inf", "nan", "1e400"):
            monkeypatch.setenv(rb.DELAY_ENV, bad)
            assert rb.reboot_delay() == 2.0, bad

    def test_normal_values_kept(self, rb, monkeypatch):
        monkeypatch.setenv(rb.DELAY_ENV, "0")
        assert rb.reboot_delay() == 0.0
        monkeypatch.setenv(rb.DELAY_ENV, "3.5")
        assert rb.reboot_delay() == 3.5
        monkeypatch.setenv(rb.DELAY_ENV, "-1")
        assert rb.reboot_delay() == 0.0  # 负值钳 0（既有行为）


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
        # REVIEW-3ce6e0a..de09478 M2：reboot 经 execv/_exit 退出，on_shutdown
        # 钩子永不执行——这里必须自己把 scheduler 递进停机序列（顺序第一环）
        assert "scheduler" in seen
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
        monkeypatch.setattr(
            rb, "schedule_reboot", lambda target=None: scheduled.append(target)
        )
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
        monkeypatch.setattr(rb, "schedule_reboot", lambda target=None: None)
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

    def test_first_phase_cmd_same_scope_as_audit(self, rb, monkeypatch, tmp_path):
        """REVIEW L8：第一段响应的 cmd 与审计同口径只回 argv[:1]，不回显完整
        argv（运维可能把连接串/凭据写进参数）。"""
        monkeypatch.setenv(
            rb.CMD_ENV, '["docker","--host","tcp://secret-host:2375","restart","x"]'
        )
        c, _ = self._mount(rb, monkeypatch, tmp_path)
        r1 = c.post(self.URL, json={"reason": "x"}).json()
        assert r1["cmd"] == ["docker"]

    def test_web_reboot_never_registers_notice(self, rb, monkeypatch, tmp_path):
        """REVIEW L11：web 重启无会话上下文，必须以 target=None 调度（不登记
        回执）——「web 重启不回执」承诺的用例锁。"""
        c, _ = self._mount(rb, monkeypatch, tmp_path)
        scheduled = []
        monkeypatch.setattr(
            rb, "schedule_reboot", lambda target=None: scheduled.append(target)
        )
        r1 = c.post(self.URL, json={"reason": "y"}).json()
        r2 = c.post(
            self.URL,
            json={
                "confirm_token": r1["confirm_token"],
                "confirm_nonce": r1["confirm_nonce"],
                "reason": "y",
            },
        )
        assert r2.status_code == 200
        assert scheduled == [None]

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


class TestRebootMatcherWiring:
    """/reboot **路由**回归（2026-09-30 线上事故：私聊 /reboot 不重启）。

    事故链：reboot.py 的 ``on_command`` 是**模块级注册**，而
    ``_load_plugin_modules()`` 从未 import 过 ``.reboot``（8575d6a 只补了 web
    handler 的懒 import）→ 线上进程里 reboot 模块根本不在，``/reboot`` 没有
    响应器，消息落给 priority=10 的普通聊天响应器，由 LLM 作答而不是重启。
    叠加第二因：事故进程 02:20 启动，而 reboot.py 02:37 才提交（鸡生蛋——
    /reboot 要用得先重启，重启原先只能手动）。

    为什么用子进程：rb fixture 会直接 import reboot 模块，注册动作因此发生在
    「绕过加载器」的路径上，同进程断言对「加载器漏 import」不敏感（变异实测：
    删掉接线后 trie/路由断言仍全绿 = 假阴性）。子进程从零 init，唯一导入路径
    就是加载器，与生产启动等价。

    本类锁两层：① 生产加载路径之后 ``/reboot`` 真注册；② 私聊消息经
    ``handle_event`` 真路由到 reboot 响应器（而非聊天响应器）。
    handler 行为（权限/黑名单/停机编排）由 TestRebootCommandGate 覆盖。
    """

    LOADER_SCRIPT = """
import asyncio, os, sys
os.environ["SUPERUSERS"] = '["2224513919"]'
from nonebot import init, get_driver
from nonebot.adapters.onebot.v11 import Adapter
init(); get_driver().register_adapter(Adapter)
import plugins.qq_agent_adapter as pkg
pkg._load_plugin_modules()
from nonebot.rule import TrieRule
print("REBOOT_ROUTE:", pkg.reboot_route is not None)
for prefix in ("/reboot", "/重启"):
    print("PREFIX:", prefix, prefix in TrieRule.prefix)
from nonebot.matcher import matchers as reg
print(
    "MATCHER:",
    any(
        getattr(m, "plugin", None) is not None and m.__module__ == "nonebot.internal.matcher.matcher"
        for prio in reg.keys() for m in reg[prio]
    ),
)
print("CHAT_MATCHERS:", sum(1 for prio in reg.keys() for m in reg[prio]))
from nonebot.internal.driver.abstract import Driver as _Driver
print(
    "BOT_CONNECT_HOOKS:",
    "|".join(
        sorted(
            getattr(dep.call, "__name__", str(dep.call))
            for dep in _Driver._bot_connection_hook
        )
    ),
)
"""

    DISPATCH_SCRIPT = """
import asyncio, os
os.environ["SUPERUSERS"] = '["2224513919"]'
# 防止子进程真跑重启序列：宽限拉到 1 小时，脚本退出即取消未落地的 task
os.environ["AGENT_REBOOT_DELAY"] = "3600"
from nonebot import init, get_driver
from nonebot.adapters.onebot.v11 import Adapter
init(); get_driver().register_adapter(Adapter)
import plugins.qq_agent_adapter as pkg
pkg._load_plugin_modules()
from nonebot.adapters.onebot.v11 import PrivateMessageEvent
from nonebot.message import handle_event

async def main():
    event = PrivateMessageEvent.parse_obj({
        "time": 0, "self_id": 999, "post_type": "message", "sub_type": "friend",
        "user_id": 2224513919, "message_type": "private", "message_id": 1,
        "message": [{"type": "text", "data": {"text": "/reboot"}}],
        "original_message": [{"type": "text", "data": {"text": "/reboot"}}],
        "raw_message": "/reboot", "font": 0,
        "sender": {"user_id": 2224513919, "nickname": "", "card": ""},
        "to_me": False, "reply": None,
    })

    class _StubBot:
        type = "StubBot"
        self_id = "999"

        async def call_api(self, *a, **k):
            return {}

        async def send(self, *a, **k):
            return {}

    try:
        await handle_event(_StubBot(), event)
    except Exception as e:
        print("DISPATCH_ERROR:", repr(e))
    else:
        print("DISPATCH_OK")

asyncio.run(main())
"""

    @classmethod
    def _run_fresh_process(cls, script: str) -> tuple[str, str]:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, f"子进程失败：\n{proc.stderr[-2000:]}"
        return proc.stdout, proc.stderr

    def test_reboot_module_imported_by_loader(self):
        """_load_plugin_modules 必须把 .reboot 纳进来（本次事故的根因）。"""
        out, _ = self._run_fresh_process(self.LOADER_SCRIPT)
        assert "REBOOT_ROUTE: True" in out, out

    def test_reboot_prefix_registered(self):
        out, _ = self._run_fresh_process(self.LOADER_SCRIPT)
        for prefix in ("/reboot", "/重启"):
            assert f"PREFIX: {prefix} True" in out, (prefix, out)

    def test_reboot_done_notice_hook_registered(self):
        """REVIEW-3ce6e0a..de09478 L11：回执钩子是模块级 on_bot_connect 注册，
        漏 import 模块即静默失效（漏接线事故的回执版）——必须在加载器路径上锁。"""
        out, _ = self._run_fresh_process(self.LOADER_SCRIPT)
        hooks_line = out.split("BOT_CONNECT_HOOKS:")[1].splitlines()[0]
        assert "_send_reboot_done" in hooks_line, out

    def test_private_reboot_routes_to_reboot_matcher(self):
        """端到端：私聊 /reboot 由 reboot 响应器接住，聊天响应器不接手。

        判据是 reboot handler 自己的审计日志（stdlib logging，WARNING 级必然
        落到 stderr）：``reboot: 管理员 2224513919 触发重启``。这行只在
        ``handle_reboot`` 通过 ACL/superuser 闸门之后打印——消息若落给
        priority=10 的聊天响应器（线上事故形态），它永远不会出现。
        脚本本身**不 import reboot 模块**——否则注册又发生在绕过加载器的路径上
        （子进程隔离同理：先 import 过就不再敏感）。
        """
        out, err = self._run_fresh_process(self.DISPATCH_SCRIPT)
        assert "DISPATCH_OK" in out, (out, err[-1500:])
        assert "触发重启" in err and "2224513919" in err, err[-1500:]
        assert "DISPATCH_ERROR" not in out, (out, err[-1500:])


class TestRebootDoneNotice:
    """重启完毕回执（2026-09-30 新增）：登记文件 + bot 连接钩子 + 耗时。

    用户诉求：「只有即将重启的提示，没有相同路由的重启完毕提示，加上耗时」。
    跨进程是该功能的核心难点：起始时间与目标会话必须落盘（原子写），新进程
    由 on_bot_connect 一次性消费。
    """

    @pytest.fixture(autouse=True)
    def _notice_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv(
            "AGENT_REBOOT_NOTICE_FILE", str(tmp_path / "reboot-notice.json")
        )

    def _path(self) -> Path:
        import os

        return Path(os.environ["AGENT_REBOOT_NOTICE_FILE"])

    # ---------- 登记/消费 ----------
    def test_write_and_consume_roundtrip(self, rb):
        rb.write_reboot_notice("private:10000", 1000.0)
        notice = rb.consume_reboot_notice()
        assert notice == {"target": "private:10000", "started_at": 1000.0, "by": "-"}
        assert not self._path().exists(), "消费后必须删除（一次性）"
        assert rb.consume_reboot_notice() is None

    def test_consume_missing_is_none(self, rb):
        assert rb.consume_reboot_notice() is None

    def test_consume_corrupt_dropped(self, rb):
        for bad in (
            "not json",
            '{"target": 123}',
            '{"target": "private:1", "started_at": "x"}',
        ):
            self._path().write_text(bad, encoding="utf-8")
            assert rb.consume_reboot_notice() is None, bad
            assert not self._path().exists(), "坏登记必须被清掉，否则每次启动都告警"

    # ---------- 生产者 ----------
    @pytest.mark.asyncio
    async def test_reboot_after_reply_registers_target(self, rb, monkeypatch):
        """perform_reboot 之前必须落盘回执（含起始时间，覆盖宽限+停机全程）。"""
        calls = []
        monkeypatch.setattr(rb, "reboot_delay", lambda: 0.0)

        async def _noop_shutdown():
            calls.append("shutdown")

        monkeypatch.setattr(rb, "_shutdown_everything", _noop_shutdown)
        monkeypatch.setattr(rb, "perform_reboot", lambda **kw: calls.append("reboot"))
        before = time.time()
        await rb.reboot_after_reply(target="private:2224513919")
        notice = rb.consume_reboot_notice()
        assert calls == ["shutdown", "reboot"]
        assert notice["target"] == "private:2224513919"
        assert before <= notice["started_at"] <= time.time()

    # ---------- 消费者 ----------
    @staticmethod
    def _stub_bot():
        class _Bot:
            def __init__(self):
                self.sent = []

            async def send_private_msg(self, user_id, message):
                self.sent.append(("private", user_id, message))

            async def send_group_msg(self, group_id, message):
                self.sent.append(("group", group_id, message))

        return _Bot()

    @pytest.mark.asyncio
    async def test_send_done_private_with_elapsed(self, rb):
        rb.write_reboot_notice("private:2224513919", time.time() - 5)
        bot = self._stub_bot()
        await rb._send_reboot_done(bot)
        assert len(bot.sent) == 1
        kind, uid, text = bot.sent[0]
        assert kind == "private" and uid == 2224513919
        assert "重启完毕" in text
        assert "耗时 5." in text or "耗时 4.9" in text or "耗时 5.0" in text
        assert rb.consume_reboot_notice() is None, "回执一次性，不能重复发"

    @pytest.mark.asyncio
    async def test_send_done_group(self, rb):
        rb.write_reboot_notice("group:1051425116", time.time())
        bot = self._stub_bot()
        await rb._send_reboot_done(bot)
        assert bot.sent and bot.sent[0][0] == "group"
        assert bot.sent[0][1] == 1051425116

    @pytest.mark.asyncio
    async def test_send_done_silent_without_notice(self, rb):
        bot = self._stub_bot()
        await rb._send_reboot_done(bot)
        assert bot.sent == [], "正常启动（非 /reboot）不能发声"

    @pytest.mark.asyncio
    async def test_send_done_bad_target_no_send(self, rb):
        for target in ("bogus:1", "private:abc", "private:", "", "group:１２３"):
            rb.write_reboot_notice(target, time.time())
            bot = self._stub_bot()
            await rb._send_reboot_done(bot)
            assert bot.sent == [], target
            assert rb.consume_reboot_notice() is None, target

    @pytest.mark.asyncio
    async def test_handle_reboot_passes_chat_target(self, rb, monkeypatch):
        """handler 必须把命令所在会话传给回执（群→group:，私聊→private:）。"""
        monkeypatch.setenv("SUPERUSERS", "2224513919")
        monkeypatch.setattr(rb, "reboot_plan", lambda: ("execv", None))
        scheduled = []
        monkeypatch.setattr(
            rb, "schedule_reboot", lambda target=None: scheduled.append(target)
        )
        monkeypatch.setattr(rb, "is_allowed", lambda ev: True)
        sent = []
        monkeypatch.setattr(rb.reboot_cmd, "send", _mk_send(sent))

        from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent

        priv = PrivateMessageEvent.parse_obj(
            {
                "time": 0,
                "self_id": 999,
                "post_type": "message",
                "sub_type": "friend",
                "user_id": 2224513919,
                "message_type": "private",
                "message_id": 1,
                "message": [{"type": "text", "data": {"text": "/reboot"}}],
                "original_message": [{"type": "text", "data": {"text": "/reboot"}}],
                "raw_message": "/reboot",
                "font": 0,
                "sender": {"user_id": 2224513919, "nickname": "", "card": ""},
                "to_me": False,
                "reply": None,
            }
        )
        grp = GroupMessageEvent.parse_obj(
            {
                "time": 0,
                "self_id": 999,
                "post_type": "message",
                "message_type": "group",
                "sub_type": "normal",
                "group_id": 1051425116,
                "user_id": 2224513919,
                "message_id": 2,
                "message": [{"type": "text", "data": {"text": "/reboot"}}],
                "original_message": [{"type": "text", "data": {"text": "/reboot"}}],
                "raw_message": "/reboot",
                "font": 0,
                "sender": {"user_id": 2224513919, "nickname": "", "card": ""},
                "to_me": False,
                "reply": None,
            }
        )
        for ev, expect in ((priv, "private:2224513919"), (grp, "group:1051425116")):
            scheduled.clear()
            await rb.handle_reboot(ev)
            assert scheduled == [expect], (expect, scheduled)
