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


def _write_jobs(tmp_path, **over) -> Path:
    """落一个合法 jobs.json 并返回路径（模块级，供多个 Test 类共用）。"""
    p = tmp_path / "jobs.json"
    jobs, _ = jobs_mod.parse_jobs({"jobs": [_job_raw(**over)]})
    jobs_mod.save(jobs, p)
    return p


class TestParse:
    def test_valid_job(self, tmp_path):
        jobs, errors = jobs_mod.parse_jobs({"jobs": [_job_raw()]})
        assert not errors and len(jobs) == 1
        j = jobs[0]
        assert j.id == "j1" and j.to == "group:123456789"

    def test_rejects_never_fire_cron_expr(self):
        """能解析但永触发（2 月 30 日）必须拒：``_next_run_ms`` 对这类表达式返
        ``_NEVER_FIRE``，而 tick 的「首次运行」语义把 0 当没跑过 → 每 60s 跑一次
        完整 LLM 回合 + 投递，无限烧 token 且永不标记 failed。"""
        jobs, errors = jobs_mod.parse_jobs(
            {"jobs": [_job_raw(schedule={"kind": "cron", "expr": "0 0 30 2 *"})]}
        )
        assert jobs == [] and "永触发" in errors[0]

    def test_accepts_leap_day_cron_expr(self):
        """闰日（2 月 29）每 4 年真会触发，不是永触发，不能误拒。"""
        jobs, errors = jobs_mod.parse_jobs(
            {"jobs": [_job_raw(schedule={"kind": "cron", "expr": "0 0 29 2 *"})]}
        )
        assert len(jobs) == 1 and errors == []

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

    def test_chinese_name_without_id_gets_unicode_slug(self):
        """M5（REVIEW-63f9444..858afcc）：纯中文 name 无显式 id 必须可解析。

        旧 _ID_RE 只认 ASCII，job 会被自己派生的中文 id 拒之门外——docstring
        自举的例子「每3小时用电用水报告」就是反例（已复现 REJECTED）。
        """
        jobs, errors = jobs_mod.parse_jobs(
            {"jobs": [_job_raw(id="", name="每3小时用电用水报告")]}
        )
        assert not errors, errors
        assert jobs[0].id == "每3小时用电用水报告"
        assert jobs_mod._ID_RE.match(jobs[0].id)
        # 空格/标点仍然拒绝（id 会进会话键与日志，不能放任任意字符）
        _, errors2 = jobs_mod.parse_jobs({"jobs": [_job_raw(id="坏 id!")]})
        assert len(errors2) == 1 and "id 非法" in errors2[0]


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
    def __init__(self, outcome: str = "sent"):
        self.sent: list[str] = []
        self.outcome = outcome

    async def send_once(self, target, message, *, ref: str = ""):
        self.sent.append(target)
        return self.outcome


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
    async def test_m2_identity_pseudo_user_and_unattended_flag(self, tmp_path):
        """M2/M3（REVIEW-63f9444..858afcc）：轮次身份是伪用户 cron:<job_id>。

        旧实现私聊目标把真实 QQ 塞进 user_id——where 轴视作私聊全放行（超管
        目标的无人值守轮次可见全部 superuser 技能），growth 也记到真实用户名下
        （其本人私聊即召回）。收窄后权限轴按非超管收口，unattended 供 engine
        跳过 growth；目标身份只用于投递（Sink 正文 + 技能级 delivery_*，
        见 test_delivery_target_in_context_for_delivery_skills）。
        """
        p = self._write_job(tmp_path)
        engine = _FakeEngine()
        runner = AgentTurnRunner(
            engine, _FakeSink(), group_allowed=lambda g: True, jobs_path=p
        )
        jobs, _ = jobs_mod.load(p)
        await runner.run_job(jobs[0])
        ctx = engine.contexts[0]
        assert ctx["user_id"] == "cron:j1", "群目标也以伪用户执行"
        assert ctx["unattended"] is True
        assert ctx["group_id"] == "123456789"

        p2 = self._write_job(
            tmp_path,
            id="j2",
            payload={
                "kind": "agent_turn",
                "message": "私聊报告",
                "channel": "onebot",
                "to": "private:2224513919",
            },
        )
        engine2 = _FakeEngine()
        runner2 = AgentTurnRunner(engine2, _FakeSink(), jobs_path=p2)
        jobs2, _ = jobs_mod.load(p2)
        await runner2.run_job(jobs2[0])
        ctx2 = engine2.contexts[0]
        assert ctx2["user_id"] == "cron:j2", "私聊目标不得以真实 QQ 作轮次身份"
        assert ctx2["group_id"] == "" and ctx2["unattended"] is True

    @pytest.mark.asyncio
    async def test_delivery_target_in_context_for_delivery_skills(self, tmp_path):
        """无人值守轮次的技能级投递目标：job.to 解析进 context（delivery_*）。

        轮次身份是伪用户 cron:<job_id>，发文件/图片类技能直接拿 user_id 当
        投递目标必然失败（非法 QQ 号）——产物去处由 delivery_user_id /
        delivery_group_id 声明。只解耦「投给谁」，权限轴不变（superuser 技能
        依旧不可见，见 test_m2_identity_pseudo_user_and_unattended_flag）。
        """
        p = self._write_job(tmp_path)  # 默认群目标 group:123456789
        engine = _FakeEngine()
        runner = AgentTurnRunner(
            engine, _FakeSink(), group_allowed=lambda g: True, jobs_path=p
        )
        jobs, _ = jobs_mod.load(p)
        await runner.run_job(jobs[0])
        ctx = engine.contexts[0]
        assert ctx["delivery_group_id"] == "123456789"
        assert ctx["delivery_user_id"] == ""

        p2 = self._write_job(
            tmp_path,
            id="j2",
            payload={
                "kind": "agent_turn",
                "message": "私聊报告",
                "channel": "onebot",
                "to": "private:2224513919",
            },
        )
        engine2 = _FakeEngine()
        runner2 = AgentTurnRunner(engine2, _FakeSink(), jobs_path=p2)
        jobs2, _ = jobs_mod.load(p2)
        await runner2.run_job(jobs2[0])
        ctx2 = engine2.contexts[0]
        assert ctx2["delivery_user_id"] == "2224513919"
        assert ctx2["delivery_group_id"] == ""

    @pytest.mark.asyncio
    async def test_trusted_job_carries_perm_user_id_fail_closed(self, tmp_path):
        """AC-cron-trusted：trusted+owner → context 带 perm_user_id=owner
        （权限身份，仅影响技能可见性/放行）；trusted 与 owner 任一缺失 →
        不带（fail-closed，按伪用户收窄）。trusted 轮次记审计。"""
        from agentcore.diagnostics import recent

        def trusted_events() -> list[dict]:
            return [e for e in recent() if e.get("kind") == "cron_trusted_run"]

        baseline = len(trusted_events())

        # ① 非 trusted：无 perm_user_id，无审计
        p = self._write_job(tmp_path, id="plain")
        engine = _FakeEngine()
        runner = AgentTurnRunner(
            engine, _FakeSink(), group_allowed=lambda g: True, jobs_path=p
        )
        jobs, _ = jobs_mod.load(p)
        await runner.run_job(jobs[0])
        assert "perm_user_id" not in engine.contexts[0]
        assert len(trusted_events()) == baseline

        # ② trusted + owner：perm_user_id=owner + 审计一条（其余身份字段不变）
        p2 = self._write_job(tmp_path, id="t1", trusted=True, owner="2224513919")
        engine2 = _FakeEngine()
        runner2 = AgentTurnRunner(
            engine2, _FakeSink(), group_allowed=lambda g: True, jobs_path=p2
        )
        jobs2, _ = jobs_mod.load(p2)
        await runner2.run_job(jobs2[0])
        ctx = engine2.contexts[0]
        assert ctx["perm_user_id"] == "2224513919"
        assert ctx["user_id"] == "cron:t1", "数据身份仍是伪用户"
        assert ctx["session_override"] == "cron:t1", "会话隔离不变"
        assert ctx["unattended"] is True, "growth 跳过不变"
        assert len(trusted_events()) == baseline + 1

        # ③ trusted 但无 owner：fail-closed
        p3 = self._write_job(tmp_path, id="t2", trusted=True)
        engine3 = _FakeEngine()
        runner3 = AgentTurnRunner(
            engine3, _FakeSink(), group_allowed=lambda g: True, jobs_path=p3
        )
        jobs3, _ = jobs_mod.load(p3)
        await runner3.run_job(jobs3[0])
        assert "perm_user_id" not in engine3.contexts[0]
        assert len(trusted_events()) == baseline + 1

        # ④ 非 trusted + 有 owner（/cron add 的默认形态）：owner 不等于放权，
        # 必须显式 trusted——漏检这个等于开关失效
        p4 = self._write_job(tmp_path, id="t3", owner="2224513919")
        engine4 = _FakeEngine()
        runner4 = AgentTurnRunner(
            engine4, _FakeSink(), group_allowed=lambda g: True, jobs_path=p4
        )
        jobs4, _ = jobs_mod.load(p4)
        await runner4.run_job(jobs4[0])
        assert "perm_user_id" not in engine4.contexts[0]
        assert len(trusted_events()) == baseline + 1

    @pytest.mark.asyncio
    async def test_m9_concurrent_run_jobs_are_serialized(self, tmp_path):
        """M9：手动试跑（/cron run）与 tick 并发进入 run_job 必须串行。

        旧实现无锁：试跑 create_task 与 60s tick 的竞争窗口内同一 job 双跑，
        同一天两次投递（nudge 有现成同款锁未复用）。
        """
        import asyncio

        p = self._write_job(tmp_path)
        events: list[str] = []

        class _SlowEngine:
            async def run(self, context, message):
                events.append("in")
                await asyncio.sleep(0.02)
                events.append("out")
                return "x"

        runner = AgentTurnRunner(
            _SlowEngine(), _FakeSink(), group_allowed=lambda g: True, jobs_path=p
        )
        jobs, _ = jobs_mod.load(p)
        await asyncio.gather(runner.run_job(jobs[0]), runner.run_job(jobs[0]))
        assert events == ["in", "out", "in", "out"], (
            "并发 run_job 必须串行，否则同 job 双投递"
        )

    @pytest.mark.asyncio
    async def test_l3_crash_mid_run_does_not_rerun(self, tmp_path):
        """L3：执行前先把 next 推进到下个周期——执行途中崩溃（结尾回写不可达）
        时 sidecar 仍有「running + 新 next」，下个 tick 不会重跑。

        崩溃窗口的构造：send_once 抛异常（run_job 对它没有 try/except——结尾的
        update_state 不可达），能断言的只有执行前那次写入。"""
        import pytest

        class _BoomSink:
            async def send_once(self, target, message, *, ref: str = ""):
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


class TestNotifyBudgetGate:
    """P3 打扰预算在 agent_turn 面的接线。

    预算闸放在引擎**之前**：agent_turn 的产出是 LLM 生成正文，跑一轮要花
    token——拦在引擎前省的是真金白银的调用，也省一次无意义的 tool-loop。
    """

    def _exhaust(self, runner, target: str = "group:123456789") -> None:
        """把当日全局额度占满（模拟该目标今天已经被别类投递刷满）。

        直接往 runner 自己那个共享实例上记账：另起实例读的是各自的内存计数，
        看不到对方的写入（记账本就该只有一个写入口）。
        """
        budget = runner._budget
        for _ in range(max(1, budget.daily_limit)):
            budget.record(target, "push")

    @pytest.mark.asyncio
    async def test_exhausted_budget_skips_engine_run(self, tmp_path, caplog):
        """额度耗尽 → 不跑引擎、不投递，但仍推进下次执行时刻。

        不推进会让 tick 每 60s 重试一次，把日志刷满。
        """
        import logging

        p = _write_jobs(tmp_path)
        sink = _FakeSink()
        engine = _FakeEngine()
        runner = AgentTurnRunner(
            engine, sink, group_allowed=lambda g: True, jobs_path=p
        )
        self._exhaust(runner)
        jobs, _ = jobs_mod.load(p)
        with caplog.at_level(logging.WARNING, logger="agentcore.scheduler.agent_turn"):
            status = await runner.run_job(jobs[0])
        assert status == "failed"
        assert engine.contexts == [], "额度耗尽不得执行 agent 轮（省 token）"
        assert sink.sent == [], "额度耗尽不得投递"
        assert runner._budget.by_kind("group:123456789").get("agent_turn", 0) == 0, (
            "被拦下的投递不记账"
        )
        assert "打扰预算" in caplog.text
        sidecar = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
        assert sidecar["j1"]["lastStatus"] == "failed"
        assert sidecar["j1"]["nextRunAtMs"] > 0, "被拦也要推进下次，否则每 tick 重试"

    @pytest.mark.asyncio
    async def test_sent_records_budget(self, tmp_path):
        """确认送达才记账（uncertain 不记——可能没到用户手上）。"""
        p = _write_jobs(tmp_path)
        runner = AgentTurnRunner(
            _FakeEngine(), _FakeSink(), group_allowed=lambda g: True, jobs_path=p
        )
        jobs, _ = jobs_mod.load(p)
        await runner.run_job(jobs[0])
        assert runner._budget.by_kind("group:123456789") == {"agent_turn": 1}

    @pytest.mark.asyncio
    async def test_uncertain_is_not_recorded(self, tmp_path):
        """结果不确定时不记账：记了会让预算凭空少一格。"""
        p = _write_jobs(tmp_path)
        runner = AgentTurnRunner(
            _FakeEngine(),
            _FakeSink(outcome="uncertain"),
            group_allowed=lambda g: True,
            jobs_path=p,
        )
        jobs, _ = jobs_mod.load(p)
        await runner.run_job(jobs[0])
        assert runner._budget.by_kind("group:123456789") == {}

    @pytest.mark.asyncio
    async def test_budget_warning_deduped_per_job(self, tmp_path, caplog):
        """同一 job 的预算告警只报一次（按 (job.id, target) 去重）。

        与 ACL 告警的区别：预算每天重置，恢复后要能重新报。
        """
        import logging

        p = _write_jobs(tmp_path)
        runner = AgentTurnRunner(
            _FakeEngine(), _FakeSink(), group_allowed=lambda g: True, jobs_path=p
        )
        self._exhaust(runner)
        jobs, _ = jobs_mod.load(p)
        with caplog.at_level(logging.WARNING, logger="agentcore.scheduler.agent_turn"):
            await runner.run_job(jobs[0])
            await runner.run_job(jobs[0])
        assert caplog.text.count("打扰预算") == 1
