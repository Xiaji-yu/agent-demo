"""`/update` 命令面：权限、参数解析、回执文案、systemd-run 接线。

不测真实更新：脚本本身由 tests/test_self_update.py 覆盖决策面，这里只锁
「谁来触发、怎么拉起、说什么话」——历史教训是模块级 on_command 漏接线后
命令静默落到聊天响应器（2026-09-30 /reboot 事故）。
"""

from __future__ import annotations

import fcntl
import importlib
import json
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent
from nonebot.exception import FinishedException

update_mod = None


@pytest.fixture(scope="module", autouse=True)
def _load_module():
    global update_mod
    import nonebot

    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init(_env_file=None, superusers={"10000"})
    update_mod = importlib.import_module("plugins.qq_agent_adapter.update_cmd")


class _FakeMatcher:
    def __init__(self):
        self.finished: str | None = None
        self.sent: list[str] = []

    async def finish(self, msg=None, **kwargs):
        self.finished = msg
        raise FinishedException

    async def send(self, msg=None, **kwargs):
        self.sent.append(msg)


def _private_event(text: str, user_id: int = 10000) -> PrivateMessageEvent:
    segs = [{"type": "text", "data": {"text": text}}]
    return PrivateMessageEvent.parse_obj(
        {
            "time": 0,
            "self_id": 1,
            "post_type": "message",
            "sub_type": "friend",
            "user_id": user_id,
            "message_type": "private",
            "message_id": 1,
            "message": segs,
            "original_message": segs,
            "raw_message": text,
            "font": 0,
            "sender": {"user_id": user_id, "nickname": ""},
            "to_me": False,
            "reply": None,
        }
    )


def _group_event(text: str, user_id: int = 10000, group_id: int = 456):
    segs = [{"type": "text", "data": {"text": text}}]
    return GroupMessageEvent.parse_obj(
        {
            "time": 0,
            "self_id": 1,
            "post_type": "message",
            "sub_type": "group",
            "user_id": user_id,
            "message_type": "group",
            "message_id": 1,
            "group_id": group_id,
            "message": segs,
            "original_message": segs,
            "raw_message": text,
            "font": 0,
            "sender": {"user_id": user_id, "nickname": "", "card": ""},
            "to_me": False,
            "reply": None,
            "anonymous": None,
        }
    )


async def _run(monkeypatch, event, *, superuser=True, spawn=None):
    matcher = _FakeMatcher()
    monkeypatch.setattr(update_mod, "update_cmd", matcher)
    monkeypatch.setattr(update_mod, "is_allowed", lambda _e: True)
    monkeypatch.setattr(update_mod, "is_superuser", lambda _uid: superuser)
    watched: list[str] = []
    monkeypatch.setattr(update_mod, "_start_watcher", watched.append)
    if spawn is not None:
        monkeypatch.setattr(update_mod, "_spawn_update", spawn)
    try:
        await update_mod.handle_update(event)
    except FinishedException:
        pass
    return matcher, watched


class TestParsing:
    def test_parse_args(self):
        assert update_mod.parse_update_arg("/update") == "run"
        assert update_mod.parse_update_arg("/update   ") == "run"
        assert update_mod.parse_update_arg("/update status") == "status"
        assert update_mod.parse_update_arg("/update 状态") == "status"
        assert update_mod.parse_update_arg("/update now") == "unknown"

    def test_reply_target(self):
        assert update_mod.reply_target(_private_event("/update")) == "private:10000"
        assert update_mod.reply_target(_group_event("/update")) == "group:456"


class TestNotice:
    def test_formats_each_status(self):
        assert "更新完成" in update_mod.format_notice(
            {"status": "ok", "from": "aaa", "to": "bbb"}
        )
        assert "已是最新" in update_mod.format_notice({"status": "up_to_date"})
        rolled = update_mod.format_notice(
            {
                "status": "rolled_back",
                "from": "aaa",
                "to": "bbb",
                "reason": "import 失败",
            }
        )
        assert "已自动回滚" in rolled and "import 失败" in rolled
        assert "更新失败" in update_mod.format_notice({"status": "failed"})
        assert "未更新" in update_mod.format_notice(
            {"status": "refused", "reason": "脏树"}
        )

    def test_stale_notice_is_not_replayed(self):
        """几小时前的失败结果不该在每次重启后又弹一遍（22:35 实测踩中）。"""
        boot = 10_000.0
        fresh = {"started_at": boot - 5}
        stale = {"started_at": boot - 3600}
        assert update_mod.is_stale_notice(fresh, boot) is False
        assert update_mod.is_stale_notice(stale, boot) is True
        assert update_mod.is_stale_notice({}, boot) is False
        assert update_mod.is_stale_notice(stale, None) is False

    def test_slow_update_result_is_not_stale(self):
        """M2：慢更新（合法 >600s）的终态不得被判 stale——否则永远无回执。

        fetch 180s + merge 120s + uv sync 900s + import 180s 轻松超过旧窗口，
        此时 started_at 远早于 boot_started，但 finished_at 就在重启前后。
        旧判据把**本次**的成功/回滚当旧账跳过，notified 永不置位，之后每次
        重启同样 stale——用户只收到过一句「开始更新」。
        """
        boot = 10_000.0
        slow = {"started_at": boot - 1400.0, "finished_at": boot - 1.0}
        assert update_mod.is_stale_notice(slow, boot) is False
        # 真正旧的结果（终态落盘于几小时前）仍然 stale
        old = {"started_at": boot - 7200.0, "finished_at": boot - 7000.0}
        assert update_mod.is_stale_notice(old, boot) is True
        # 缺 finished_at 时回退 started_at（旧行为不丢）
        assert update_mod.is_stale_notice({"started_at": boot - 5}, boot) is False
        assert update_mod.is_stale_notice({"started_at": boot - 3600}, boot) is True

    @pytest.mark.asyncio
    async def test_watcher_outlives_slow_fetch_failure(self, monkeypatch, tmp_path):
        """M3：watcher 预算必须超过脚本各步超时之和，否则回执面整个丢失。

        旧值 180s 与 git fetch 的 timeout 压线：fetch 挂满后脚本约 182s 才写
        failed，watcher 已静默退出——而「更新前失败」正是设计派给旧进程的回执。
        这里用虚拟时钟跑真实 _watch_result：终态在 182s 处才出现。
        """
        now = {"t": 1_000.0}

        def fake_time():
            return now["t"]

        async def fake_sleep(seconds):
            now["t"] += float(seconds)

        monkeypatch.setattr(update_mod, "time", types.SimpleNamespace(time=fake_time))
        monkeypatch.setattr(
            update_mod, "asyncio", types.SimpleNamespace(sleep=fake_sleep)
        )
        monkeypatch.setattr(update_mod, "STATE_DIR", tmp_path)
        monkeypatch.setattr(update_mod, "UPDATE_STATE", tmp_path / "update.json")
        sent: list[str] = []

        async def fake_send(target, text):
            sent.append(text)
            return True

        monkeypatch.setattr(update_mod, "_send", fake_send)

        def fake_read(_path):
            # 脚本最坏的前重启链：fetch 挂满 180s → ~182s 处才落 failed
            if now["t"] >= 1_182.0:
                return {
                    "status": "failed",
                    "reason": "git fetch 失败：timed out",
                    "notify_to": "private:10000",
                }
            return {}

        monkeypatch.setattr(update_mod, "_read", fake_read)

        await update_mod._watch_result("private:10000")

        assert sent, "fetch 挂满后的失败回执必须由旧进程 watcher 补发"
        assert "更新失败" in sent[0]

    def test_status_text_reads_state(self, tmp_path, monkeypatch):
        boot = tmp_path / "boot.json"
        last = tmp_path / "update.json"
        boot.write_text(
            json.dumps({"sha": "abc123456789", "branch": "main", "version": "1.0.4"}),
            encoding="utf-8",
        )
        last.write_text(
            json.dumps({"status": "ok", "from": "a", "to": "b"}), encoding="utf-8"
        )
        monkeypatch.setattr(update_mod, "BOOT_STATE", boot)
        monkeypatch.setattr(update_mod, "UPDATE_STATE", last)

        text = update_mod.status_text()

        assert "abc123456789" in text and "1.0.4" in text
        assert "ok a → b" in text


class TestUpdateInterlock:
    """M4：``/update`` 与 ``/reboot`` 的互锁判据（reboot 两个入口共用）。

    并发重启（``os.execve`` PID 不变）会让 ``self_update.boot_ready`` 的
    「pid 变了」永不满足，90s 后 ``reset --hard`` 回滚刚更新的代码、状态写
    ``rolled_back``——回执显示「启动校验未通过」，真实原因是并发重启。
    判据必须是**活的**：脚本被杀后 state 里的 ``restarting`` 会永远留下，
    按它拦截会把 /reboot 这个恢复手段永久挡死。
    """

    def _patch_state(self, monkeypatch, tmp_path):
        monkeypatch.setattr(update_mod, "STATE_DIR", tmp_path)
        monkeypatch.setattr(update_mod, "UPDATE_STATE", tmp_path / "update.json")

    def test_lock_held_blocks(self, tmp_path, monkeypatch):
        """脚本持锁（存活）→ 有拒绝原因。"""
        self._patch_state(monkeypatch, tmp_path)
        holder = (tmp_path / "update.lock").open("a+")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            reason = update_mod.update_in_progress()
        finally:
            holder.close()
        assert reason and "更新" in reason

    def test_lock_free_allows(self, tmp_path, monkeypatch):
        """没有更新在跑（锁空、无 state）→ 空串。"""
        self._patch_state(monkeypatch, tmp_path)
        assert update_mod.update_in_progress() == ""

    def test_dead_script_state_does_not_block(self, tmp_path, monkeypatch, caplog):
        """脚本已死但 state 停在 restarting（僵尸态）→ 放行并留 WARNING。

        机器重启会清掉 ``systemd-run --user`` 的 unit，脚本没了而 state 永远
        停在 restarting——此时 /reboot 是恢复手段，不能被死账挡死。
        """
        self._patch_state(monkeypatch, tmp_path)
        (tmp_path / "update.json").write_text(
            json.dumps({"status": "restarting", "started_at": 1.0}), encoding="utf-8"
        )
        with caplog.at_level("WARNING", logger="plugins.qq_agent_adapter.update_cmd"):
            assert update_mod.update_in_progress() == ""
        assert "更新锁已释放" in caplog.text

    def test_early_phase_also_blocks(self, tmp_path, monkeypatch):
        """脚本已起来但还没写 state（preflight/fetch 阶段）同样在窗口内。

        这一条关掉 /update 与 /reboot 之间的竞态：spawn 后到写 restarting
        之前有几百毫秒空档，只看 state 会漏过整段 fetch/merge/sync。
        """
        self._patch_state(monkeypatch, tmp_path)
        holder = (tmp_path / "update.lock").open("a+")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            assert update_mod.update_in_progress() != ""
        finally:
            holder.close()


class TestHandler:
    @pytest.mark.asyncio
    async def test_non_superuser_rejected(self, monkeypatch):
        matcher, watched = await _run(
            monkeypatch, _private_event("/update"), superuser=False
        )
        assert matcher.finished == "只有管理员能执行更新。"
        assert not watched

    @pytest.mark.asyncio
    async def test_status_is_read_only(self, monkeypatch, tmp_path):
        monkeypatch.setattr(update_mod, "BOOT_STATE", tmp_path / "boot.json")
        monkeypatch.setattr(update_mod, "UPDATE_STATE", tmp_path / "update.json")
        matcher, watched = await _run(monkeypatch, _private_event("/update status"))
        assert "当前运行" in str(matcher.finished)
        assert not watched

    @pytest.mark.asyncio
    async def test_refuses_outside_production(self, monkeypatch):
        monkeypatch.delenv("AGENT_UPDATE_OK", raising=False)
        matcher, watched = await _run(monkeypatch, _private_event("/update"))
        assert "AGENT_UPDATE_OK" in str(matcher.finished)
        assert not watched

    @pytest.mark.asyncio
    async def test_refuses_when_previous_update_running(self, monkeypatch, tmp_path):
        import time

        monkeypatch.setenv("AGENT_UPDATE_OK", "1")
        state = tmp_path / "update.json"
        state.write_text(
            json.dumps({"status": "restarting", "started_at": time.time()}),
            encoding="utf-8",
        )
        monkeypatch.setattr(update_mod, "UPDATE_STATE", state)

        matcher, watched = await _run(monkeypatch, _private_event("/update"))

        assert "未开始" in str(matcher.finished)
        assert not watched

    @pytest.mark.asyncio
    async def test_starts_update_and_passes_group_target(self, monkeypatch):
        monkeypatch.setenv("AGENT_UPDATE_OK", "1")
        monkeypatch.setattr(
            update_mod, "UPDATE_STATE", update_mod.STATE_DIR / "nope.json"
        )
        calls: list[str] = []

        def fake_spawn(target: str):
            calls.append(target)
            return True, ""

        matcher, watched = await _run(
            monkeypatch,
            _group_event("/update", group_id=456),
            spawn=fake_spawn,
        )

        assert calls == ["group:456"]
        assert watched == ["group:456"], "重启后的回执要靠新进程，旧进程要先挂 watcher"
        assert any("开始更新" in str(m) for m in matcher.sent)

    @pytest.mark.asyncio
    async def test_spawn_failure_is_reported(self, monkeypatch):
        monkeypatch.setenv("AGENT_UPDATE_OK", "1")
        monkeypatch.setattr(
            update_mod, "UPDATE_STATE", update_mod.STATE_DIR / "nope.json"
        )

        def fake_spawn(_target: str):
            return False, "systemd-run 不可用"

        matcher, watched = await _run(
            monkeypatch, _private_event("/update"), spawn=fake_spawn
        )

        assert "启动更新脚本失败" in str(matcher.finished)
        assert not watched


class TestUpdateMatcherWiring:
    """/update 必须由 ``_load_plugin_modules()`` 真正 import（2026-09-30 /reboot 教训）。

    同进程断言对「加载器漏 import」不敏感——fixture 自己已经 import 过模块了。
    所以用子进程从零 init，唯一导入路径就是加载器，与生产启动等价。
    """

    SCRIPT = """
import os
os.environ["SUPERUSERS"] = '["2224513919"]'
from nonebot import init, get_driver
from nonebot.adapters.onebot.v11 import Adapter
init(); get_driver().register_adapter(Adapter)
import plugins.qq_agent_adapter as pkg
pkg._load_plugin_modules()
from nonebot.rule import TrieRule
print("UPDATE_ROUTE:", pkg.update_route is not None)
print("PREFIX:", "/update" in TrieRule.prefix)
"""

    def test_loader_registers_update_command(self):
        import subprocess
        import sys

        proc = subprocess.run(
            [sys.executable, "-c", self.SCRIPT],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True,
            text=True,
            timeout=120,
        )
        out = proc.stdout + proc.stderr
        assert "UPDATE_ROUTE: True" in out, out[-800:]
        assert "PREFIX: True" in out, out[-800:]


class TestSpawner:
    def test_uses_systemd_run_in_its_own_unit(self, monkeypatch):
        captured: dict = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["env"] = kwargs.get("env")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(update_mod.subprocess, "run", fake_run)

        ok, reason = update_mod._spawn_update("private:1")

        assert ok, reason
        argv = captured["argv"]
        assert argv[0] == "systemd-run"
        assert "--user" in argv, (
            "必须跑在用户管理器里，否则会被服务重启的 cgroup 清理杀掉"
        )
        assert "--setenv=AGENT_UPDATE_OK=1" in argv, (
            "用户管理器不继承 system unit 的 Environment=，闸门必须显式带过去"
            "（2026-11-02 首次 /update 实测：脚本因缺少该变量拒绝执行）"
        )
        assert "--no-block" in argv, (
            "必须异步拉起：否则 handler 会一直等到更新（含重启）结束"
        )
        assert any(a.startswith("--unit=agent-demo-update-") for a in argv)
        assert argv[-2:] == ["--notify-to", "private:1"]
        assert str(update_mod.ROOT / "scripts" / "self_update.py") in argv
        assert captured["env"]["XDG_RUNTIME_DIR"].startswith("/run/user/")
