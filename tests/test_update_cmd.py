"""`/update` 命令面：权限、参数解析、回执文案、systemd-run 接线。

不测真实更新：脚本本身由 tests/test_self_update.py 覆盖决策面，这里只锁
「谁来触发、怎么拉起、说什么话」——历史教训是模块级 on_command 漏接线后
命令静默落到聊天响应器（2026-09-30 /reboot 事故）。
"""

from __future__ import annotations

import importlib
import json
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
        assert any(a.startswith("--unit=agent-demo-update-") for a in argv)
        assert argv[-2:] == ["--notify-to", "private:1"]
        assert str(update_mod.ROOT / "scripts" / "self_update.py") in argv
        assert captured["env"]["XDG_RUNTIME_DIR"].startswith("/run/user/")
