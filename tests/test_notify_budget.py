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

import json
import logging
import os
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
