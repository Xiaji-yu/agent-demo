"""总览面板「待决定区」（P4）的回归用例。

来源：REVIEW 2026-11-02 全量评审第二轮 P4（``plugins/qq_agent_adapter/web.py``
的 ``_pending_decisions``）。为什么单独一个文件：``tests/`` 下原先没有任何
``build_overview`` 用例，这一块从零起步。

锁三件事：
1. 三类悬空事项各自成型（出站 uncertain / 被预算拦下 / 没跑成的一次性 job）；
2. 只出字段不出正文——job 的 message 一个字都不进面板响应；
3. 单类取不到只进 ``errors``，其余照常（总览「任何一块失败不影响其他块」铁律）。
"""

from __future__ import annotations

import json

import pytest

from agentcore import outbox
from agentcore.scheduler import jobs as jobs_mod
from agentcore.scheduler.notify_budget import get_notify_budget
from plugins.qq_agent_adapter import web


def _job(jid: str, **over) -> dict:
    """一份合法的 jobs.json 条目（OpenClaw 兼容 schema，见 cron-jobs.example.json）。"""
    raw = {
        "id": jid,
        "name": f"任务-{jid}",
        "enabled": True,
        "schedule": {"kind": "cron", "expr": "0 9 * * *", "tz": "Asia/Shanghai"},
        "payload": {
            "kind": "agent_turn",
            "message": f"正文-{jid}",
            "channel": "onebot",
            "to": "group:123456789",
        },
        "deleteAfterRun": True,
    }
    raw.update(over)
    return raw


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    """出站日志与 cron 目录都指到临时目录（notify_budget 由 conftest 统一隔离）。"""
    monkeypatch.setenv("AGENT_OUTBOX_FILE", str(tmp_path / "outbox.jsonl"))
    monkeypatch.setenv("AGENT_CRON_DIR", str(tmp_path / "cron"))
    (tmp_path / "cron").mkdir()
    yield


def _write_jobs(rows: list[dict]) -> None:
    jobs_mod.jobs_path().write_text(
        json.dumps({"version": 1, "jobs": rows}, ensure_ascii=False), encoding="utf-8"
    )


def _write_state(states: dict) -> None:
    """写 sidecar 运行态。**顶层扁平** ``{<id>: {...}}``——上游 M4 把运行态从
    jobs.json 搬进 sidecar 后就是这个形态（``jobs.py:451-459 update_state``），
    包一层 "jobs" 会让 ``load`` 的 ``state.get(job.id)`` 全部取空。
    """
    jobs_mod.state_path().write_text(
        json.dumps(states, ensure_ascii=False), encoding="utf-8"
    )


def _append_outbox(rows: list[dict]) -> None:
    path = outbox.journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


class TestPendingOutbox:
    """① 出站悬空投递：只列 uncertain，且**只读不消费**。"""

    def test_only_uncertain_listed(self):
        _append_outbox(
            [
                {
                    "ts": 1.0,
                    "target": "group:1",
                    "outcome": "sent",
                    "ref": "a",
                    "detail": "",
                },
                {
                    "ts": 2.0,
                    "target": "group:2",
                    "outcome": "failed",
                    "ref": "b",
                    "detail": "x",
                },
                {
                    "ts": 3.0,
                    "target": "group:3",
                    "outcome": "uncertain",
                    "ref": "c",
                    "detail": "timeout",
                },
            ]
        )
        res = web._pending_decisions()
        assert [e["target"] for e in res["outbox_uncertain"]] == ["group:3"]
        assert res["outbox_uncertain"][0]["detail"] == "timeout"
        assert res["errors"] == []

    def test_recent_is_read_not_consumed(self):
        """面板绝不能吃掉 uncertain 行——启动通知才是唯一认领方。"""
        _append_outbox(
            [
                {
                    "ts": 1.0,
                    "target": "group:1",
                    "outcome": "uncertain",
                    "ref": "a",
                    "detail": "",
                },
            ]
        )
        first = web._pending_decisions()
        second = web._pending_decisions()
        assert first["outbox_uncertain"] == second["outbox_uncertain"] != []
        # 文件里的 uncertain 行还在（take_uncertain 会把它删掉）
        assert len(outbox.recent(50)) == 1

    def test_ref_and_detail_truncated(self):
        _append_outbox(
            [
                {
                    "ts": 1.0,
                    "target": "group:1",
                    "outcome": "uncertain",
                    "ref": "r" * 100,
                    "detail": "d" * 400,
                },
            ]
        )
        res = web._pending_decisions()
        e = res["outbox_uncertain"][0]
        assert len(e["ref"]) == 64
        assert len(e["detail"]) == 200

    def test_capped_at_ten_and_scans_last_200(self):
        rows = [
            {
                "ts": float(i),
                "target": f"group:{i}",
                "outcome": "uncertain",
                "ref": "",
                "detail": "",
            }
            for i in range(300)
        ]
        _append_outbox(rows)
        res = web._pending_decisions()
        got = [e["target"] for e in res["outbox_uncertain"]]
        # 只扫最近 200 行、只列最近 10 条
        assert got == [f"group:{i}" for i in range(290, 300)]

    def test_no_file_is_empty_not_error(self):
        res = web._pending_decisions()
        assert res["outbox_uncertain"] == []
        assert res["errors"] == []


class TestPendingBudgetBlocked:
    """② 被全局打扰预算拦下的投递：事实（record_blocked）而非推断。"""

    def test_blocked_snapshot_surfaced_sorted_by_total(self):
        nb = get_notify_budget()
        nb.record_blocked("group:1", "agent_turn")
        nb.record_blocked("group:1", "agent_turn")
        nb.record_blocked("group:2", "nudge")
        res = web._pending_decisions()
        snap = res["budget_blocked"]
        assert snap["limit"] == 8
        assert list(snap["targets"]) == ["group:1", "group:2"]  # 按 total 降序
        assert snap["targets"]["group:1"] == {"total": 2, "agent_turn": 2}
        assert snap["targets"]["group:2"] == {"total": 1, "nudge": 1}

    def test_nothing_blocked_is_empty(self):
        res = web._pending_decisions()
        assert res["budget_blocked"]["targets"] == {}
        assert res["errors"] == []

    def test_blocked_is_not_spent(self):
        """拦下 = 没送达：绝不能占自己的额度（否则上限提前用尽）。"""
        nb = get_notify_budget()
        nb.record_blocked("group:1", "push")
        assert nb.spent("group:1") == 0
        assert nb.by_kind("group:1") == {}


class TestPendingOneShotJobs:
    """③ 待处理的一次性 job。

    M6（2026-11 评审）：判据从「还在文件里 + 开着」改为 **state.json 的
    ``lastStatus != "ok"``**——``deleteAfterRun`` 从来没有删除实现（jobs.json
    已回归纯配置、运行态只写 sidecar），跑成的 job 会一直躺在文件里，按旧判据
    面板会永久挂着一条假阳性。
    """

    def test_enabled_one_shot_listed(self):
        _write_jobs([_job("once")])
        res = web._pending_decisions()
        assert [j["id"] for j in res["one_shot_jobs"]] == ["once"]
        assert res["one_shot_jobs_total"] == 1
        assert res["one_shot_jobs_errors"] == 0
        assert res["errors"] == []

    def test_disabled_and_recurring_excluded(self):
        _write_jobs(
            [
                _job("once"),
                _job("off", enabled=False),
                _job("keep", deleteAfterRun=False),
            ]
        )
        res = web._pending_decisions()
        assert [j["id"] for j in res["one_shot_jobs"]] == ["once"]

    def test_succeeded_one_shot_drops_off(self):
        """lastStatus=ok 的一次性 job 不得再挂榜（旧判据的假阳性）。"""
        _write_jobs([_job("once")])
        _write_state({"once": {"lastStatus": "ok", "lastRunAtMs": 1}})
        res = web._pending_decisions()
        assert res["one_shot_jobs"] == []
        assert res["one_shot_jobs_total"] == 0

    def test_failed_one_shot_stays_listed(self):
        """失败/被拦的一次性 job 仍要挂榜——它需要人来看。"""
        _write_jobs([_job("once")])
        _write_state({"once": {"lastStatus": "failed"}})
        res = web._pending_decisions()
        assert [j["id"] for j in res["one_shot_jobs"]] == ["once"]

    def test_parse_errors_counted(self):
        _write_jobs([_job("once"), {"id": "broken", "name": "x"}])
        res = web._pending_decisions()
        assert res["one_shot_jobs_errors"] == 1
        # 解析不过的条目不挡其他条目
        assert [j["id"] for j in res["one_shot_jobs"]] == ["once"]

    def test_message_body_never_leaked(self):
        _write_jobs(
            [
                _job(
                    "once",
                    payload={
                        "kind": "agent_turn",
                        "message": "绝不外泄的正文",
                        "channel": "onebot",
                        "to": "group:123456789",
                    },
                )
            ]
        )
        res = web._pending_decisions()
        blob = json.dumps(res, ensure_ascii=False)
        assert "绝不外泄的正文" not in blob
        assert "payload" not in blob

    def test_overdue_flagged_and_sorted_first(self):
        _write_jobs([_job("future"), _job("past")])
        _write_state(
            {
                "past": {"nextRunAtMs": 1, "lastRunAtMs": 0, "lastStatus": "failed"},
                "future": {
                    "nextRunAtMs": 99999999999999,
                    "lastRunAtMs": 0,
                    "lastStatus": "",
                },
            }
        )
        res = web._pending_decisions()
        ids = [j["id"] for j in res["one_shot_jobs"]]
        assert ids == ["past", "future"]  # 过期的排前面
        assert res["one_shot_jobs"][0]["overdue"] is True
        assert res["one_shot_jobs"][0]["last_status"] == "failed"
        assert res["one_shot_jobs"][1]["overdue"] is False

    def test_no_file_is_empty_not_error(self):
        res = web._pending_decisions()
        assert res["one_shot_jobs"] == []
        assert res["one_shot_jobs_total"] == 0
        assert res["errors"] == []


class TestPendingIsolation:
    """单类取不到只进 errors，其余照常（总览铁律）。"""

    def test_outbox_failure_isolated(self, monkeypatch):
        _append_outbox(
            [
                {
                    "ts": 1.0,
                    "target": "g",
                    "outcome": "uncertain",
                    "ref": "",
                    "detail": "",
                },
            ]
        )

        def boom(limit=50):
            raise RuntimeError("disk gone")

        monkeypatch.setattr(outbox, "recent", boom)
        res = web._pending_decisions()
        assert res["outbox_uncertain"] == []
        assert res["errors"] == ["pending.outbox: RuntimeError"]
        # 另两类不受影响
        assert res["budget_blocked"]["targets"] == {}
        assert res["one_shot_jobs"] == []

    def test_budget_failure_isolated(self, monkeypatch):
        def boom():
            raise RuntimeError("no budget")

        monkeypatch.setattr(get_notify_budget(), "blocked_snapshot", boom)
        res = web._pending_decisions()
        assert res["budget_blocked"] is None
        assert res["errors"] == ["pending.budget_blocked: RuntimeError"]
        assert res["outbox_uncertain"] == []
        assert res["one_shot_jobs"] == []

    def test_jobs_failure_isolated(self, monkeypatch):
        def boom(path=None):
            raise RuntimeError("bad jobs")

        monkeypatch.setattr(jobs_mod, "load", boom)
        res = web._pending_decisions()
        assert res["one_shot_jobs"] == []
        assert res["errors"] == ["pending.one_shot_jobs: RuntimeError"]
        assert res["outbox_uncertain"] == []
        assert res["budget_blocked"]["targets"] == {}


class TestOverviewWiring:
    """build_overview 把 pending 块挂上去，且自己炸了也不影响其他块。"""

    @pytest.mark.asyncio
    async def test_pending_present_in_overview(self):
        ov = await web.build_overview()
        assert "pending" in ov
        pending = ov["pending"]
        assert isinstance(pending, dict)
        assert isinstance(pending["errors"], list)
        # 三类键一个都不能少
        for k in ("outbox_uncertain", "budget_blocked", "one_shot_jobs"):
            assert k in pending

    @pytest.mark.asyncio
    async def test_pending_failure_does_not_break_overview(self, monkeypatch):
        def boom():
            raise RuntimeError("nope")

        monkeypatch.setattr(web, "_pending_decisions", boom)
        ov = await web.build_overview()
        assert ov["pending"] is None
        assert any(e.startswith("pending:") for e in ov["errors"])
        # 其他块仍在
        assert "recent_events" in ov
        assert "budget" in ov


class TestJobRowShape:
    """面板行的字段形状：id/name/to/cron/tz/next_run_at_ms/last_status/overdue。"""

    def test_row_fields(self):
        _write_jobs([_job("once", name="起个名字")])
        res = web._pending_decisions()
        row = res["one_shot_jobs"][0]
        assert set(row) == {
            "id",
            "name",
            "to",
            "cron",
            "tz",
            "next_run_at_ms",
            "last_status",
            "overdue",
        }
        assert row["name"] == "起个名字"
        assert row["to"] == "group:123456789"
        assert row["cron"] == "0 9 * * *"
        assert row["tz"] == "Asia/Shanghai"

    def test_capped_at_ten(self):
        _write_jobs([_job(f"j{i:02d}") for i in range(15)])
        res = web._pending_decisions()
        assert res["one_shot_jobs_total"] == 15
        assert len(res["one_shot_jobs"]) == 10


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
