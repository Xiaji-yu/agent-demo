"""scripts/self_update.py 的单测：前置检查、健康判定、状态落盘、拒绝路径。

真实的 SIGTERM→systemd 重启不在单测里跑（会动服务）；这里锁的是「要不要重启 /
要不要回滚」的决策面——它错了，线上就是一次没人发现的失败更新。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "self_update_script", ROOT / "scripts" / "self_update.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


update = _load()


def _proc(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class TestPreflight:
    def test_requires_update_ok(self):
        ok, reason = update.preflight({})
        assert ok is False
        assert "AGENT_UPDATE_OK" in reason, "dev/手工启动的实例必须被挡住"

    def test_rejects_wrong_branch(self, monkeypatch):
        monkeypatch.setattr(update, "git", lambda *a, **k: _proc(stdout="feat/x\n"))
        ok, reason = update.preflight({"AGENT_UPDATE_OK": "1"})
        assert ok is False
        assert "main" in reason

    def test_rejects_dirty_tree(self, monkeypatch):
        def fake_git(*args, **kwargs):
            if args[:2] == ("rev-parse", "--abbrev-ref"):
                return _proc(stdout="main\n")
            return _proc(stdout=" M agentcore/x.py\n")

        monkeypatch.setattr(update, "git", fake_git)
        ok, reason = update.preflight({"AGENT_UPDATE_OK": "1"})
        assert ok is False
        assert "工作树" in reason

    def test_runtime_paths_are_not_blocking(self):
        """data/ 与人格目录是运行时可写的：装个技能不该把更新永久挡住。"""
        assert update.blocking_dirty(["data/skills/new.yaml", "data/fonts/x.ttf"]) == []
        assert update.blocking_dirty(
            ["agentcore/personas/yun.md", "agentcore/x.py"]
        ) == ["agentcore/x.py"]

    def test_porcelain_parsing_handles_rename(self):
        text = " M agentcore/x.py\n?? data/skills/new.yaml\nR  old.py -> new.py\n"
        assert update.porcelain_paths(text) == [
            "agentcore/x.py",
            "data/skills/new.yaml",
            "new.py",
        ]

    def test_incoming_overlap_is_the_real_gate(self):
        """运行态改动允许，但上游这次改了同名文件就必须停——merge 会覆盖它。"""
        assert update.conflicts_with_incoming(
            ["data/skills/a.yaml"], ["data/skills/a.yaml", "agentcore/x.py"]
        ) == ["data/skills/a.yaml"]
        assert (
            update.conflicts_with_incoming(["data/skills/a.yaml"], ["agentcore/x.py"])
            == []
        )


class TestBootReady:
    @staticmethod
    def _wait(boot: Path, target: str, old_pid: int, *, rounds: int = 3) -> bool:
        clock = {"now": 0.0}

        def tick(_seconds):
            clock["now"] += 1.0

        return update.boot_ready(
            target,
            old_pid,
            path=boot,
            timeout=float(rounds),
            poll=1.0,
            clock=lambda: clock["now"],
            sleep=tick,
        )

    def test_requires_new_pid_and_target_sha(self, tmp_path: Path):
        boot = tmp_path / "boot.json"
        boot.write_text(json.dumps({"pid": 222, "sha": "abc123"}), encoding="utf-8")

        assert self._wait(boot, "abc123", 111)
        assert not self._wait(boot, "abc123", 222), "PID 没变说明进程还没重启"
        assert not self._wait(boot, "deadbeef", 111), "sha 不匹配说明跑的不是目标提交"

    def test_times_out_without_marker(self, tmp_path: Path):
        assert not self._wait(tmp_path / "missing.json", "abc", 1)


class TestStateFile:
    def test_write_state_is_private_and_atomic(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(update, "STATE_DIR", tmp_path)
        monkeypatch.setattr(update, "UPDATE_STATE", tmp_path / "update.json")

        update.write_state({"status": "ok", "to": "abc"})

        target = tmp_path / "update.json"
        assert json.loads(target.read_text(encoding="utf-8"))["status"] == "ok"
        assert target.stat().st_mode & 0o777 == 0o600
        assert not list(tmp_path.glob(".update-*")), "临时文件必须被 os.replace 吃掉"

    def test_main_refuses_without_gate_and_reports(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(update, "STATE_DIR", tmp_path)
        monkeypatch.setattr(update, "LOCK_FILE", tmp_path / "update.lock")
        monkeypatch.setattr(update, "UPDATE_STATE", tmp_path / "update.json")
        monkeypatch.setattr(update, "LOG_FILE", tmp_path / "update.log")
        monkeypatch.delenv("AGENT_UPDATE_OK", raising=False)

        rc = update.main(["--notify-to", "private:1"])

        assert rc == 2
        data = json.loads((tmp_path / "update.json").read_text(encoding="utf-8"))
        assert data["status"] == "refused"
        assert "AGENT_UPDATE_OK" in data["reason"], (
            "拒绝原因必须指向闸门本身；否则分不清是「没门槛」还是「分支不对」"
        )
        assert data["notify_to"] == "private:1"
