"""共享打扰预算（P3，2026-11-02）：agentcore/scheduler/notify_budget.py。

由来：四类主动投递原本各有一套上限且互不知晓（push 的
AGENT_PUSH_DAILY_CAP_PER_TARGET / nudge 的 max_daily_total / agent_turn 完全无上限 /
reminder 无全局闸），合起来能给同一目标一天发 20+ 条。本文件锁：

1. 记账四类、限流三类（GATED_KINDS）——remind 只记不拦
2. 全局上限是天花板，不替代各面自有细分上限（push/nudge 另算）
3. record 任何失败都不影响投递本身（记账是弱约束）
4. _prune 以当天为锚点的窗口：未来日键不得把当天的桶挤掉（曾让每日上限静默失效）
5. 状态文件创建即 0600（.part + os.replace 原子写），不做事后 chmod
6. 脏 env / 损坏状态文件都降级而不是抛异常

变异锚点（改坏实现必须红）：
1. check 对 remind 也返回 spent>=limit → remind 被全局预算吞掉，用例红
2. record 不再落盘 → 跨实例读不到计数，用例红
3. _prune 改回「排序取最后 N 天」→ 未来日键挤掉当天，用例红
4. open_part_0600 改 write_text + chmod → fchmod 未被调用，用例红
5. _env_int 脏值不回退 → 脏值直接抛/当 0，用例红
6. _load 损坏文件抛异常 → 构造即炸，用例红
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path

from agentcore.scheduler import notify_budget as nb


def _mk(tmp_path: Path, **kw) -> nb.NotifyBudget:
    return nb.NotifyBudget(tmp_path / "notify-budget.json", **kw)


class TestCounting:
    def test_record_and_read_back(self, tmp_path):
        b = _mk(tmp_path)
        now = time.time()
        b.record("group:1", "push", now)
        b.record("group:1", "push", now)
        b.record("group:1", "nudge", now)
        assert b.spent("group:1", now) == 3
        assert b.by_kind("group:1", now) == {"push": 2, "nudge": 1}
        assert b.by_kind("group:2", now) == {}

    def test_record_survives_new_instance(self, tmp_path):
        """跨进程/重启不清零——重启清零 = 上限失效（审查 P1 的同一条）。"""
        now = time.time()
        b1 = _mk(tmp_path)
        b1.record("group:1", "push", now)
        b2 = _mk(tmp_path)
        assert b2.spent("group:1", now) == 1
        assert b2.by_kind("group:1", now) == {"push": 1}

    def test_day_key_is_tz_aligned(self, tmp_path):
        """日界与 tz.day_key 同源：UTC 服务器上「每日上限」不得早 8 小时才重置。"""
        from agentcore import tz

        b = _mk(tmp_path)
        now = time.time()
        assert b.day_key(now) == tz.day_key(now)

    def test_snapshot_hides_nothing_but_targets(self, tmp_path):
        now = time.time()
        b = _mk(tmp_path, daily_limit=5)
        b.record("group:1", "push", now)
        b.record("group:1", "remind", now)
        snap = b.snapshot(now)
        assert snap["day"] == b.day_key(now)
        assert snap["limit"] == 5
        assert snap["targets"]["group:1"] == {"total": 2, "push": 1, "remind": 1}


class TestGating:
    def test_remind_is_recorded_but_never_gated(self, tmp_path):
        """remind 只记不拦：用户显式预约的提醒被全局预算吞掉是语义错误。"""
        b = _mk(tmp_path, daily_limit=1)
        now = time.time()
        b.record("group:1", "remind", now)
        assert b.spent("group:1", now) == 1
        assert b.allowed("group:1", now) is False  # 全局已用尽
        assert b.check("group:1", "remind", now) is True  # 但 remind 仍放行

    def test_gated_kind_blocked_when_limit_reached(self, tmp_path):
        b = _mk(tmp_path, daily_limit=2)
        now = time.time()
        for _ in range(2):
            b.record("group:1", "push", now)
        assert b.remaining("group:1", now) == 0
        assert b.check("group:1", "push", now) is False
        assert b.check("group:1", "nudge", now) is False
        assert b.check("group:1", "agent_turn", now) is False
        # 别的目标不受影响
        assert b.check("group:2", "push", now) is True

    def test_limit_zero_means_unlimited(self, tmp_path):
        b = _mk(tmp_path, daily_limit=0)
        now = time.time()
        for _ in range(20):
            b.record("group:1", "push", now)
        assert b.remaining("group:1", now) == -1
        assert b.allowed("group:1", now) is True
        assert b.check("group:1", "push", now) is True

    def test_unknown_kind_is_not_gated(self, tmp_path):
        """KINDS 之外的 type 只记账不限流（fail-open 于记账，fail-closed 于限流）。"""
        b = _mk(tmp_path, daily_limit=1)
        now = time.time()
        b.record("group:1", "push", now)
        assert b.check("group:1", "whatever", now) is True

    def test_custom_gated_kinds(self, tmp_path):
        b = _mk(tmp_path, daily_limit=1, gated_kinds={"nudge"})
        now = time.time()
        b.record("group:1", "push", now)
        assert b.check("group:1", "push", now) is True
        assert b.check("group:1", "nudge", now) is False


class TestPrune:
    def test_future_day_keys_do_not_evict_today(self, tmp_path):
        """回归：_prune 曾用「排序取最后 N 天」，未来日键把当天整桶挤掉。

        实测成因：test_nudge 注入的时间戳让 state 文件出现 11-04/11-06/11-16，
        占满 KEEP_DAYS=3，当天桶每次 record 后就被清掉 → push 每日上限静默失效。
        """
        path = tmp_path / "notify-budget.json"
        today = nb.NotifyBudget(path).day_key()
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "days": {
                        "2099-01-01": {"group:9": {"push": 3}},
                        "2099-01-02": {"group:9": {"push": 3}},
                        "2099-01-03": {"group:9": {"push": 3}},
                    },
                }
            ),
            encoding="utf-8",
        )
        b = nb.NotifyBudget(path)
        now = time.time()
        b.record("group:1", "push", now)
        assert b.spent("group:1", now) == 1, "当天计数被未来日键挤掉了"
        assert set(b._days) == {today}

    def test_old_days_are_dropped(self, tmp_path):
        path = tmp_path / "notify-budget.json"
        b0 = nb.NotifyBudget(path)
        old = b0.day_key(time.time() - 30 * 86400)
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "days": {old: {"group:1": {"push": 1}}},
                }
            ),
            encoding="utf-8",
        )
        b = nb.NotifyBudget(path)
        now = time.time()
        assert b.spent("group:1", now) == 0, "过期日键不应计入当天"
        assert old not in b._days, "过期日键应在 _load 时就被清理"
        b.record("group:1", "push", now)
        assert set(b._days) == {b.day_key(now)}

    def test_dirty_day_key_does_not_explode(self, tmp_path):
        """脏日键（手改 state 文件）只封顶当天目标数，不抛异常。"""
        path = tmp_path / "notify-budget.json"
        path.write_text(
            json.dumps(
                {"version": 1, "days": {"not-a-date": {"group:1": {"push": 1}}}}
            ),
            encoding="utf-8",
        )
        b = nb.NotifyBudget(path)
        now = time.time()
        b.record("group:1", "nudge", now)
        assert b.spent("group:1", now) == 1


class TestStateFile:
    def test_atomic_and_private(self, tmp_path):
        b = _mk(tmp_path)
        b.record("group:1", "push", time.time())
        part = Path(f"{b.path}.part")
        assert b.path.exists()
        assert not part.exists(), "原子替换后无残留"
        raw = json.loads(b.path.read_text(encoding="utf-8"))
        assert raw["version"] == 1
        assert isinstance(raw["days"], dict)

    def test_part_created_strict_no_post_chmod(self, tmp_path, monkeypatch):
        """C4（REVIEW 2026-11-02）：.part 创建即 0600，不得 write_text 后补 chmod。

        Windows 上 st_mode 恒 0o666（落盘断言在本机不成立），所以锁行为：
        fchmod 以 0o600 被调用，且 .part 上不出现 os.chmod。
        """
        seen: list[int] = []
        real_fchmod = os.fchmod

        def _spy_fchmod(fd, mode):
            seen.append(mode)
            return real_fchmod(fd, mode)

        monkeypatch.setattr(os, "fchmod", _spy_fchmod)
        chmod_calls: list[str] = []
        real_chmod = os.chmod

        def _spy_chmod(path, mode):
            chmod_calls.append(str(path))
            return real_chmod(path, mode)

        monkeypatch.setattr(os, "chmod", _spy_chmod)
        b = _mk(tmp_path)
        b.record("group:9", "push", time.time())
        assert seen == [0o600]
        assert all(not p.endswith(".part") for p in chmod_calls)

    def test_corrupt_state_file_falls_back_to_empty(self, tmp_path, caplog):
        path = tmp_path / "notify-budget.json"
        path.write_text("{ not json", encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            b = nb.NotifyBudget(path)
        assert b.spent("group:1", time.time()) == 0
        assert "notify_budget: 状态文件损坏" in caplog.text

    def test_non_dict_state_file_is_ignored(self, tmp_path):
        path = tmp_path / "notify-budget.json"
        path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        assert nb.NotifyBudget(path).spent("group:1", time.time()) == 0

    def test_env_dirty_value_warns_and_falls_back(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("AGENT_NOTIFY_DAILY_LIMIT", "abc")
        with caplog.at_level(logging.WARNING):
            b = nb.NotifyBudget(tmp_path / "x.json")
        assert b.daily_limit == 8
        assert "AGENT_NOTIFY_DAILY_LIMIT" in caplog.text

    def test_env_limit_is_read(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENT_NOTIFY_DAILY_LIMIT", "3")
        assert nb.NotifyBudget(tmp_path / "x.json").daily_limit == 3

    def test_explicit_path_wins_over_env(self, tmp_path, monkeypatch):
        """显式形参赢过部署 env：调用方给了路径就是要这个路径。"""
        env_path = tmp_path / "from-env.json"
        monkeypatch.setenv("AGENT_NOTIFY_BUDGET_FILE", str(env_path))
        b = nb.NotifyBudget(tmp_path / "explicit.json")
        assert b.path == tmp_path / "explicit.json"

    def test_record_failure_never_breaks_delivery(self, tmp_path, monkeypatch, caplog):
        """记账失败绝不能让一次已经成功的投递变成异常。"""
        b = _mk(tmp_path)

        def _boom(*a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr(b, "_save", _boom)
        with caplog.at_level(logging.WARNING):
            b.record("group:1", "push", time.time())
        assert "notify_budget: 记账失败" in caplog.text


class TestSingleton:
    def test_get_notify_budget_is_lazy_and_shared(self, monkeypatch, tmp_path):
        import agentcore.scheduler.notify_budget as mod

        monkeypatch.setattr(mod, "_default", None)
        monkeypatch.setenv("AGENT_NOTIFY_BUDGET_FILE", str(tmp_path / "s.json"))
        a = mod.get_notify_budget()
        assert a is mod.get_notify_budget()
        assert a.path == tmp_path / "s.json"

    def test_open_part_0600_truncates_stale_part(self, tmp_path):
        """C1 根因修正：上次崩溃留下的陈旧 .part 必须被截断，不能接着 append。"""
        part = tmp_path / "x.json.part"
        part.write_bytes(b"x" * 4096)
        with nb.open_part_0600(part) as fh:
            fh.write(b"{}")
        assert part.read_bytes() == b"{}"


class TestEnvDefaults:
    def test_kinds_and_gating(self):
        assert nb.GATED_KINDS == {"push", "nudge", "agent_turn"}
        assert "remind" in nb.KINDS
        assert "remind" not in nb.GATED_KINDS
        assert nb.KEEP_DAYS >= 1
        assert nb.MAX_TARGETS_PER_DAY >= 1


class TestBlocked:
    """P4 的 blocked 段：被全局闸拦下的计数。与 days 同构但**不参与 spent**。

    为什么单独一段：拦下 = 没有送达。混进 days 会让 spent 虚高、把上限提前用尽
    （自己占自己额度）；而面板要回答的正是「被拦下了几次」这个事实，只从
    snapshot 推断「今日已达上限的目标」是猜不是账。
    """

    def test_record_blocked_and_read_back(self, tmp_path):
        b = _mk(tmp_path)
        b.record_blocked("group:1", "agent_turn")
        b.record_blocked("group:1", "agent_turn")
        b.record_blocked("group:2", "nudge")
        assert b.blocked_by_kind("group:1") == {"agent_turn": 2}
        assert b.blocked_by_kind("group:2") == {"nudge": 1}

    def test_blocked_is_not_spent(self, tmp_path):
        b = _mk(tmp_path)
        b.record_blocked("group:1", "push")
        assert b.spent("group:1") == 0
        assert b.by_kind("group:1") == {}
        assert b.allowed("group:1") is True

    def test_blocked_survives_new_instance(self, tmp_path):
        b = _mk(tmp_path)
        b.record_blocked("group:1", "push")
        b2 = _mk(tmp_path)
        assert b2.blocked_by_kind("group:1") == {"push": 1}

    def test_blocked_snapshot_sorted_by_total_desc(self, tmp_path):
        b = _mk(tmp_path)
        b.record_blocked("group:2", "nudge")
        b.record_blocked("group:1", "agent_turn")
        b.record_blocked("group:1", "agent_turn")
        snap = b.blocked_snapshot()
        assert list(snap["targets"]) == ["group:1", "group:2"]
        assert snap["targets"]["group:1"] == {"total": 2, "agent_turn": 2}
        assert snap["day"] and snap["limit"]

    def test_prune_clears_both_buckets(self, tmp_path):
        """blocked 桶和 days 桶同一个清理窗口——否则坏手挑容量大的那半边撑爆文件。"""
        path = tmp_path / "notify-budget.json"
        today = nb.NotifyBudget(path).day_key()
        # 两个桶都各放一个过期日键（record/record_blocked 收的时间戳，不是日键）
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "days": {"2026-01-01": {"group:1": {"push": 1}}},
                    "blocked": {"2026-01-01": {"group:1": {"push": 1}}},
                }
            ),
            encoding="utf-8",
        )
        b = nb.NotifyBudget(path)
        now = time.time()
        b.record("group:1", "push", now)
        b.record_blocked("group:1", "push", now)
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert set(raw["days"]) == {today}
        assert set(raw["blocked"]) == {today}
        # 过期键在两个桶里都被清掉
        assert "2026-01-01" not in raw["days"]
        assert "2026-01-01" not in raw["blocked"]

    def test_future_day_keys_do_not_evict_today_blocked(self, tmp_path):
        """与 days 桶同一条不变量：未来日键不得把当天桶挤掉（P3 踩过的坑）。"""
        path = tmp_path / "notify-budget.json"
        today = nb.NotifyBudget(path).day_key()
        path.write_text(
            json.dumps(
                {"version": 1, "blocked": {"2099-01-01": {"group:9": {"push": 3}}}}
            ),
            encoding="utf-8",
        )
        b = nb.NotifyBudget(path)
        now = time.time()
        b.record_blocked("group:2", "push", now)
        assert b.blocked_by_kind("group:2", now) == {"push": 1}, (
            "当天桶被未来日键挤掉了"
        )
        assert set(b._blocked) == {today}

    def test_record_blocked_failure_never_explodes(self, tmp_path, monkeypatch, caplog):
        b = _mk(tmp_path)

        def boom(day):
            raise OSError("disk gone")

        monkeypatch.setattr(b, "_save", boom)
        with caplog.at_level(logging.WARNING):
            b.record_blocked("group:1", "push")
        # 记账是弱约束：失败只告警，绝不抛给闸门
        assert "notify_budget: 被拦计数失败" in caplog.text

    def test_dirty_state_file_falls_back_to_empty_blocked(self, tmp_path):
        Path(_mk(tmp_path).path).parent.mkdir(parents=True, exist_ok=True)
        p = tmp_path / "notify-budget.json"
        p.write_text('{"days": {}, "blocked": "不是对象"}', encoding="utf-8")
        b = _mk(tmp_path)
        assert b.blocked_snapshot()["targets"] == {}
        assert b.snapshot()["targets"] == {}


class TestReservation:
    """M13：check→send→record 的 TOCTOU 窗口，用「判定 + 预占」关闭。

    旧流程三步之间隔着整个 ``await sink.send_once(...)``；push / nudge /
    agent_turn 是三个独立 interval job、同一事件循环并发，同一目标多路同时到点时
    两边都看到 ``spent=limit-1`` → 双双放行 → 超限。
    """

    @staticmethod
    async def _flow(budget, target: str, kind: str, outcome: str) -> str:
        """一个闸门的完整生命周期：reserve →（await 投递）→ 结算。"""
        ok, handle = budget.reserve(target, kind)
        if not ok:
            return "blocked"
        await asyncio.sleep(0.02)  # 模拟 send_once 的 await 窗口
        if outcome == "sent":
            if handle is not None:
                handle.commit()
            return "sent"
        if handle is not None:
            handle.rollback()
        return outcome

    def test_concurrent_reserves_cannot_overshoot(self, tmp_path):
        """并发放行数不得超过上限（旧实现在此超发）。"""
        b = _mk(tmp_path, daily_limit=2)
        results: list[str] = []

        async def _one(i: int) -> None:
            results.append(await self._flow(b, "g:1", "push", "sent"))

        threads = [
            threading.Thread(target=lambda i=i: asyncio.run(_one(i))) for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        sent = results.count("sent")
        assert sent == 2, f"上限 2 只准放行 2 条，实际放行 {sent}（{results}）"
        assert b.spent("g:1") == 2, "放行数必须与账面一致"

    def test_unlimited_and_ungated_need_no_settlement(self, tmp_path):
        """不限（limit=0）与非 gated 类型返回 (True, None)——没有可占的额度。"""
        b = _mk(tmp_path, daily_limit=0)
        assert b.reserve("g:1", "push") == (True, None)
        b2 = _mk(tmp_path, daily_limit=1)
        assert b2.reserve("g:1", "remind") == (True, None)

    def test_rollback_returns_the_slot(self, tmp_path):
        """没送达就归还：失败/uncertain 不得让预算凭空少一格。"""
        b = _mk(tmp_path, daily_limit=1)
        ok, handle = b.reserve("g:1", "push")
        assert ok and handle is not None
        assert b.spent("g:1") == 1, "预占即计数"
        handle.rollback()
        assert b.spent("g:1") == 0, "归还不留残数"
        assert b.remaining("g:1") == 1
        handle.rollback()  # 幂等
        assert b.spent("g:1") == 0

    def test_rollback_cleans_empty_buckets(self, tmp_path):
        """归还到 0 时把空桶清掉：否则 _prune 的封顶按目标数算，空桶白占额度。"""
        b = _mk(tmp_path, daily_limit=5)
        ok, handle = b.reserve("g:1", "push")
        handle.rollback()
        raw = json.loads((tmp_path / "notify-budget.json").read_text(encoding="utf-8"))
        day = next(iter(raw["days"]))
        assert raw["days"][day] == {}, f"空目标桶必须清掉，实际 {raw['days'][day]}"

    def test_reserve_survives_new_instance(self, tmp_path):
        """预占也落盘：重启后不能把在飞投递的额度吐出来（双记）。"""
        path = tmp_path / "notify-budget.json"
        b = nb.NotifyBudget(path, daily_limit=2)
        ok, _ = b.reserve("g:1", "push")
        assert ok
        b2 = nb.NotifyBudget(path, daily_limit=2)
        assert b2.spent("g:1") == 1, "预占必须持久化"

    def test_blocked_reserve_leaves_no_trace(self, tmp_path):
        """被拦下的 reserve 不占额度（调用方另行 record_blocked 留痕）。"""
        b = _mk(tmp_path, daily_limit=1)
        assert b.reserve("g:1", "push")[0] is True
        assert b.reserve("g:1", "push") == (False, None)
        assert b.spent("g:1") == 1
        assert b.by_kind("g:1").get("push") == 1
