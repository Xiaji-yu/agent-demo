"""scripts/self_update.py 的单测：前置检查、健康判定、状态落盘、拒绝路径。

真实的 SIGTERM→systemd 重启不在单测里跑（会动服务）；这里锁的是「要不要重启 /
要不要回滚」的决策面——它错了，线上就是一次没人发现的失败更新。
"""

from __future__ import annotations

import importlib.util
import json
import os
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
        monkeypatch.setattr(update, "main_pid", lambda: 4242)
        monkeypatch.setattr(update, "git", lambda *a, **k: _proc(stdout="feat/x\n"))
        ok, reason = update.preflight({"AGENT_UPDATE_OK": "1"})
        assert ok is False
        assert "main" in reason

    def test_rejects_dirty_tree(self, monkeypatch):
        monkeypatch.setattr(update, "main_pid", lambda: 4242)

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

    def test_requires_restart_target_before_touching_code(self, monkeypatch):
        """M1：重启目标必须在 fetch 之前就位，否则 user_mode 下白拉代码再回滚。"""
        monkeypatch.setattr(update, "main_pid", lambda: None)
        ok, reason = update.preflight({"AGENT_UPDATE_OK": "1"})
        assert ok is False
        assert "主进程号" in reason
        assert "拒绝在改代码之后才发现无法重启" in reason

    def test_main_pid_verifies_cmdline_ownership(self, monkeypatch, tmp_path):
        """M1：同名残留 unit 的 PID 不得采信——按 cmdline 校验归属。"""
        seen = []

        def fake_run(argv, **kwargs):
            seen.append(argv)
            # system 总线返回一个不属于本 checkout 的 PID（残留 unit）
            return _proc(stdout="999999\n")

        monkeypatch.setattr(update, "_run", fake_run)
        monkeypatch.setattr(update, "_pid_runs_this_checkout", lambda pid: False)
        assert update.main_pid() is None
        assert seen == [
            ["systemctl", "show", "-p", "MainPID", "--value", update.UNIT],
            ["systemctl", "--user", "show", "-p", "MainPID", "--value", update.UNIT],
        ], "system 总线的 PID 归属不对时还要试 user 总线（deploy.py --user 形态）"

        monkeypatch.setattr(update, "_pid_runs_this_checkout", lambda pid: True)
        assert update.main_pid() == 999999

    def test_pid_ownership_check_reads_proc(self, monkeypatch):
        """_pid_runs_this_checkout：cmdline 含 bot.py 且路径属于本 checkout。"""
        monkeypatch.setattr(
            update.Path, "read_bytes", lambda self: b"/usr/bin/python\0bot.py\0"
        )
        monkeypatch.setattr(update, "ROOT", Path("/opt/agent"))
        assert update._pid_runs_this_checkout(1) is False  # 路径不是本 checkout
        monkeypatch.setattr(
            update.Path,
            "read_bytes",
            lambda self: b"/opt/agent/.venv/bin/python\0/opt/agent/bot.py\0",
        )
        assert update._pid_runs_this_checkout(1) is True

        # 读不到 /proc（非 Linux / 进程已退）：验不了归属就不采信
        def _boom(self):
            raise OSError("no proc")

        monkeypatch.setattr(update.Path, "read_bytes", _boom)
        assert update._pid_runs_this_checkout(1) is False

    def test_pid_ownership_rejects_prefix_overlap(self, monkeypatch):
        """M3（REVIEW-6867995..e079b8d）：路径前缀重叠的残留 checkout 不得采信。

        ``agent-demo.bak`` / ``agent-demo2`` 的 cmdline 同样含 ``bot.py`` 与 ROOT
        子串——裸子串匹配会误采信并 SIGTERM 旧部署的进程；按 argv 解出脚本
        路径、realpath 后必须逐段等于本 checkout 的 bot.py。
        """
        monkeypatch.setattr(update, "ROOT", Path("/srv/agent-demo"))
        for intruder in ("/srv/agent-demo.bak", "/srv/agent-demo2"):
            payload = f"{intruder}/.venv/bin/python\0{intruder}/bot.py\0".encode()
            monkeypatch.setattr(update.Path, "read_bytes", lambda self, _p=payload: _p)
            assert update._pid_runs_this_checkout(1) is False, (
                f"{intruder} 是别的 checkout，归属校验必须拒绝"
            )
        monkeypatch.setattr(
            update.Path,
            "read_bytes",
            lambda self: b"/srv/agent-demo/.venv/bin/python\0/srv/agent-demo/bot.py\0",
        )
        assert update._pid_runs_this_checkout(1) is True

    def test_pid_ownership_resolves_relative_script_via_proc_cwd(self, monkeypatch):
        """deploy.py 的 ExecStart 形态是相对路径 ``bot.py``——按 /proc/<pid>/cwd 解析。

        cwd 读不到（权限 / 已退出）就验不了归属，fail-closed 不采信。
        """
        monkeypatch.setattr(update, "ROOT", Path("/srv/agent-demo"))
        monkeypatch.setattr(
            update.Path,
            "read_bytes",
            lambda self: b"/srv/agent-demo/.venv/bin/python\0bot.py\0",
        )
        monkeypatch.setattr(update.os, "readlink", lambda _p: "/srv/agent-demo")
        assert update._pid_runs_this_checkout(7) is True

        def _boom(_p):
            raise OSError("permission")

        monkeypatch.setattr(update.os, "readlink", _boom)
        assert update._pid_runs_this_checkout(7) is False


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

    def test_terminal_states_get_finished_at(self, tmp_path: Path, monkeypatch):
        """M2：终态一律自动补 finished_at——bot 侧 stale 判据读它。

        漏一处的后果：慢更新（fetch+merge+uv sync+import 合法 >600s）时新进程
        按 started_at 把本次结果判成旧账，回执永远不发。显式给了值不被覆盖；
        运行态（restarting/rollingback）不补——它会被随后的终态覆写。
        """
        monkeypatch.setattr(update, "STATE_DIR", tmp_path)
        monkeypatch.setattr(update, "UPDATE_STATE", tmp_path / "update.json")

        update.write_state({"status": "failed", "reason": "x"})
        data = json.loads((tmp_path / "update.json").read_text(encoding="utf-8"))
        assert isinstance(data["finished_at"], float)

        update.write_state({"status": "restarting"})
        data = json.loads((tmp_path / "update.json").read_text(encoding="utf-8"))
        assert "finished_at" not in data

        update.write_state({"status": "ok", "finished_at": 1.0})
        data = json.loads((tmp_path / "update.json").read_text(encoding="utf-8"))
        assert data["finished_at"] == 1.0

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


class TestRollbackRuntimeBackup:
    """M5：回滚前必须把 RUNTIME_PREFIXES 内被放行的已跟踪修改落备份。"""

    def _fake_git(self, calls):
        def fake_git(*args, **kwargs):
            calls.append(args)
            if args[:2] == ("diff", "--binary") or "--binary" in args:
                return _proc(
                    stdout="diff --git a/data/skills/x.yaml b/data/skills/x.yaml\n"
                )
            if "--name-only" in args:
                return _proc(stdout="data/skills/x.yaml\n")
            if args[:2] == ("reset", "--hard"):
                return _proc()
            return _proc()

        return fake_git

    def test_backup_written_before_reset(self, monkeypatch, tmp_path):
        calls: list[tuple] = []
        monkeypatch.setattr(update, "git", self._fake_git(calls))
        monkeypatch.setattr(update, "ROOT", tmp_path)
        monkeypatch.setattr(update, "LOG_FILE", tmp_path / "update.log")
        # 被备份的源文件要真实存在（copy2 需要）
        src = tmp_path / "data" / "skills" / "x.yaml"
        src.parent.mkdir(parents=True)
        src.write_text("name: x\n", encoding="utf-8")

        dest = update._backup_runtime_changes("a" * 40)

        assert dest, "有改动就必须落备份"
        base = tmp_path / "data" / "backups" / "update-rollback"
        backups = list(base.iterdir())
        assert len(backups) == 1 and backups[0].name.endswith("a" * 12)
        assert (
            (backups[0] / "runtime.patch")
            .read_text(encoding="utf-8")
            .startswith("diff --git")
        )
        assert (backups[0] / "files" / "data" / "skills" / "x.yaml").read_text(
            encoding="utf-8"
        ) == "name: x\n"
        diff_calls = [c for c in calls if "--binary" in c]
        assert diff_calls and "HEAD" in diff_calls[0], (
            "线3-L1：diff 基点必须是 HEAD——裸 git diff 只看 worktree-vs-index，"
            "staged 的运行态改动 reset 会连 index 一起丢且不在备份内"
        )
        patch_mode = os.stat(backups[0] / "runtime.patch").st_mode & 0o777
        assert patch_mode == 0o600, (
            f"线3-L2：runtime.patch 应 0600 落盘（含人格模板/技能 YAML 全文），"
            f"实为 {oct(patch_mode)}"
        )

    def test_backup_survives_non_utf8_diff_output(self, monkeypatch, tmp_path):
        """M4（REVIEW-6867995..e079b8d）：diff 输出含非 UTF-8 字节不得让备份静默消失。

        旧实现 text=True 严格解码 → UnicodeDecodeError 被 _run 兜成 127 →
        「diff 失败」分支静默 return，备份既无文件也无痕、reset 照旧丢改动。
        """
        seen_errors: list[str | None] = []

        def fake_git(*args, **kwargs):
            seen_errors.append(kwargs.get("decode_errors"))
            if "--binary" in args:
                return _proc(stdout="diff --git a/data/x b/data/x\n\xff\xfe\n")
            if "--name-only" in args:
                return _proc(stdout="data/x\n")
            return _proc()

        monkeypatch.setattr(update, "git", fake_git)
        monkeypatch.setattr(update, "ROOT", tmp_path)
        monkeypatch.setattr(update, "LOG_FILE", tmp_path / "update.log")
        src = tmp_path / "data" / "x"
        src.parent.mkdir(parents=True)
        src.write_bytes(b"v\xff\n")

        dest = update._backup_runtime_changes("d" * 40)

        assert dest, "非 UTF-8 内容也必须落备份"
        assert seen_errors and all(e == "replace" for e in seen_errors), (
            "diff/name-only 两路都必须带 decode_errors=replace"
        )
        assert (Path(dest) / "files" / "data" / "x").read_bytes() == b"v\xff\n", (
            "files/ 副本是字节级 copy2，真身不丢"
        )

    def test_run_decode_errors_replace_survives_bad_bytes(self):
        """M4 根因钉子：严格解码遇非 UTF-8 stdout 会 UnicodeDecodeError（→127）。"""
        proc = update._run(
            ["python3", "-c", "import sys; sys.stdout.buffer.write(b'a\\xffb')"],
            decode_errors="replace",
        )
        assert proc.returncode == 0
        assert "\ufffd" in proc.stdout, "坏字节替换为 U+FFFD 而不是抛解码异常"

    def test_no_changes_no_backup(self, monkeypatch, tmp_path):
        calls: list[tuple] = []
        monkeypatch.setattr(update, "ROOT", tmp_path)
        monkeypatch.setattr(update, "LOG_FILE", tmp_path / "update.log")
        monkeypatch.setattr(
            update, "git", lambda *a, **k: (calls.append(a), _proc())[1]
        )
        assert update._backup_runtime_changes("b" * 40) == ""
        assert not (tmp_path / "data" / "backups").exists()

    def test_rollback_calls_backup_before_reset(self, monkeypatch, tmp_path):
        """顺序：先备份、后 reset——反了备份的就是回滚后的样子（什么都没保住）。"""
        calls: list[tuple] = []

        def fake_git(*args, **kwargs):
            calls.append(args)
            if "--binary" in args:
                return _proc(stdout="diff --git a/data/x b/data/x\n")
            if "--name-only" in args:
                return _proc(stdout="data/x\n")
            return _proc()

        monkeypatch.setattr(update, "git", fake_git)
        monkeypatch.setattr(update, "ROOT", tmp_path)
        monkeypatch.setattr(update, "LOG_FILE", tmp_path / "update.log")
        monkeypatch.setattr(update, "_sync_and_smoke", lambda: (True, ""))
        monkeypatch.setattr(update, "_restart_and_wait", lambda sha: (True, ""))
        (tmp_path / "data").mkdir(parents=True, exist_ok=True)
        (tmp_path / "data" / "x").write_text("v\n", encoding="utf-8")

        ok, _ = update._rollback("c" * 40)

        assert ok is True
        kinds = [
            "diff"
            if "--binary" in c
            else ("reset" if c[:2] == ("reset", "--hard") else "other")
            for c in calls
        ]
        assert kinds.index("diff") < kinds.index("reset"), "备份必须发生在 reset 之前"
