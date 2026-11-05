"""Web「定时」页：频率 → cron 表达式、表单校验、jobs.json 落盘。

重点锁三件容易漂移的事：
- 星期字段是 **APScheduler** 口径（0=周一，不是传统 cron 的 0=周日）；
- 单双日是「日期单双号」，不是隔天；
- 新建/编辑后要写 sidecar 的下次时间，否则 tick 会把 next_run=0 当
  「立即跑一次」——Web 保存任务不该在保存瞬间投递一条消息。
"""

import datetime as dt

import pytest

from plugins.qq_agent_adapter import cron_web

NOW = dt.datetime(2026, 11, 6, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))  # 周五


def _base(**kw):
    p = {
        "kind": "agent_turn",
        "name": "测试任务",
        "message": "做点事",
        "target": "private:10000",
        "freq": "daily",
        "time": "14:30",
    }
    p.update(kw)
    return p


class TestSchedule:
    def test_daily(self):
        s = cron_web.build_schedule(_base(), now=NOW)
        assert s["cron"] == "30 14 * * *"
        assert s["delete_after_run"] is False
        assert s["next_run"] == dt.datetime(2026, 11, 6, 14, 30, tzinfo=NOW.tzinfo)

    def test_weekly_uses_apscheduler_weekday(self):
        # 0=周一 … 6=周日。周五 10:00 + 周一/三 14:30 → 下周一。
        s = cron_web.build_schedule(_base(freq="weekly", weekdays=[0, 2]), now=NOW)
        assert s["cron"] == "30 14 * * 0,2"
        assert s["next_run"].weekday() == 0

    def test_weekdays_and_weekends(self):
        assert (
            cron_web.build_schedule(_base(freq="weekdays"), now=NOW)["cron"]
            == "30 14 * * 0-4"
        )
        assert (
            cron_web.build_schedule(_base(freq="weekends"), now=NOW)["cron"]
            == "30 14 * * 5,6"
        )

    def test_odd_even_are_calendar_days(self):
        assert (
            cron_web.build_schedule(_base(freq="odd"), now=NOW)["cron"]
            == "30 14 1-31/2 * *"
        )
        assert (
            cron_web.build_schedule(_base(freq="even"), now=NOW)["cron"]
            == "30 14 2-30/2 * *"
        )

    def test_monthly(self):
        s = cron_web.build_schedule(_base(freq="monthly", day=15), now=NOW)
        assert s["cron"] == "30 14 15 * *"

    def test_once_future(self):
        s = cron_web.build_schedule(_base(freq="once", date="2026-11-10"), now=NOW)
        assert s["cron"] == "30 14 10 11 *"
        assert s["delete_after_run"] is True
        assert s["next_run"].day == 10

    def test_once_past_rejected(self):
        with pytest.raises(cron_web.CronWebError):
            cron_web.build_schedule(_base(freq="once", date="2026-11-01"), now=NOW)

    def test_custom_cron(self):
        s = cron_web.build_schedule(_base(freq="custom", cron="0 9 * * 6"), now=NOW)
        assert s["cron"] == "0 9 * * 6"
        assert s["next_run"].weekday() == 6  # 周日

    def test_bad_inputs_rejected(self):
        for bad in (
            _base(time="25:00"),
            _base(freq="weekly", weekdays=[]),
            _base(freq="weekly", weekdays=[7]),
            _base(freq="monthly", day=32),
            _base(freq="custom", cron=""),
            _base(freq="nope"),
        ):
            with pytest.raises(cron_web.CronWebError):
                cron_web.build_schedule(bad, now=NOW)


class TestNormalize:
    def test_defaults(self):
        item = cron_web.normalize_payload(_base(), {"10000"})
        assert item["enabled"] is True
        assert item["owner"] == "10000"
        assert item["cron"] == "30 14 * * *"
        assert item["next_run_ts"] > 0

    def test_trusted_requires_superuser_owner(self):
        with pytest.raises(cron_web.CronWebError):
            cron_web.normalize_payload(_base(trusted=True), {"10000"})
        with pytest.raises(cron_web.CronWebError):
            cron_web.normalize_payload(_base(trusted=True, owner="20000"), {"10000"})

    def test_trusted_defaults_disabled(self):
        item = cron_web.normalize_payload(_base(trusted=True, owner="10000"), {"10000"})
        assert item["enabled"] is False
        item2 = cron_web.normalize_payload(
            _base(trusted=True, owner="10000", enabled=True), {"10000"}
        )
        assert item2["enabled"] is True

    def test_reminder_message_limit(self):
        with pytest.raises(cron_web.CronWebError):
            cron_web.normalize_payload(
                _base(kind="reminder", message="x" * 201), {"10000"}
            )
        item = cron_web.normalize_payload(
            _base(kind="reminder", message="喝水"), {"10000"}
        )
        assert item["kind"] == "reminder"

    def test_target_validation(self):
        for bad in ("private:abc", "group:", "private:123", "x:12345", ""):
            with pytest.raises(cron_web.CronWebError):
                cron_web.normalize_payload(_base(target=bad), {"10000"})

    def test_name_required(self):
        with pytest.raises(cron_web.CronWebError):
            cron_web.normalize_payload(_base(name="  "), {"10000"})


@pytest.fixture
def cron_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CRON_DIR", str(tmp_path / "cron"))
    return tmp_path / "cron"


class TestPersistence:
    def test_save_load_and_next_run(self, cron_dir):
        from agentcore.scheduler import jobs as jobs_mod

        item = cron_web.normalize_payload(_base(), {"10000"})
        jid = cron_web.save_agent_turn(item)

        jobs, errors = jobs_mod.load()
        assert not errors
        assert [j.id for j in jobs] == [jid]
        assert jobs[0].enabled is True
        assert jobs[0].cron == "30 14 * * *"
        assert jobs[0].owner == "10000"
        assert jobs[0].trusted is False

        # 关键：sidecar 里要有下次时间，否则 tick 的 next_run=0 会立即跑一次
        state = jobs_mod._load_state(jobs_mod.state_path())
        assert state[jid]["nextRunAtMs"] == int(item["next_run_ts"] * 1000)

    def test_edit_keeps_id_and_updates_fields(self, cron_dir):
        from agentcore.scheduler import jobs as jobs_mod

        item = cron_web.normalize_payload(_base(), {"10000"})
        jid = cron_web.save_agent_turn(item)
        item2 = cron_web.normalize_payload(
            _base(id=jid, name="改名", freq="weekdays", time="09:00"), {"10000"}
        )
        assert cron_web.save_agent_turn(item2) == jid
        jobs, _ = jobs_mod.load()
        assert len(jobs) == 1
        assert jobs[0].name == "改名"
        assert jobs[0].cron == "0 9 * * 0-4"

    def test_toggle_and_delete(self, cron_dir):
        from agentcore.scheduler import jobs as jobs_mod

        jid = cron_web.save_agent_turn(cron_web.normalize_payload(_base(), {"10000"}))
        cron_web.toggle_agent_turn(jid, False)
        jobs, _ = jobs_mod.load()
        assert jobs[0].enabled is False
        cron_web.delete_agent_turn(jid)
        assert jobs_mod.load()[0] == []
        with pytest.raises(cron_web.CronWebError):
            cron_web.delete_agent_turn(jid)

    def test_edit_missing_id_rejected(self, cron_dir):
        item = cron_web.normalize_payload(_base(id="nope"), {"10000"})
        with pytest.raises(cron_web.CronWebError):
            cron_web.save_agent_turn(item)


class _FakeStore:
    def __init__(self):
        self.added: list[dict] = []
        self.cancelled: list[tuple] = []

    async def schedule_add(self, **kw):
        self.added.append(kw)
        return "r1"

    async def schedule_cancel(self, sid, user_id=None):
        self.cancelled.append((sid, user_id))
        return True

    async def schedule_list(self, user_id=None, include_disabled=False):
        return [
            {
                "id": "r1",
                "kind": "cron",
                "target": "private:10000",
                "message": "喝水",
                "action": "remind",
                "cron": "0 9 * * *",
                "next_run": 1,
            }
        ]


class TestReminder:
    @pytest.mark.asyncio
    async def test_save_and_edit(self, cron_dir):
        store = _FakeStore()
        item = cron_web.normalize_payload(
            _base(kind="reminder", message="喝水", freq="daily", time="09:00"),
            {"10000"},
        )
        assert await cron_web.save_reminder(item, store) == "r1"
        assert store.added[0]["kind"] == "cron"
        assert store.added[0]["action"] == "remind"
        assert store.added[0]["user_id"] == "10000"

        item2 = cron_web.normalize_payload(
            _base(id="r1", kind="reminder", message="喝水2", freq="daily"),
            {"10000"},
        )
        await cron_web.save_reminder(item2, store)
        assert store.cancelled == [("r1", None)]

    @pytest.mark.asyncio
    async def test_list_all_filters_reminders(self, cron_dir):
        data = await cron_web.list_all(_FakeStore())
        assert data["jobs"] == []
        assert len(data["reminders"]) == 1
