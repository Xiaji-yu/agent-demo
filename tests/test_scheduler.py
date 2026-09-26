"""定时内容推送（M7）与调度面的主题测试。

来源：M7「定时内容推送」落地（REVIEW-bbd8913..f6dffcc L17 点名的口径缺口）。
与提醒同库不同源：``agentcore/scheduler/push.py`` 的配置解析、生成/兜底、
ACL 与每目标上限、失败退避与停用告警，以及提醒与推送**不能互相串味**。

写用例时的纪律（AGENTS.md §2.7）：每条断言都要在「实现被改坏」时失败——
例如只断言「发送过」不够，必须把回退/停用/上限三个分支各自钉死。
"""

import asyncio
import logging
import time

import pytest

from agentcore.scheduler.push import (
    ACTION_PUSH,
    OWNER,
    PushService,
    load_push_config,
    parse_target,
)


class _FakeSink:
    """测试桩：outcome 三态（"sent"/"failed"/"uncertain"）+ 旧布尔接口。"""

    def __init__(self, ok=True, outcome=None):
        self.ok = ok
        self._outcome = outcome
        self.sent: list[tuple[str, str]] = []

    async def send_once(self, target: str, message: str) -> str:
        self.sent.append((target, message))
        if self._outcome is not None:
            return self._outcome
        return "sent" if self.ok else "failed"

    async def send(self, target: str, message: str) -> bool:
        return (await self.send_once(target, message)) == "sent"


class _FakeLLM:
    """按脚本返回响应；``error`` 非空时抛异常。"""

    def __init__(self, content="早安，今天也要加油呀", error=None):
        self.content = content
        self.error = error
        self.calls: list[dict] = []

    async def chat(self, messages, tools=None, max_tokens=None):
        self.calls.append({"messages": messages, "max_tokens": max_tokens})
        if self.error:
            raise self.error
        return {"choices": [{"message": {"content": self.content}}]}


def _blocked_budget():
    class _B:
        def chat_blocked(self):
            return True, "（今日 LLM 预算已用完，服务明日自动恢复。）"

    return _B()


class _OpenBudget:
    def chat_blocked(self):
        return False, ""


def _job_row(**over):
    row = {
        "id": "1",
        "kind": "cron",
        "target": "group:100",
        "message": "模板兜底文案",
        "user_id": OWNER,
        "cron": "0 8 * * *",
        "next_run": time.time() - 1,
        "enabled": True,
        "action": ACTION_PUSH,
        "params": {
            "job_key": "早安问候",
            "prompt": "用一句话道早安",
            "template": "模板兜底文案",
        },
    }
    row.update(over)
    return row


async def _add_job_row(store, **over):
    """把一个到点的 push 行写进 store（id/enabled 由 store 决定，不可传入）。"""
    row = _job_row(**over)
    for key in ("id", "enabled"):
        row.pop(key, None)
    return await store.schedule_add(**row)


def _service(store, sink=None, llm=None, **kw):
    sink = sink or _FakeSink()
    llm = llm or _FakeLLM()
    cfg = kw.pop("config", None)
    service = PushService(
        store,
        sink,
        llm,
        config=cfg,
        allowed_groups=kw.pop("allowed_groups", {"100"}),
        on_alert=kw.pop("on_alert", None),
    )
    return service, sink, llm


# ---------------------------------------------------------------------------
# 配置解析


class TestPushConfigParsing:
    def test_defaults_without_config(self):
        cfg = load_push_config({})
        assert cfg.enabled is True
        assert cfg.tick == 30
        assert cfg.daily_cap_per_target == 1
        assert cfg.max_chars == 500
        assert cfg.jobs == []

    def test_env_overrides_and_dirty_values(self, monkeypatch):
        monkeypatch.setenv("AGENT_PUSH_TICK", "10")
        monkeypatch.setenv("AGENT_PUSH_MAX_FAILURES", "abc")  # 脏值回退 5
        monkeypatch.setenv("AGENT_PUSH_MAX_CHARS", "0")  # 0 = 不限
        cfg = load_push_config({})
        assert cfg.tick == 10
        assert cfg.max_failures == 5
        assert cfg.max_chars == 0

    def test_tick_clamped_to_minimum(self, monkeypatch):
        monkeypatch.setenv("AGENT_PUSH_TICK", "1")
        assert load_push_config({}).tick == 5

    def test_disabled_by_env(self, monkeypatch):
        monkeypatch.setenv("AGENT_PUSH_ENABLED", "0")
        assert load_push_config({"push": {"jobs": []}}).enabled is False

    def test_valid_job_parsed(self):
        cfg = load_push_config(
            {
                "push": {
                    "jobs": [
                        {
                            "name": "早安",
                            "cron": "0 8 * * *",
                            "target": "group:100",
                            "prompt": "道早安",
                            "template": "早上好",
                        }
                    ]
                }
            }
        )
        assert len(cfg.jobs) == 1
        job = cfg.jobs[0]
        assert (job.name, job.cron, job.target) == ("早安", "0 8 * * *", "group:100")
        assert (job.prompt, job.template) == ("道早安", "早上好")

    @pytest.mark.parametrize(
        "raw,why",
        [
            ({"cron": "0 8 * * *", "target": "group:abc"}, "非数字群号"),
            ({"cron": "0 8 * * *", "target": "group:１２３"}, "全角数字"),
            ({"cron": "0 8 * * *", "target": "chat:100"}, "未知类型"),
            ({"cron": "0 8 * * *", "target": "100"}, "缺类型前缀"),
            ({"cron": "bad cron", "target": "group:100"}, "cron 无法解析"),
            ({"cron": "0 8 * * *", "target": "group:100"}, "prompt 与 template 均空"),
            ("不是对象", "整段不是对象"),
        ],
    )
    def test_invalid_jobs_are_skipped(self, raw, why, caplog):
        with caplog.at_level(logging.WARNING):
            cfg = load_push_config({"push": {"jobs": [raw]}})
        assert cfg.jobs == [], why
        assert "WARNING" in caplog.text or any(
            r.levelno >= logging.WARNING for r in caplog.records
        ), why

    def test_jobs_capped_at_max(self, caplog):
        raw = [
            {"name": f"j{i}", "cron": "0 8 * * *", "target": "group:100", "prompt": "x"}
            for i in range(60)
        ]
        with caplog.at_level(logging.WARNING):
            cfg = load_push_config({"push": {"jobs": raw}})
        assert len(cfg.jobs) == 50
        assert any("超过" in r.getMessage() for r in caplog.records)

    def test_full_width_digits_rejected_by_parse_target(self):
        # AGENTS.md §5：全角 "１２３" 也能 isdigit()，会被 int() 成 123（发错人）
        assert "１２３".isdigit() is True
        assert int("１２３") == 123
        assert parse_target("group:１２３") is None

    def test_parse_target_accepts_both_kinds(self):
        assert parse_target("group:100") == ("group", 100)
        assert parse_target("private:42") == ("private", 42)


# ---------------------------------------------------------------------------
# 配置落库（幂等注册）


class TestRegisterJobs:
    @pytest.mark.asyncio
    async def test_registers_and_is_idempotent(self):
        from agentcore.memory.store import InMemoryMemoryStore

        store = InMemoryMemoryStore()
        service, _sink, _llm = _service(store)
        service.cfg.jobs = []
        from agentcore.scheduler.push import PushJob

        job = PushJob(
            name="早安",
            cron="0 8 * * *",
            target="group:100",
            prompt="道早安",
            template="早上好",
        )
        service.cfg.jobs = [job]

        first = await service.register_jobs()
        assert first == {"added": 1, "kept": 0, "replaced": 0, "skipped": 0}
        second = await service.register_jobs()
        assert second == {"added": 0, "kept": 1, "replaced": 0, "skipped": 0}, (
            "重启不得重复登记"
        )
        rows = await store.schedule_list(OWNER, include_disabled=True)
        assert len(rows) == 1
        assert rows[0]["action"] == ACTION_PUSH
        assert rows[0]["params"]["job_key"] == "早安"

    @pytest.mark.asyncio
    async def test_changed_cron_replaces_row(self):
        from agentcore.memory.store import InMemoryMemoryStore
        from agentcore.scheduler.push import PushJob

        store = InMemoryMemoryStore()
        service, _sink, _llm = _service(store)
        service.cfg.jobs = [
            PushJob(
                name="早安",
                cron="0 8 * * *",
                target="group:100",
                prompt="道早安",
                template="早上好",
            )
        ]
        await service.register_jobs()
        old_id = (await service.list_jobs())[0]["id"]

        service.cfg.jobs = [
            PushJob(
                name="早安",
                cron="30 8 * * *",
                target="group:100",
                prompt="道早安",
                template="早上好",
            )
        ]
        stats = await service.register_jobs()
        assert stats["replaced"] == 1
        assert stats["added"] == 0
        rows = await service.list_jobs()
        enabled = [r for r in rows if r["enabled"]]
        assert [r["id"] for r in enabled] == [str(int(old_id) + 1)], (
            "改过时间必须重建，且只能有一条启用行"
        )
        assert enabled[0]["cron"] == "30 8 * * *"
        assert any(not r["enabled"] and r["id"] == old_id for r in rows), (
            "旧行应被停用而不是删除（/push 里要看得见）"
        )

    @pytest.mark.asyncio
    async def test_admin_disabled_job_is_not_resurrected(self):
        from agentcore.memory.store import InMemoryMemoryStore
        from agentcore.scheduler.push import PushJob

        store = InMemoryMemoryStore()
        service, _sink, _llm = _service(store)
        service.cfg.jobs = [
            PushJob(
                name="早安",
                cron="0 8 * * *",
                target="group:100",
                prompt="道早安",
                template="早上好",
            )
        ]
        await service.register_jobs()
        row = (await service.list_jobs())[0]
        await service.disable_job(row["id"])

        stats = await service.register_jobs()
        assert stats["added"] == 0, "/push off 停用的任务不得被重启复活"
        assert [r["id"] for r in await service.list_jobs() if r["enabled"]] == []


# ---------------------------------------------------------------------------
# 到点投递


class TestPushDelivery:
    @pytest.mark.asyncio
    async def test_cron_job_sends_generated_text_and_reschedules(self):
        from agentcore.memory.store import InMemoryMemoryStore

        store = InMemoryMemoryStore()
        await _add_job_row(store)
        service, sink, llm = _service(store)

        out = await service.tick()
        assert out == {"due": 1, "sent": 1, "failed": 0, "capped": 0, "disabled": 0}
        assert sink.sent and sink.sent[0][0] == "group:100"
        assert sink.sent[0][1] == "早安，今天也要加油呀"
        assert llm.calls, "有 prompt 时必须调 LLM"
        rows = await store.schedule_list(OWNER, include_disabled=True)
        assert len(rows) == 1 and rows[0]["enabled"] is True
        assert rows[0]["next_run"] > time.time(), "cron 任务要推进到下次触发"

    @pytest.mark.asyncio
    async def test_once_job_disables_after_send(self):
        from agentcore.memory.store import InMemoryMemoryStore

        store = InMemoryMemoryStore()
        await _add_job_row(store, kind="once", cron="")
        service, sink, _llm = _service(store)
        assert await service.tick() == {
            "due": 1,
            "sent": 1,
            "failed": 0,
            "capped": 0,
            "disabled": 0,
        }
        rows = await store.schedule_list(OWNER, include_disabled=True)
        assert rows[0]["enabled"] is False, "一次性任务送达即停用"
        assert (await service.tick())["due"] == 0
        assert len(sink.sent) == 1

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_template(self):
        from agentcore.memory.store import InMemoryMemoryStore

        store = InMemoryMemoryStore()
        await _add_job_row(store)
        service, sink, _llm = _service(store, llm=_FakeLLM(error=RuntimeError("boom")))
        out = await service.tick()
        assert out["sent"] == 1
        assert sink.sent[0][1] == "模板兜底文案", "LLM 挂了也要把内容发出去"

    @pytest.mark.asyncio
    async def test_budget_gate_falls_back_to_template(self, monkeypatch):
        from agentcore.memory.store import InMemoryMemoryStore

        store = InMemoryMemoryStore()
        await _add_job_row(store)
        service, sink, llm = _service(store)
        monkeypatch.setattr("agentcore.scheduler.push.get_budget", _blocked_budget)
        out = await service.tick()
        assert out["sent"] == 1
        assert sink.sent[0][1] == "模板兜底文案"
        assert llm.calls == [], "预算熔断时不得再打 LLM"

    @pytest.mark.asyncio
    async def test_empty_llm_output_falls_back_to_template(self):
        from agentcore.memory.store import InMemoryMemoryStore

        store = InMemoryMemoryStore()
        await _add_job_row(store)
        service, sink, _llm = _service(store, llm=_FakeLLM(content="   "))
        out = await service.tick()
        assert out["sent"] == 1
        assert sink.sent[0][1] == "模板兜底文案"

    @pytest.mark.asyncio
    async def test_no_prompt_uses_template_without_llm(self):
        from agentcore.memory.store import InMemoryMemoryStore

        store = InMemoryMemoryStore()
        await _add_job_row(
            store,
            params={"job_key": "早安", "prompt": "", "template": "早上好呀"},
        )
        service, sink, llm = _service(store)
        out = await service.tick()
        assert out["sent"] == 1
        assert sink.sent[0][1] == "早上好呀"
        assert llm.calls == []

    @pytest.mark.asyncio
    async def test_max_chars_clamps_long_generation(self):
        from agentcore.memory.store import InMemoryMemoryStore
        from agentcore.scheduler.push import PushConfig

        store = InMemoryMemoryStore()
        await _add_job_row(store)
        service, sink, _llm = _service(
            store,
            llm=_FakeLLM(content="很长" * 400),
            config=PushConfig(max_chars=50, daily_cap_per_target=1),
        )
        out = await service.tick()
        assert out["sent"] == 1
        assert len(sink.sent[0][1]) == 50

    @pytest.mark.asyncio
    async def test_reminders_are_not_touched_by_push_tick(self):
        """两个服务共用 schedules 表：提醒行不能被推送轮询当成推送发出去。"""
        from agentcore.memory.store import InMemoryMemoryStore

        store = InMemoryMemoryStore()
        await store.schedule_add(
            kind="once",
            target="private:1",
            message="吃药",
            user_id="1",
            next_run=time.time() - 1,
        )
        service, sink, _llm = _service(store)
        assert (await service.tick())["due"] == 0
        assert sink.sent == []
        row = (await store.schedule_list("1"))[0]
        assert row["enabled"] is True, "推送轮询不得动提醒的状态"


# ---------------------------------------------------------------------------
# ACL 与上限


class TestAclAndCaps:
    @pytest.mark.asyncio
    async def test_group_outside_allowlist_is_disabled_and_alerted(self):
        from agentcore.memory.store import InMemoryMemoryStore

        store = InMemoryMemoryStore()
        await _add_job_row(store, target="group:999")
        alerts = []
        service, sink, _llm = _service(
            store, allowed_groups={"100"}, on_alert=alerts.append
        )
        out = await service.tick()
        assert out["disabled"] == 1 and out["sent"] == 0
        assert sink.sent == [], "不在白名单的群一条消息都不能发"
        rows = await store.schedule_list(OWNER, include_disabled=True)
        assert rows[0]["enabled"] is False
        assert alerts and "ALLOWED_GROUPS" in alerts[0]

    @pytest.mark.asyncio
    async def test_empty_allowlist_blocks_all_groups(self):
        from agentcore.memory.store import InMemoryMemoryStore

        store = InMemoryMemoryStore()
        await _add_job_row(store)
        service, sink, _llm = _service(store, allowed_groups=set())
        assert (await service.tick())["disabled"] == 1
        assert sink.sent == []

    @pytest.mark.asyncio
    async def test_private_target_needs_no_group_allowlist(self):
        from agentcore.memory.store import InMemoryMemoryStore

        store = InMemoryMemoryStore()
        await _add_job_row(store, target="private:42")
        service, sink, _llm = _service(store, allowed_groups=set())
        assert (await service.tick())["sent"] == 1
        assert sink.sent[0][0] == "private:42"

    @pytest.mark.asyncio
    async def test_daily_cap_skips_but_still_reschedules(self):
        from agentcore.memory.store import InMemoryMemoryStore

        store = InMemoryMemoryStore()
        await _add_job_row(store)
        service, sink, _llm = _service(store)
        assert (await service.tick())["sent"] == 1
        # 同一个 tick 到达两次（配置里两个任务指向同一群）时第二次应被上限挡下
        out = await service.process(_job_row(next_run=time.time() - 1))
        assert out == "capped"
        assert len(sink.sent) == 1, "每目标每日上限必须真的挡住投递"
        rows = await store.schedule_list(OWNER, include_disabled=True)
        assert rows[0]["next_run"] > time.time(), "被挡下也要推进，否则每 tick 空转"

    @pytest.mark.asyncio
    async def test_cap_zero_means_unlimited(self):
        from agentcore.memory.store import InMemoryMemoryStore
        from agentcore.scheduler.push import PushConfig

        store = InMemoryMemoryStore()
        await _add_job_row(store)
        service, sink, _llm = _service(store, config=PushConfig(daily_cap_per_target=0))
        assert await service.tick() == {
            "due": 1,
            "sent": 1,
            "failed": 0,
            "capped": 0,
            "disabled": 0,
        }
        assert await service.process(_job_row(next_run=time.time() - 1)) == "sent"
        assert len(sink.sent) == 2


# ---------------------------------------------------------------------------
# 失败退避与停用


class TestFailureHandling:
    @pytest.mark.asyncio
    async def test_failure_retries_with_exponential_backoff(self):
        from agentcore.memory.store import InMemoryMemoryStore
        from agentcore.scheduler.push import PushConfig

        store = InMemoryMemoryStore()
        await _add_job_row(store)
        service, _sink, _llm = _service(
            store,
            sink=_FakeSink(ok=False),
            config=PushConfig(retry_delay=300, max_retry_delay=3600, max_failures=5),
        )
        now = time.time()
        assert await service.process(_job_row(next_run=now - 1)) == "failed"
        first = (await store.schedule_list(OWNER, include_disabled=True))[0]["next_run"]
        assert 250 <= first - now <= 400, f"首次顺延应约 retry_delay：{first - now}"

        second_now = time.time()
        assert await service.process(_job_row(next_run=second_now - 1)) == "failed"
        second = (await store.schedule_list(OWNER, include_disabled=True))[0][
            "next_run"
        ]
        assert 500 <= second - second_now <= 900, f"第二次应翻倍：{second - second_now}"
        assert second > first, "退避必须递增"

    @pytest.mark.asyncio
    async def test_repeated_failures_disable_and_alert(self):
        from agentcore.memory.store import InMemoryMemoryStore
        from agentcore.scheduler.push import PushConfig

        store = InMemoryMemoryStore()
        await _add_job_row(store)
        alerts = []
        service, sink, _llm = _service(
            store,
            sink=_FakeSink(ok=False),
            config=PushConfig(retry_delay=60, max_retry_delay=120, max_failures=3),
            on_alert=alerts.append,
        )
        for _ in range(2):
            assert await service.process(_job_row(next_run=time.time() - 1)) == "failed"
        assert await service.process(_job_row(next_run=time.time() - 1)) == "disabled"
        rows = await store.schedule_list(OWNER, include_disabled=True)
        assert rows[0]["enabled"] is False, "连续失败必须停用，不能无限重试"
        assert alerts and "连续 3 次投递失败" in alerts[0]
        # 停用后不会再被 tick 拾起（_fake sink 连失败尝试也记账，故比对次数不增长）
        attempts = len(sink.sent)
        assert (await service.tick())["due"] == 0
        assert len(sink.sent) == attempts

    @pytest.mark.asyncio
    async def test_success_clears_failure_counter(self):
        from agentcore.memory.store import InMemoryMemoryStore
        from agentcore.scheduler.push import PushConfig

        store = InMemoryMemoryStore()
        await _add_job_row(store)
        service, _sink, _llm = _service(
            store,
            sink=_FakeSink(ok=False),
            config=PushConfig(retry_delay=60, max_failures=2, daily_cap_per_target=0),
        )
        row = _job_row(next_run=time.time() - 1)
        sid = str(row["id"])
        assert await service.process(row) == "failed"
        assert service._failures[sid] == 1
        # 恢复送达后计数清零：下一次失败要从头数，而不是直接触发停用
        service.sink = _FakeSink(ok=True)
        assert await service.process(row) == "sent"
        assert service._failures.get(sid) is None
        service.sink = _FakeSink(ok=False)
        assert await service.process(row) == "failed"
        assert service._failures[sid] == 1
        rows = await store.schedule_list(OWNER, include_disabled=True)
        assert rows[0]["enabled"] is True, "计数已清零，不得因这次失败被停用"

    @pytest.mark.asyncio
    async def test_content_empty_without_prompt_or_template_disables(self):
        from agentcore.memory.store import InMemoryMemoryStore

        store = InMemoryMemoryStore()
        await _add_job_row(
            store,
            message="",
            params={"job_key": "空任务", "prompt": "", "template": ""},
        )
        alerts = []
        service, sink, _llm = _service(store, on_alert=alerts.append)
        out = await service.tick()
        assert out["disabled"] == 1 and out["sent"] == 0
        assert sink.sent == []
        assert alerts and "没有可推送内容" in alerts[0]


# ---------------------------------------------------------------------------
# 管理入口（/push）


class TestPushAdminCommand:
    @staticmethod
    def _admin():
        import importlib as _il

        import nonebot

        nonebot.init(_env_file=None, superusers={"10000"})
        return _il.import_module("plugins.qq_agent_adapter.admin")

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("/push", ("list", "")),
            ("/push ls", ("list", "")),
            ("/push off 3", ("off", "3")),
            ("/push stop 3", ("off", "3")),
            ("/push run 7", ("run", "7")),
            ("/push help", ("help", "")),
            ("/定时推送 off 2", ("off", "2")),
            ("/push 不认识的东西", ("help", "")),
        ],
    )
    def test_parse_push_cmd(self, raw, expected):
        admin = self._admin()
        assert admin.parse_push_cmd(raw) == expected

    @pytest.mark.asyncio
    async def test_non_superuser_cannot_manage(self, monkeypatch):
        from nonebot.exception import FinishedException

        admin = self._admin()
        calls = []

        async def fake_finish(msg=None, **kw):
            calls.append(msg)
            raise FinishedException()

        monkeypatch.setattr(admin.push_cmd, "finish", fake_finish)
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: False)

        def _boom():
            raise AssertionError("非管理员不应触达推送服务")

        monkeypatch.setattr(admin, "_get_push", _boom)

        class _Ev:
            def get_message(self):
                return "/push"

            def get_user_id(self):
                return "20002"

        with pytest.raises(FinishedException):
            await admin.handle_push(_Ev())
        assert calls == ["只有管理员能管理定时推送。"]

    @pytest.mark.asyncio
    async def test_off_and_run_flows(self, monkeypatch):
        from nonebot.exception import FinishedException

        from agentcore.memory.store import InMemoryMemoryStore

        admin = self._admin()
        calls = []

        async def fake_finish(msg=None, **kw):
            calls.append(msg)
            raise FinishedException()

        monkeypatch.setattr(admin.push_cmd, "finish", fake_finish)
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: True)

        store = InMemoryMemoryStore()
        service, _sink, _llm = _service(store)
        await _add_job_row(store)

        class _Ev:
            def __init__(self, text):
                self._text = text

            def get_message(self):
                return self._text

            def get_user_id(self):
                return "10000"

        monkeypatch.setattr(admin, "_get_push", lambda: service)

        with pytest.raises(FinishedException):
            await admin.handle_push(_Ev("/push list"))
        assert calls[-1].startswith("推送任务（1 条）")
        assert "#1" in calls[-1] and "group:100" in calls[-1]

        with pytest.raises(FinishedException):
            await admin.handle_push(_Ev("/push run 1"))
        assert "已触发 #1" in calls[-1]

        with pytest.raises(FinishedException):
            await admin.handle_push(_Ev("/push off 1"))
        assert calls[-1] == "已停用推送任务 #1"
        assert [r["id"] for r in await service.list_jobs() if r["enabled"]] == []

        with pytest.raises(FinishedException):
            await admin.handle_push(_Ev("/push off 1"))
        assert calls[-1].startswith("没找到可停用的推送任务 #1")

    @pytest.mark.asyncio
    async def test_disabled_service_is_explained(self, monkeypatch):
        from nonebot.exception import FinishedException

        admin = self._admin()
        calls = []

        async def fake_finish(msg=None, **kw):
            calls.append(msg)
            raise FinishedException()

        monkeypatch.setattr(admin.push_cmd, "finish", fake_finish)
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: True)
        monkeypatch.setattr(admin, "_get_push", lambda: None)

        class _Ev:
            def get_message(self):
                return "/push"

            def get_user_id(self):
                return "10000"

        with pytest.raises(FinishedException):
            await admin.handle_push(_Ev())
        assert "未启用" in calls[-1]


class TestRescheduleEdges:
    """cron 算不出下次触发时间时必须停用并留痕（否则每 tick 重发同一条）。"""

    @pytest.mark.asyncio
    async def test_uncomputable_cron_disables_job(self, monkeypatch, caplog):
        from agentcore.memory.store import InMemoryMemoryStore

        monkeypatch.setattr(
            "agentcore.scheduler.push.next_cron_time", lambda *a, **k: None
        )
        store = InMemoryMemoryStore()
        await _add_job_row(store)
        service, sink, _llm = _service(store)
        with caplog.at_level(logging.ERROR):
            assert (await service.tick())["sent"] == 1
        rows = await store.schedule_list(OWNER, include_disabled=True)
        assert rows[0]["enabled"] is False, "算不出下次触发就必须停用"
        assert any("无法计算下次触发时间" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_reminders_ignored_even_when_due_together(self):
        """同 tick 里既有提醒又有推送时互不干扰（两个 job 各自取数）。"""
        from agentcore.memory.store import InMemoryMemoryStore
        from agentcore.scheduler.reminder import ReminderService

        store = InMemoryMemoryStore()
        await _add_job_row(store)
        await store.schedule_add(
            kind="once",
            target="private:1",
            message="喝水",
            user_id="1",
            next_run=time.time() - 1,
        )
        push_sink = _FakeSink()
        service, _s, _l = _service(store, sink=push_sink)
        reminder_sink = _FakeSink()
        assert (await service.tick())["sent"] == 1
        out = await ReminderService(store, reminder_sink).tick()
        assert out == {"due": 1, "sent": 1, "failed": 0, "uncertain": 0}
        assert "⏰ 提醒：喝水" in reminder_sink.sent[0][1]
        # 推送那条走的是 LLM 生成内容，不是提醒模板
        assert push_sink.sent[0][0] == "group:100"
        assert "喝水" not in push_sink.sent[0][1]


class TestBudgetGatePromptOnlyReschedules:
    """预算闸门属临时态：prompt-only 任务撞闸门须重新调度，不得永久停用（审查 P1）。

    旧实现 `_content` 把「预算挡住生成」与「配置无内容」都折叠成空串，process
    一律 `_disable("没有可推送内容…")`——预算恢复后任务已经没了，还误告警一次。
    """

    @pytest.mark.asyncio
    async def test_budget_gate_prompt_only_skips_and_reschedules(self, monkeypatch):
        from agentcore.memory.store import InMemoryMemoryStore

        store = InMemoryMemoryStore()
        await _add_job_row(
            store,
            params={"job_key": "早安", "prompt": "道早安", "template": ""},
        )
        alerts = []
        service, sink, llm = _service(store, on_alert=alerts.append)
        monkeypatch.setattr("agentcore.scheduler.push.get_budget", _blocked_budget)
        out = await service.tick()
        assert out["capped"] == 1, out
        assert out["disabled"] == 0
        assert sink.sent == [], "无模板兜底时不得发送"
        assert llm.calls == [], "预算熔断时不得再打 LLM"
        assert alerts == [], "临时跳过不是配置错误，不得触发停用告警"
        rows = await store.schedule_list(OWNER, include_disabled=True)
        assert rows and rows[0]["enabled"] is True, "任务必须保留到下个周期"
        assert rows[0]["next_run"] is not None and rows[0]["next_run"] > time.time()

    @pytest.mark.asyncio
    async def test_truly_empty_config_still_disables(self):
        """真·无内容配置（prompt 与 template 均空）仍须停用——语义不变。"""
        from agentcore.memory.store import InMemoryMemoryStore

        store = InMemoryMemoryStore()
        await _add_job_row(
            store,
            params={"job_key": "空任务", "prompt": "", "template": ""},
        )
        service, sink, _llm = _service(store)
        out = await service.tick()
        assert out["disabled"] == 1
        assert sink.sent == []


class TestPushConfigYamlScalars:
    """config.yaml 的 push 标量曾被静默丢弃（只认 env + 内置默认）（审查 P1 回归）。

    现在解析顺序为 env > config.yaml > 内置默认，脏值告警回退。
    """

    def test_yaml_scalars_honored(self):
        cfg = load_push_config(
            {
                "push": {
                    "enabled": False,
                    "tick": 60,
                    "retry_delay": 120,
                    "max_failures": 3,
                    "max_retry_delay": 3600,
                    "daily_cap_per_target": 2,
                    "max_chars": 800,
                    "jobs": [],
                }
            }
        )
        assert cfg.enabled is False
        assert (cfg.tick, cfg.retry_delay, cfg.max_failures) == (60, 120, 3)
        assert (
            cfg.max_retry_delay,
            cfg.daily_cap_per_target,
            cfg.max_chars,
        ) == (3600, 2, 800)

    def test_env_beats_yaml(self, monkeypatch):
        monkeypatch.setenv("AGENT_PUSH_TICK", "10")
        cfg = load_push_config({"push": {"tick": 60}})
        assert cfg.tick == 10

    def test_yaml_dirty_values_warn_and_fall_back(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            cfg = load_push_config({"push": {"tick": "abc", "max_failures": 0}})
        assert cfg.tick == 30
        assert cfg.max_failures == 5
        assert "push.tick" in caplog.text
        assert "push.max_failures" in caplog.text


class TestUnifiedDayKey:
    """预算日界/推送日键/cron 语义统一到 agentcore.tz（重审 P1/P2 回归）。

    旧 push 实现写 `dt.ZoneInfo(...)`（datetime 模块无此属性）被 except 吞掉，
    日键实为 no-op；budget 用服务器本地 date.today()，三处「今天」互不相同。
    """

    TS = 1767198600  # UTC 2025-12-31 16:30 == 上海 2026-01-01 00:30

    def test_day_key_shanghai(self, monkeypatch):
        import agentcore.tz as tz

        monkeypatch.setenv("AGENT_SCHEDULER_TZ", "Asia/Shanghai")
        assert tz.day_key(self.TS) == "2026-01-01"
        assert tz.today_date().isoformat() == tz.day_key()

    def test_day_key_env_override_utc(self, monkeypatch):
        import agentcore.tz as tz

        monkeypatch.setenv("AGENT_SCHEDULER_TZ", "UTC")
        assert tz.day_key(self.TS) == "2025-12-31"

    def test_push_day_key_follows_tz(self, monkeypatch):
        from agentcore.scheduler.push import PushService

        monkeypatch.setenv("AGENT_SCHEDULER_TZ", "UTC")
        svc = PushService(store=None, sink=None, llm=None)
        assert svc._day_key(self.TS) == "2025-12-31"
        monkeypatch.setenv("AGENT_SCHEDULER_TZ", "Asia/Shanghai")
        assert svc._day_key(self.TS) == "2026-01-01"

    def test_anchor_matches_day_key(self, monkeypatch):
        from agentcore.scheduler.push import PushService, _today_anchor

        monkeypatch.setenv("AGENT_SCHEDULER_TZ", "UTC")
        svc = PushService(store=None, sink=None, llm=None)
        day = svc._day_key(time.time())
        anchor = _today_anchor()
        y, m, d = day.split("-")
        assert f"{int(y)} 年 {int(m)} 月 {int(d)} 日" in anchor

    def test_next_cron_time_uses_unified_tz(self, monkeypatch):
        """UTC 覆盖下「0 9 * * *」的下次触发应是 UTC 09:00（北京 17:00）。"""
        from agentcore.scheduler.reminder import next_cron_time

        monkeypatch.setenv("AGENT_SCHEDULER_TZ", "UTC")
        # after: 2026-01-01 00:30 UTC → next fire 同日 09:00 UTC
        nxt = next_cron_time("0 9 * * *", after_ts=self.TS)
        assert nxt is not None
        import datetime as dt

        fire = dt.datetime.fromtimestamp(nxt, dt.UTC)
        assert (fire.hour, fire.minute) == (9, 0), fire


class TestPushRegisterOrphans:
    """配置删除任务后孤儿启用行必须停用 + 同名任务去重（审查 P1/verified）。"""

    @pytest.mark.asyncio
    async def test_removed_job_row_gets_disabled(self, tmp_path):
        from agentcore.memory.store import InMemoryMemoryStore
        from agentcore.scheduler.push import (
            PushConfig,
            PushJob,
            PushService,
        )

        store = InMemoryMemoryStore()
        await store.init()
        job = PushJob(
            name="news",
            cron="* * * * *",
            target="g:1",
            prompt="早报",
            template="早报来了",
        )
        svc = PushService(store, None, None, config=PushConfig(jobs=[job]))
        stats = await svc.register_jobs()
        assert stats["added"] == 1
        rows = [
            r
            for r in await store.schedule_list(OWNER, include_disabled=True)
            if r.get("enabled")
        ]
        assert len(rows) == 1

        # 配置里删掉该任务再重启注册：孤儿启用行停用，不再投递
        svc2 = PushService(store, None, None, config=PushConfig(jobs=[]))
        stats2 = await svc2.register_jobs()
        assert stats2.get("disabled") == 1
        rows = [
            r
            for r in await store.schedule_list(OWNER, include_disabled=True)
            if r.get("enabled")
        ]
        assert rows == []

    @pytest.mark.asyncio
    async def test_duplicate_job_names_kept_once(self, tmp_path):
        from agentcore.memory.store import InMemoryMemoryStore
        from agentcore.scheduler.push import PushConfig, PushJob, PushService

        store = InMemoryMemoryStore()
        await store.init()
        jobs = [
            PushJob(
                name="dup", cron="* * * * *", target="g:1", prompt="a", template="t1"
            ),
            PushJob(
                name="dup", cron="*/5 * * * *", target="g:2", prompt="b", template="t2"
            ),
        ]
        svc = PushService(store, None, None, config=PushConfig(jobs=jobs))
        stats = await svc.register_jobs()
        assert stats["added"] == 1, "同名只登记首条"
        enabled = [
            r
            for r in await store.schedule_list(OWNER, include_disabled=True)
            if r.get("enabled")
        ]
        assert len(enabled) == 1


class TestPushDailyCapPersistence:
    """每日每目标上限跨进程持久化（审查 P1 verified：重启清零=上限失效）。"""

    def test_state_roundtrip(self, tmp_path):
        import time as _t

        from agentcore.memory.store import InMemoryMemoryStore
        from agentcore.scheduler.push import PushService

        state_file = tmp_path / "push-state.json"

        async def _run():
            store = InMemoryMemoryStore()
            await store.init()
            svc = PushService(store, None, None, state_file=str(state_file))
            svc._bump_delivered("g:1", _t.time())
            svc._bump_delivered("g:1", _t.time())
            return svc

        asyncio.run(_run())

        # 新进程加载同一天计数：cap=2 时立即视为达限
        async def _verify():
            store2 = InMemoryMemoryStore()
            await store2.init()
            svc2 = PushService(store2, None, None, state_file=str(state_file))
            svc2.cfg.daily_cap_per_target = 2
            assert svc2._cap_reached("g:1", _t.time()) is True
            assert svc2._cap_reached("g:2", _t.time()) is False

        asyncio.run(_verify())

    def test_state_file_atomic_and_private(self, tmp_path):
        import os
        import time as _t

        from agentcore.memory.store import InMemoryMemoryStore
        from agentcore.scheduler.push import PushService

        state_file = tmp_path / "push-state.json"
        store = InMemoryMemoryStore()
        svc = PushService(store, None, None, state_file=str(state_file))
        svc._bump_delivered("g:9", _t.time())
        assert state_file.exists()
        assert not state_file.with_suffix(".json.part").exists(), "原子替换后无残留"
        assert os.stat(state_file).st_mode & 0o777 == 0o600


class TestUncertainNoResend:
    """sink 结果不确定 → 不重发、照常推进调度（审查 P2：违反不重发铁律面）。"""

    @pytest.mark.asyncio
    async def test_reminder_uncertain_advances_once(self):
        from agentcore.memory.store import InMemoryMemoryStore
        from agentcore.scheduler.reminder import ReminderService

        store = InMemoryMemoryStore()
        await store.init()
        await store.schedule_add(
            kind="once",
            target="private:1",
            message="喝水",
            user_id="1",
            next_run=time.time() - 1,
        )
        sink = _FakeSink(outcome="uncertain")
        out = await ReminderService(store, sink).tick()
        assert out["uncertain"] == 1 and out["sent"] == 0
        # 立刻再 tick：行已推进，不得再次投递（重复投递=铁律违规）
        out2 = await ReminderService(store, sink).tick()
        assert out2["due"] == 0
        assert len(sink.sent) == 1

    @pytest.mark.asyncio
    async def test_push_uncertain_does_not_count_failure(self):
        from agentcore.memory.store import InMemoryMemoryStore
        from agentcore.scheduler.push import (
            PushConfig,
            PushJob,
            PushService,
        )

        store = InMemoryMemoryStore()
        await store.init()
        job = PushJob(
            name="u", cron="* * * * *", target="group:1", prompt="早", template="早"
        )
        sink = _FakeSink(outcome="uncertain")
        svc = PushService(
            store,
            sink,
            None,
            config=PushConfig(jobs=[job]),
            allowed_groups={"1"},
        )
        stats = await svc.register_jobs()
        assert stats["added"] == 1
        rows = await svc._push_rows()
        row = rows[0]
        result = await svc.process(row, time.time())
        assert result == "uncertain"
        assert svc._failures.get(row["id"], 0) == 0, "不确定不计失败（不触发停用）"
