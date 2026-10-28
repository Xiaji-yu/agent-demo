"""jobs.json 定时任务：解析 / 校验 / 落盘 / 状态回写 / agent_turn 执行。

2026-10-28 需求②：OpenClaw 兼容的 ``data/workspace/cron/jobs.json``，只接管
``payload.kind == "agent_turn"``（到点让 agent 跑一轮并投递到指定会话）；
kb_digest / db_backup / 群统计 / 提醒轮询仍是系统任务，继续走 env。

变异锚点（改坏实现必须红）：
1. 去掉 schedule.kind 校验 → 非 cron 被接受，用例红
2. 去掉 payload.to 校验 → 非法目标被接受，用例红
3. 去掉 save 的未知键保留 → 用户备注被抹掉，用例红
4. 去掉 enabled 过滤 → 停用的 job 照样跑，用例红
5. 去掉 _warn_once 去重 → 告警刷屏，用例红
6. 去掉写后重读校验 → 半截文件不回落，用例红
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agentcore.scheduler import jobs as J
from agentcore.scheduler.agent_turn import AgentTurnRunner, _next_run_ms


@pytest.fixture(autouse=True)
def _clean_cron_env(monkeypatch):
    for key in ("AGENT_CRON_DIR", "AGENT_CONFIG_JSON"):
        monkeypatch.delenv(key, raising=False)
    yield
    for key in ("AGENT_CRON_DIR", "AGENT_CONFIG_JSON"):
        os.environ.pop(key, None)


def _job_dict(**over):
    base = {
        "id": "j1",
        "name": "测试任务",
        "enabled": True,
        "schedule": {"kind": "cron", "expr": "0 */3 * * *", "tz": "Asia/Shanghai"},
        "payload": {
            "kind": "agent_turn",
            "message": "执行脚本并发到群里",
            "channel": "onebot",
            "to": "group:123",
        },
    }
    base.update(over)
    return base


class TestPaths:
    def test_default_dir(self):
        assert J.cron_dir() == Path("data/workspace/cron")
        assert J.jobs_path() == Path("data/workspace/cron/jobs.json")

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("AGENT_CRON_DIR", "/tmp/mycron")
        assert J.cron_dir() == Path("/tmp/mycron")

    def test_blank_env_falls_back(self, monkeypatch):
        """M18 同型：env 空串/纯空白必须回落默认，不能变成 Path('') = '.'。"""
        for raw in ("", "   "):
            monkeypatch.setenv("AGENT_CRON_DIR", raw)
            assert J.cron_dir() == Path("data/workspace/cron")


class TestParseJobs:
    def test_valid_job(self):
        jobs, errors = J.parse_jobs({"version": 1, "jobs": [_job_dict()]})
        assert errors == []
        assert len(jobs) == 1
        j = jobs[0]
        assert j.id == "j1" and j.name == "测试任务"
        assert j.cron == "0 */3 * * *" and j.tz == "Asia/Shanghai"
        assert j.channel == "onebot" and j.to == "group:123"
        assert j.enabled is True and j.delete_after_run is False

    def test_no_jobs_key_is_empty_not_error(self):
        jobs, errors = J.parse_jobs({"version": 1})
        assert jobs == [] and errors == []

    def test_id_derived_from_name(self):
        """中文任务名也要得到可读 id（旧 ASCII slug 会把它削成 "3"）。"""
        jobs, _ = J.parse_jobs({"jobs": [_job_dict(id="", name="每3小时用电用水报告")]})
        assert jobs[0].id == "每3小时用电用水报告"

    def test_id_derived_ascii_still_slugged(self):
        jobs, _ = J.parse_jobs({"jobs": [_job_dict(id="", name="My Daily Report!")]})
        assert jobs[0].id == "My-Daily-Report"

    def test_id_derived_fallback_when_no_word_chars(self):
        jobs, _ = J.parse_jobs({"jobs": [_job_dict(id="", name="！！！")]})
        assert jobs[0].id == "job"

    def test_rejects_non_cron_kind(self):
        jobs, errors = J.parse_jobs(
            {"jobs": [_job_dict(schedule={"kind": "interval", "expr": "60"})]}
        )
        assert jobs == [] and "kind" in errors[0]

    def test_rejects_bad_cron_expr(self):
        jobs, errors = J.parse_jobs(
            {"jobs": [_job_dict(schedule={"kind": "cron", "expr": "not a cron"})]}
        )
        assert jobs == [] and "cron" in errors[0]

    def test_rejects_empty_cron(self):
        jobs, errors = J.parse_jobs(
            {"jobs": [_job_dict(schedule={"kind": "cron", "expr": "  "})]}
        )
        assert jobs == [] and errors

    def test_rejects_non_agent_turn_payload(self):
        jobs, errors = J.parse_jobs(
            {
                "jobs": [
                    _job_dict(
                        payload={"kind": "shell", "message": "x", "to": "group:1"}
                    )
                ]
            }
        )
        assert jobs == [] and "payload.kind" in errors[0]

    def test_rejects_bad_target(self):
        for bad in ("123", "chan:123", "group:１２３", "group:", ""):
            jobs, errors = J.parse_jobs(
                {
                    "jobs": [
                        _job_dict(
                            payload={"kind": "agent_turn", "message": "x", "to": bad}
                        )
                    ]
                }
            )
            assert jobs == [], bad
            assert errors, bad

    def test_accepts_private_target(self):
        jobs, errors = J.parse_jobs(
            {
                "jobs": [
                    _job_dict(
                        payload={
                            "kind": "agent_turn",
                            "message": "x",
                            "to": "private:456",
                        }
                    )
                ]
            }
        )
        assert errors == [] and jobs[0].to == "private:456"

    def test_rejects_empty_message(self):
        jobs, errors = J.parse_jobs(
            {
                "jobs": [
                    _job_dict(
                        payload={"kind": "agent_turn", "message": "  ", "to": "group:1"}
                    )
                ]
            }
        )
        assert jobs == [] and errors

    def test_rejects_oversized_message(self):
        jobs, errors = J.parse_jobs(
            {
                "jobs": [
                    _job_dict(
                        payload={
                            "kind": "agent_turn",
                            "message": "x" * 20001,
                            "to": "group:1",
                        }
                    )
                ]
            }
        )
        assert jobs == [] and errors

    def test_rejects_bad_channel(self):
        jobs, errors = J.parse_jobs(
            {
                "jobs": [
                    _job_dict(
                        payload={
                            "kind": "agent_turn",
                            "message": "x",
                            "channel": "feishu",
                            "to": "group:1",
                        }
                    )
                ]
            }
        )
        assert jobs == [] and "channel" in errors[0]

    def test_rejects_duplicate_id(self):
        jobs, errors = J.parse_jobs({"jobs": [_job_dict(), _job_dict()]})
        assert len(jobs) == 1 and "重复" in errors[0]

    def test_bad_entry_does_not_kill_good_ones(self):
        """一个坏 job 不该把其余的也废掉（与 push 同口径）。"""
        jobs, errors = J.parse_jobs(
            {
                "jobs": [
                    _job_dict(id="good"),
                    _job_dict(id="bad", schedule={"kind": "nope"}),
                ]
            }
        )
        assert [j.id for j in jobs] == ["good"]
        assert len(errors) == 1

    def test_top_level_not_object_raises(self):
        with pytest.raises(J.JobsError):
            J.parse_jobs([1, 2])

    def test_jobs_not_list_raises(self):
        with pytest.raises(J.JobsError, match="jobs"):
            J.parse_jobs({"jobs": "nope"})

    def test_enabled_string_forms(self):
        for raw, want in (("false", False), ("0", False), ("true", True), ("1", True)):
            jobs, _ = J.parse_jobs({"jobs": [_job_dict(enabled=raw)]})
            assert jobs[0].enabled is want, raw

    def test_default_enabled_true(self):
        d = _job_dict()
        del d["enabled"]
        jobs, _ = J.parse_jobs({"jobs": [d]})
        assert jobs[0].enabled is True

    def test_unknown_fields_ignored(self):
        """OpenClaw 增量字段（sessionTarget/announce 等）忽略不报错。"""
        d = _job_dict()
        d["sessionTarget"] = "isolated"
        d["announce"] = True
        jobs, errors = J.parse_jobs({"jobs": [d]})
        assert errors == [] and len(jobs) == 1


class TestLoad:
    def test_missing_file_is_empty(self, tmp_path):
        jobs, errors = J.load(tmp_path / "nope.json")
        assert jobs == [] and errors == []

    def test_bad_json_raises(self, tmp_path):
        p = tmp_path / "jobs.json"
        p.write_text("{not json", encoding="utf-8")
        with pytest.raises(J.JobsError, match="JSON"):
            J.load(p)

    def test_real_file_roundtrip(self, tmp_path):
        p = tmp_path / "jobs.json"
        J.save([_cron_job()], p)
        jobs, errors = J.load(p)
        assert errors == []
        assert [j.id for j in jobs] == ["j1"]


def _cron_job(**over):
    base = dict(
        id="j1",
        name="测试任务",
        enabled=True,
        cron="0 */3 * * *",
        tz="Asia/Shanghai",
        message="执行脚本并发到群里",
        channel="onebot",
        to="group:123",
    )
    base.update(over)
    return J.CronJob(**base)


class TestSave:
    def test_roundtrip(self, tmp_path):
        p = tmp_path / "jobs.json"
        J.save([_cron_job()], p)
        jobs, _ = J.load(p)
        j = jobs[0]
        assert j.id == "j1" and j.cron == "0 */3 * * *"
        assert j.tz == "Asia/Shanghai" and j.to == "group:123"
        assert j.message == "执行脚本并发到群里"

    def test_preserves_unknown_top_level_keys(self, tmp_path):
        """只补丁 jobs 键：用户备注与 OpenClaw 增量字段不能被抹掉。"""
        p = tmp_path / "jobs.json"
        p.write_text(
            json.dumps(
                {"_说明": "我的手写备注", "version": 1, "sessionTarget": "isolated"},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        J.save([_cron_job()], p)
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["_说明"] == "我的手写备注"
        assert data["sessionTarget"] == "isolated"

    def test_creates_dir(self, tmp_path):
        p = tmp_path / "deep" / "deeper" / "jobs.json"
        J.save([_cron_job()], p)
        assert p.is_file()

    def test_0600_when_posix(self, tmp_path):
        p = tmp_path / "jobs.json"
        J.save([_cron_job()], p)
        if os.name == "posix":
            assert p.stat().st_mode & 0o777 == 0o600

    def test_backup_written(self, tmp_path):
        p = tmp_path / "jobs.json"
        J.save([_cron_job()], p)
        J.save([_cron_job(cron="0 4 * * *")], p)
        backups = list((tmp_path / "backups").glob("jobs.json.*.bak"))
        assert backups
        if os.name == "posix":
            assert backups[0].stat().st_mode & 0o777 == 0o600

    def test_symlink_fail_closed(self, tmp_path, symlinks_supported):
        target = tmp_path / "real.json"
        J.save([_cron_job()], target)
        link = tmp_path / "link.json"
        link.symlink_to(target)
        with pytest.raises(J.JobsError, match="symlink"):
            J.save([_cron_job()], link)

    def test_verify_failure_rolls_back(self, tmp_path, monkeypatch):
        p = tmp_path / "jobs.json"
        J.save([_cron_job()], p)
        original = p.read_text(encoding="utf-8")
        real_load = J.load

        def broken(path=None):
            jobs, _ = real_load(path)
            jobs[0].id = "other"
            return jobs, []

        monkeypatch.setattr(J, "load", broken)
        with pytest.raises(J.JobsError, match="rolled_back"):
            J.save([_cron_job(cron="0 5 * * *")], p)
        assert p.read_text(encoding="utf-8") == original

    def test_dump_is_openclaw_shape(self, tmp_path):
        """落盘字段名必须与 OpenClaw 一致（camelCase + schedule/payload 嵌套）。"""
        p = tmp_path / "jobs.json"
        J.save([_cron_job()], p)
        raw = json.loads(p.read_text(encoding="utf-8"))["jobs"][0]
        assert raw["schedule"] == {
            "kind": "cron",
            "expr": "0 */3 * * *",
            "tz": "Asia/Shanghai",
        }
        assert raw["payload"]["kind"] == "agent_turn"
        assert raw["payload"]["to"] == "group:123"
        assert "deleteAfterRun" in raw and "createdAtMs" in raw
        assert raw["state"]["lastStatus"] == ""

    def test_tz_omitted_when_none(self, tmp_path):
        p = tmp_path / "jobs.json"
        J.save([_cron_job(tz=None)], p)
        raw = json.loads(p.read_text(encoding="utf-8"))["jobs"][0]
        assert "tz" not in raw["schedule"]


class TestUpdateState:
    def test_writes_status(self, tmp_path):
        p = tmp_path / "jobs.json"
        J.save([_cron_job()], p)
        J.update_state("j1", status="ok", next_run_ms=1793192400000, path=p)
        jobs, _ = J.load(p)
        assert jobs[0].last_status == "ok"
        assert jobs[0].last_run_at_ms > 0
        assert jobs[0].next_run_at_ms == 1793192400000

    def test_delete_after_run_removes_on_ok(self, tmp_path):
        p = tmp_path / "jobs.json"
        J.save([_cron_job(delete_after_run=True)], p)
        J.update_state("j1", status="ok", path=p)
        jobs, _ = J.load(p)
        assert jobs == []

    def test_delete_after_run_keeps_on_failure(self, tmp_path):
        p = tmp_path / "jobs.json"
        J.save([_cron_job(delete_after_run=True)], p)
        J.update_state("j1", status="failed", path=p)
        jobs, _ = J.load(p)
        assert [j.id for j in jobs] == ["j1"]

    def test_unknown_id_is_noop(self, tmp_path):
        p = tmp_path / "jobs.json"
        J.save([_cron_job()], p)
        before = p.read_text(encoding="utf-8")
        J.update_state("nope", status="ok", path=p)
        assert p.read_text(encoding="utf-8") == before

    def test_without_path_targets_default_location(self, tmp_path, monkeypatch):
        """不传 path 时走 AGENT_CRON_DIR：绝不能默认写到仓库那份真实文件。"""
        monkeypatch.setenv("AGENT_CRON_DIR", str(tmp_path))
        J.save([_cron_job()])
        J.update_state("j1", status="ok")
        jobs, _ = J.load()
        assert jobs[0].last_status == "ok"


class TestNextRun:
    def test_valid(self):
        assert _next_run_ms("0 */3 * * *", None) > 0

    def test_with_tz(self):
        assert _next_run_ms("0 9 * * 1", "Asia/Shanghai") > 0

    def test_bad_expr_returns_zero(self):
        assert _next_run_ms("not a cron", None) == 0


class _FakeEngine:
    def __init__(self, text="报告内容"):
        self.text = text
        self.calls = []

    async def run(self, context, message, *a, **kw):
        self.calls.append((context, message))
        return self.text


class _FakeSink:
    def __init__(self, outcome="sent"):
        self.outcome = outcome
        self.sent = []

    async def send_once(self, target, message):
        self.sent.append((target, message))
        return self.outcome


class TestAgentTurnRunner:
    @pytest.mark.asyncio
    async def test_runs_and_delivers(self, tmp_path):
        p = tmp_path / "jobs.json"
        J.save([_cron_job()], p)
        eng, sink = _FakeEngine(), _FakeSink()
        runner = AgentTurnRunner(eng, sink)
        status = await runner.run_job(J.load(p)[0][0])
        assert status == "ok"
        assert eng.calls[0][1] == "执行脚本并发到群里"
        assert eng.calls[0][0]["group_id"] == "123"
        assert sink.sent == [("group:123", "报告内容")]

    @pytest.mark.asyncio
    async def test_empty_output_not_delivered(self, tmp_path):
        p = tmp_path / "jobs.json"
        J.save([_cron_job()], p)
        eng, sink = _FakeEngine(text="   "), _FakeSink()
        runner = AgentTurnRunner(eng, sink)
        assert await runner.run_job(J.load(p)[0][0]) == "failed"
        assert sink.sent == []

    @pytest.mark.asyncio
    async def test_uncertain_is_not_failure(self, tmp_path):
        """结果不确定绝不重发：记 uncertain 而非 failed（出站铁律）。"""
        p = tmp_path / "jobs.json"
        J.save([_cron_job()], p)
        runner = AgentTurnRunner(_FakeEngine(), _FakeSink(outcome="uncertain"))
        assert await runner.run_job(J.load(p)[0][0]) == "uncertain"

    @pytest.mark.asyncio
    async def test_engine_exception_is_caught(self, tmp_path):
        p = tmp_path / "jobs.json"
        J.save([_cron_job()], p)

        class _Boom:
            async def run(self, *a, **kw):
                raise RuntimeError("boom")

        runner = AgentTurnRunner(_Boom(), _FakeSink())
        assert await runner.run_job(J.load(p)[0][0]) == "failed"

    @pytest.mark.asyncio
    async def test_tick_skips_disabled(self, tmp_path, monkeypatch):
        p = tmp_path / "jobs.json"
        J.save([_cron_job(enabled=False)], p)
        monkeypatch.setenv("AGENT_CRON_DIR", str(tmp_path))
        eng, sink = _FakeEngine(), _FakeSink()
        await AgentTurnRunner(eng, sink).tick()
        assert eng.calls == [] and sink.sent == []

    @pytest.mark.asyncio
    async def test_tick_missing_file_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENT_CRON_DIR", str(tmp_path / "nope"))
        eng, sink = _FakeEngine(), _FakeSink()
        await AgentTurnRunner(eng, sink).tick()
        assert eng.calls == []

    @pytest.mark.asyncio
    async def test_tick_respects_next_run(self, tmp_path, monkeypatch):
        """nextRunAtMs 在未来 → 不到点不跑。"""
        p = tmp_path / "jobs.json"
        job = _cron_job(next_run_at_ms=9999999999999)
        J.save([job], p)
        monkeypatch.setenv("AGENT_CRON_DIR", str(tmp_path))
        eng, sink = _FakeEngine(), _FakeSink()
        await AgentTurnRunner(eng, sink).tick()
        assert eng.calls == []

    @pytest.mark.asyncio
    async def test_tick_bad_json_warns_once(self, tmp_path, monkeypatch, caplog):
        import logging

        p = tmp_path / "jobs.json"
        p.write_text("{broken", encoding="utf-8")
        monkeypatch.setenv("AGENT_CRON_DIR", str(tmp_path))
        runner = AgentTurnRunner(_FakeEngine(), _FakeSink())
        with caplog.at_level(logging.WARNING, logger="agentcore.scheduler.agent_turn"):
            await runner.tick()
            await runner.tick()
            await runner.tick()
        assert caplog.text.count("读取失败") == 1, caplog.text

    def test_warn_once_dedups(self):
        runner = AgentTurnRunner(None, None)
        runner._warn_once("same")
        runner._warn_once("same")
        assert runner._last_error == "same"

    def test_register_requires_scheduler(self):
        assert AgentTurnRunner(None, None).register() is False


class TestRepoJobsFile:
    """看守仓库自带的 jobs.json：格式必须合法，且默认停用（不误触发）。"""

    def test_repo_jobs_file_is_valid(self):
        p = (
            Path(__file__).resolve().parents[1]
            / "data"
            / "workspace"
            / "cron"
            / "jobs.json"
        )
        assert p.is_file(), "仓库应自带一份 jobs.json 样例"
        jobs, errors = J.load(p)
        assert errors == [], errors
        assert jobs, "样例至少要有一条"
        for j in jobs:
            assert j.enabled is False, "样例默认停用，避免一上线就误触发"
            assert j.channel == "onebot"
            assert J.parse_target(j.to) is not None

    def test_repo_jobs_file_is_tracked(self):
        """必须入库：否则换机器/重建镜像后定时任务全丢。"""
        import subprocess

        r = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "data/workspace/cron/jobs.json"],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        assert r.returncode == 0, "data/workspace/cron/jobs.json 必须被 git 跟踪"
