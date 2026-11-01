"""cron 定时任务（jobs.json / agent_turn）——出沙箱落点、state 拆 sidecar、
投递白名单（M1）、会话隔离（M2）、崩溃窗口不重投（L3）。

来源：AC-group-guest-mode.md D2/D4（P2 批，骨架移植自 UGREEN a7e221b 并改造）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentcore.scheduler import jobs as jobs_mod
from agentcore.scheduler.agent_turn import AgentTurnRunner, _target_allowed


def _job_raw(**over) -> dict:
    raw = {
        "id": "j1",
        "name": "测试任务",
        "enabled": True,
        "schedule": {"kind": "cron", "expr": "0 */3 * * *", "tz": "Asia/Shanghai"},
        "payload": {
            "kind": "agent_turn",
            "message": "跑一轮报告",
            "channel": "onebot",
            "to": "group:123456789",
        },
    }
    raw.update(over)
    return raw


class TestParse:
    def test_valid_job(self, tmp_path):
        jobs, errors = jobs_mod.parse_jobs({"jobs": [_job_raw()]})
        assert not errors and len(jobs) == 1
        j = jobs[0]
        assert j.id == "j1" and j.to == "group:123456789"

    def test_bad_entry_skipped_not_fatal(self, tmp_path):
        raw = _job_raw()
        raw["schedule"] = {"kind": "cron", "expr": "不是cron"}
        jobs, errors = jobs_mod.parse_jobs({"jobs": [raw, _job_raw(id="j2")]})
        assert len(jobs) == 1 and jobs[0].id == "j2"
        assert len(errors) == 1

    def test_openclaw_state_field_used_as_initial(self):
        """互拷进来的文件带 state 字段：作运行态初值（next=0 即首个 tick 立即跑）。"""
        raw = _job_raw(
            state={"lastRunAtMs": 111, "lastStatus": "ok", "nextRunAtMs": 222}
        )
        (jobs, errors) = jobs_mod.parse_jobs({"jobs": [raw]})
        assert not errors
        assert jobs[0].last_run_at_ms == 111 and jobs[0].next_run_at_ms == 222


class TestSandboxLocation:
    def test_default_dir_out_of_workspace(self, monkeypatch, tmp_path):
        """D2 出沙箱：默认目录绝不能在 data/workspace 下（H1 的落点铁律）。"""
        monkeypatch.delenv("AGENT_CRON_DIR", raising=False)
        assert "workspace" not in str(jobs_mod.cron_dir())
        assert str(jobs_mod.cron_dir()) == "data/cron"

    def test_workspace_purity_repo_guard(self):
        """仓库守卫（B1 的静态面）：生产代码不得引用 data/workspace/cron 路径。"""
        import subprocess

        r = subprocess.run(
            [
                "git",
                "grep",
                "-n",
                r"data/workspace/cron",
                "--",
                "agentcore/",
                "plugins/",
            ],
            capture_output=True,
            text=True,
        )
        assert r.stdout.strip() == "", f"沙箱内 cron 路径回迁：\n{r.stdout}"


class TestStateSidecar:
    def test_save_writes_pure_config_no_state_key(self, tmp_path):
        p = tmp_path / "jobs.json"
        jobs, _ = jobs_mod.parse_jobs({"jobs": [_job_raw()]})
        jobs[0].last_status = "ok"
        jobs_mod.save(jobs, p)
        data = json.loads(p.read_text(encoding="utf-8"))
        assert "state" not in data["jobs"][0], "jobs.json 必须是纯配置（M4）"

    def test_update_state_writes_sidecar_not_jobs(self, tmp_path):
        p = tmp_path / "jobs.json"
        jobs, _ = jobs_mod.parse_jobs({"jobs": [_job_raw()]})
        jobs_mod.save(jobs, p)
        before = p.read_text(encoding="utf-8")
        jobs_mod.update_state("j1", status="ok", next_run_ms=999, path=p)
        assert p.read_text(encoding="utf-8") == before, "状态回写不得触碰 jobs.json"
        sidecar = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
        assert sidecar["j1"]["lastStatus"] == "ok"
        assert sidecar["j1"]["nextRunAtMs"] == 999

    def test_load_merges_sidecar(self, tmp_path):
        p = tmp_path / "jobs.json"
        jobs, _ = jobs_mod.parse_jobs({"jobs": [_job_raw()]})
        jobs_mod.save(jobs, p)
        jobs_mod.update_state("j1", status="ok", next_run_ms=12345, path=p)
        loaded, _ = jobs_mod.load(p)
        assert loaded[0].last_status == "ok" and loaded[0].next_run_at_ms == 12345

    def test_save_backup_rolls(self, tmp_path):
        p = tmp_path / "jobs.json"
        jobs, _ = jobs_mod.parse_jobs({"jobs": [_job_raw()]})
        for _ in range(12):
            jobs_mod.save(jobs, p)
        backups = list((tmp_path / "backups").glob("jobs.json.*.bak"))
        assert len(backups) <= jobs_mod._BACKUP_KEEP


class _FakeSink:
    def __init__(self):
        self.sent: list[str] = []

    async def send_once(self, target, message):
        self.sent.append(target)
        return "sent"


class _FakeEngine:
    def __init__(self, text="报告正文", exc: Exception | None = None):
        self._text = text
        self._exc = exc
        self.contexts: list[dict] = []

    async def run(self, context, message):
        self.contexts.append(context)
        if self._exc:
            raise self._exc
        return self._text


class TestAgentTurn:
    def _write_job(self, tmp_path, **over):
        p = tmp_path / "jobs.json"
        jobs, _ = jobs_mod.parse_jobs({"jobs": [_job_raw(**over)]})
        jobs_mod.save(jobs, p)
        return p

    @pytest.mark.asyncio
    async def test_m1_group_target_out_of_allowlist_zero_delivery(self, tmp_path):
        """C1：白名单外群目标 → failed + WARNING + 零投递；next 已推进。"""

        p = self._write_job(tmp_path)
        sink = _FakeSink()
        engine = _FakeEngine()
        runner = AgentTurnRunner(
            engine, sink, group_allowed=lambda g: False, jobs_path=p
        )
        jobs, _ = jobs_mod.load(p)
        status = await runner.run_job(jobs[0])
        assert status == "failed"
        assert sink.sent == [], "白名单外必须零投递"
        assert engine.contexts == [], "白名单外不得执行 agent 轮"
        sidecar = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
        assert sidecar["j1"]["nextRunAtMs"] > 0, "失败的 job 也要推进到下个周期"

    @pytest.mark.asyncio
    async def test_m1_no_allowlist_fn_fail_closed(self, tmp_path):
        """未注入白名单判定：群目标 fail-closed（宁可少投，不可绕过）。"""
        p = self._write_job(tmp_path)
        sink = _FakeSink()
        runner = AgentTurnRunner(_FakeEngine(), sink, jobs_path=p)
        jobs, _ = jobs_mod.load(p)
        assert await runner.run_job(jobs[0]) == "failed"
        assert sink.sent == []

    @pytest.mark.asyncio
    async def test_m1_private_target_unrestricted(self, tmp_path):
        """私聊目标不设限（superuser 本人配置的通道）。"""
        p = self._write_job(
            tmp_path,
            payload={
                "kind": "agent_turn",
                "message": "私聊报告",
                "channel": "onebot",
                "to": "private:123456789",
            },
        )
        sink = _FakeSink()
        runner = AgentTurnRunner(
            _FakeEngine(), sink, group_allowed=lambda g: False, jobs_path=p
        )
        jobs, _ = jobs_mod.load(p)
        assert await runner.run_job(jobs[0]) == "ok"
        assert sink.sent == ["private:123456789"]

    @pytest.mark.asyncio
    async def test_m2_session_and_route_override_passed_to_engine(self, tmp_path):
        """C2：engine 收到 cron 命名空间的会话键与路由键（隔离由 engine 落实）。"""
        p = self._write_job(tmp_path)
        engine = _FakeEngine()
        runner = AgentTurnRunner(
            engine, _FakeSink(), group_allowed=lambda g: True, jobs_path=p
        )
        jobs, _ = jobs_mod.load(p)
        await runner.run_job(jobs[0])
        ctx = engine.contexts[0]
        assert ctx["session_override"] == "cron:j1"
        assert ctx["route_override"] == "cron:j1"

    @pytest.mark.asyncio
    async def test_l3_crash_mid_run_does_not_rerun(self, tmp_path):
        """L3：执行前先把 next 推进到下个周期——执行途中崩溃（结尾回写不可达）
        时 sidecar 仍有「running + 新 next」，下个 tick 不会重跑。

        崩溃窗口的构造：send_once 抛异常（run_job 对它没有 try/except——结尾的
        update_state 不可达），能断言的只有执行前那次写入。"""
        import pytest

        class _BoomSink:
            async def send_once(self, target, message):
                raise RuntimeError("进程在投递时被杀")

        p = self._write_job(tmp_path)
        runner = AgentTurnRunner(
            _FakeEngine(text="正文"),
            _BoomSink(),
            group_allowed=lambda g: True,
            jobs_path=p,
        )
        jobs, _ = jobs_mod.load(p)
        with pytest.raises(RuntimeError):
            await runner.run_job(jobs[0])
        sidecar = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
        assert sidecar["j1"]["lastStatus"] == "running"
        assert sidecar["j1"]["nextRunAtMs"] > 0, (
            "执行前必须先推进 next（崩溃窗口不重投），否则进程死亡后下个 tick 立即重跑"
        )

    @pytest.mark.asyncio
    async def test_tick_respects_future_next_run(self, tmp_path):
        """到点判断：next 在未来 → 不执行。"""
        self._write_job(
            tmp_path,
            state={"nextRunAtMs": 999999999999999},
        )
        sink = _FakeSink()
        engine = _FakeEngine()
        runner = AgentTurnRunner(engine, sink, group_allowed=lambda g: True)
        await runner.tick()
        assert sink.sent == [] and engine.contexts == []


class TestEngineOverride:
    def test_engine_honors_session_override(self):
        """engine.run 对 session_override / route_override 的落实（M2 的另一侧）。
        源码级断言：行为由 AgentEngine 集成测试覆盖，这里钉住两行不被顺手删掉。"""
        src = Path("agentcore/loop/engine.py").read_text(encoding="utf-8")
        # 子串不含引号定位（format 可能拆行）：语义级钉住两个 override 被读取
        assert "session_override" in src and "resolve_session(user_id, group_id)" in src
        assert "route_override" in src


class TestTargetAllowed:
    def test_private_always_allowed_even_without_fn(self):
        assert _target_allowed("private:123456789", None) is True

    def test_group_fail_closed_without_fn(self):
        assert _target_allowed("group:123456789", None) is False

    def test_invalid_target_rejected(self):
        assert _target_allowed("随便写", lambda g: True) is False
