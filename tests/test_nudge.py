"""主动联系（nudge）主题测试。

来源：2026-10-23「让 bot 主动联系我」方案落地（P1 骨架）。覆盖四类东西：
配置解析与热读、时间守卫（静默/晚睡/活跃）、未回复退避与配额、投递三态与
历史写回。

写用例纪律（AGENTS.md §2.7）：每条断言都要在「实现被改坏」时失败。特别是
下面几个回归点，都是方案评审里点名的坑：

- 静默时段跨零点（23:30→10:00）——按 start<end 判断会把它算成"全天不静默"
- 晚睡保护的 6 小时落点可能仍落在静默时段内——一次性判断会漏
- 暂停/止损用 ``schedule_mark_fired(None)`` 会把行停用，用户再说话也醒不过来
- 主动消息不写回历史 → 用户回复时接不上上文
- 投递 uncertain 时重发 = 同一句话发两遍
"""

import datetime as dt
import json
import logging
import random
from pathlib import Path

import pytest

from agentcore.memory.store import InMemoryMemoryStore
from agentcore.scheduler import nudge as nudge_mod
from agentcore.scheduler.nudge import (
    ACTION_NUDGE,
    OWNER,
    NudgeConfig,
    NudgeService,
    after_late_night,
    clip_at_sentence,
    earliest_allowed,
    in_quiet,
    is_busy_reply,
    load_nudge_config,
    nickname_from_facts,
    quiet_end_ts,
    save_nudge_config,
)

TARGET = "private:2224513919"
USER_ID = "2224513919"


# ---------------------------------------------------------------------------
# 测试辅助


class _FakeSink:
    """三态投递桩：记录每次投递，按脚本返回 sent/failed/uncertain。"""

    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [])
        self.sent: list[tuple[str, str]] = []

    async def send_once(self, target: str, message: str) -> str:
        self.sent.append((target, message))
        if self.outcomes:
            return self.outcomes.pop(0)
        return "sent"


class _MinRng:
    """确定性随机：``uniform`` 恒取下限——用来断言"最早 6 小时"这类边界。"""

    def uniform(self, a, b):
        return a


class _FakeEngine:
    """只实现 generate_proactive：记录入参，按脚本返回正文。"""

    def __init__(self, texts=None):
        self.texts = list(texts or ["在改那份备份脚本吗"])
        self.calls: list[dict] = []

    async def generate_proactive(
        self,
        context,
        *,
        instruction,
        extra_data="",
        recent_texts=None,
        max_tokens=0,
    ):
        self.calls.append(
            {
                "context": context,
                "instruction": instruction,
                "extra_data": extra_data,
                "recent_texts": list(recent_texts or []),
                "max_tokens": max_tokens,
            }
        )
        if len(self.texts) > 1:
            return self.texts.pop(0)
        return self.texts[0] if self.texts else ""


def _blocked_budget():
    class _B:
        def chat_blocked(self):
            return True, "（今日 LLM 预算已用完，服务明日自动恢复。）"

    return _B()


def _at(hh: int, mm: int = 0, day_offset: int = 0) -> float:
    """本地时刻 → epoch 秒（基准=今天，可偏移天数）。"""
    base = nudge_mod.tz.now().replace(hour=hh, minute=mm, second=0, microsecond=0)
    return (base + dt.timedelta(days=day_offset)).timestamp()


def _cfg(**over) -> NudgeConfig:
    cfg = NudgeConfig(enabled=True, targets=[TARGET])
    for key, value in over.items():
        setattr(cfg, key, value)
    return cfg


async def _service(
    store, *, sink=None, engine=None, cfg=None, on_alert=None, seed=7, rng=None
):
    """默认用 _MinRng：first delay 恒为 6h，时序断言才可复现。"""
    sink = sink or _FakeSink()
    engine = engine or _FakeEngine()
    svc = NudgeService(
        store,
        sink,
        engine,
        config=cfg or _cfg(),
        config_path="/nonexistent/nudge.json",
        on_alert=on_alert,
        rng=rng if rng is not None else _MinRng(),
    )
    await svc.register_targets()
    return svc, sink, engine


async def _row(store, target: str = TARGET) -> dict:
    rows = await store.schedule_list(OWNER, include_disabled=True)
    return next(r for r in rows if r["target"] == target)


async def _user_said(store, text: str, ts: float) -> None:
    session = await store.resolve_session(USER_ID, None)
    await store.append_message(session, "user", text)
    # append_message 用真实时钟记 ts，这里按用例需要改写成指定时刻
    session_msgs = store.messages[session]
    session_msgs[-1]["ts"] = ts


# ---------------------------------------------------------------------------
# 纯函数：时间守卫


class TestQuietWindow:
    def test_cross_midnight_window(self):
        cfg = _cfg()
        assert in_quiet(cfg, _at(23, 45)) is True
        assert in_quiet(cfg, _at(0, 30)) is True
        assert in_quiet(cfg, _at(9, 59)) is True
        assert in_quiet(cfg, _at(23, 29)) is False
        assert in_quiet(cfg, _at(10, 0)) is False
        assert in_quiet(cfg, _at(15, 0)) is False

    def test_same_day_window(self):
        cfg = _cfg(quiet_start="01:00", quiet_end="07:00")
        assert in_quiet(cfg, _at(2, 0)) is True
        assert in_quiet(cfg, _at(0, 59)) is False
        assert in_quiet(cfg, _at(7, 0)) is False

    def test_quiet_end_skips_to_next_morning(self):
        cfg = _cfg()
        # 00:30 → 当天 10:00；23:45 → 次日 10:00
        assert quiet_end_ts(cfg, _at(0, 30)) == pytest.approx(_at(10, 0))
        assert quiet_end_ts(cfg, _at(23, 45)) == pytest.approx(_at(10, 0, 1))

    def test_after_late_night_is_wall_clock(self):
        cfg = _cfg()
        assert after_late_night(cfg, _at(1, 0)) is True
        assert after_late_night(cfg, _at(0, 59)) is False
        assert after_late_night(cfg, _at(12, 0)) is False


class TestEarliestAllowed:
    def test_inside_quiet_is_pushed_out(self):
        cfg = _cfg()
        assert earliest_allowed(cfg, _at(2, 0), None) == pytest.approx(_at(10, 0))

    def test_late_night_floor(self):
        cfg = _cfg()
        # 01:30 说话 → floor 07:30 仍在静默内，被再推到 10:00
        assert earliest_allowed(cfg, _at(2, 0), _at(1, 30)) == pytest.approx(_at(10, 0))
        # 已经过了 floor 的计划时刻不被提前
        assert earliest_allowed(cfg, _at(12, 0), _at(1, 30)) == pytest.approx(
            _at(12, 0)
        )
        # 05:00 说话 → floor 11:00，已出静默
        assert earliest_allowed(cfg, _at(9, 0), _at(5, 0)) == pytest.approx(_at(11, 0))
        # 04:04 说话 → floor 10:04，正好压在静默结束之后
        assert earliest_allowed(cfg, _at(9, 0), _at(4, 4)) == pytest.approx(_at(10, 4))

    def test_no_constraints_keeps_time(self):
        cfg = _cfg()
        assert earliest_allowed(cfg, _at(15, 0), _at(9, 0)) == pytest.approx(_at(15, 0))


class TestClipAndNickname:
    def test_clip_at_sentence_boundary(self):
        assert clip_at_sentence("早上好呀。今天也要顺利", 5) == "早上好呀。"
        assert clip_at_sentence("没有标点的一段长文字", 6) == "没有标点的一"
        assert clip_at_sentence("短句", 60) == "短句"

    def test_nickname_only_from_first_person_facts(self):
        """``list_facts`` 的契约是 ``list[str]``（2026-10-23 线上实测的坑）。"""
        # 第三人称事实（群成员夏祭）不能当成用户本人
        assert nickname_from_facts(["用户提及的夏祭是其所在群的群成员"]) == ""
        assert nickname_from_facts(["用户昵称为夏祭"]) == "夏祭"
        assert nickname_from_facts(["用户希望被称呼为老板"]) == "老板"
        assert nickname_from_facts(["用户称呼自己为阿祭"]) == "阿祭"
        # 最新优先：列表顺序即优先级，取第一个命中的
        assert nickname_from_facts(["用户昵称为夏祭", "用户昵称为老板"]) == "夏祭"
        assert nickname_from_facts([]) == ""
        assert nickname_from_facts(None) == ""

    def test_nickname_tolerates_dict_shape(self):
        """dict 形态也认：调用方换成 recall_facts 的形状时不该炸。"""
        assert nickname_from_facts([{"content": "用户昵称为夏祭"}]) == "夏祭"
        assert nickname_from_facts([{"content": ""}, {"content": None}]) == ""

    def test_nickname_strips_control_chars(self):
        assert nickname_from_facts(["用户昵称为夏\x01祭"]) == "夏祭"

    def test_busy_reply_is_conservative(self):
        assert is_busy_reply("在忙") is True
        assert is_busy_reply("别烦我") is True
        assert is_busy_reply("我不忙") is False
        assert is_busy_reply("刚把备份脚本写完") is False
        assert is_busy_reply("") is False


# ---------------------------------------------------------------------------
# 配置


class TestConfig:
    def test_defaults_are_safe(self, tmp_path):
        cfg = load_nudge_config(tmp_path / "missing.json", env={})
        assert cfg.enabled is False, "默认关闭：未确认语气前不许主动说话"
        assert (cfg.quiet_start, cfg.quiet_end) == ("23:30", "10:00")
        assert cfg.min_idle_seconds == 30 * 60
        assert cfg.first_min_seconds == 6 * 3600
        assert cfg.first_max_seconds == 24 * 3600
        assert cfg.second_delay_seconds == 48 * 3600
        assert cfg.goodbye_after_days == 14
        assert cfg.max_unanswered == 3
        assert cfg.targets == []

    def test_file_overrides_and_env_kill_switch(self, tmp_path):
        path = tmp_path / "nudge.json"
        path.write_text(
            json.dumps({"enabled": True, "targets": [TARGET], "max_chars": 80}),
            encoding="utf-8",
        )
        cfg = load_nudge_config(path, env={})
        assert cfg.enabled is True and cfg.targets == [TARGET] and cfg.max_chars == 80
        # env 总开关优先于文件
        cfg = load_nudge_config(path, env={"AGENT_NUDGE_ENABLED": "0"})
        assert cfg.enabled is False
        cfg = load_nudge_config(path, env={"AGENT_NUDGE_ENABLED": "true"})
        assert cfg.enabled is True

    def test_broken_file_falls_back_without_raising(self, tmp_path, caplog):
        path = tmp_path / "nudge.json"
        path.write_text("{ not json", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="agentcore.scheduler.nudge"):
            cfg = load_nudge_config(path, env={})
        assert cfg.enabled is False
        assert "不是合法 JSON" in caplog.text

    def test_targets_are_validated_and_deduped(self, tmp_path, caplog):
        path = tmp_path / "nudge.json"
        path.write_text(
            json.dumps(
                {
                    "targets": [
                        TARGET,
                        TARGET,
                        "group:100",  # v1 只做私聊
                        "private:abc",
                        "private:１２３",  # 全角数字：parse_target 必须拒
                    ]
                }
            ),
            encoding="utf-8",
        )
        with caplog.at_level(logging.WARNING, logger="agentcore.scheduler.nudge"):
            cfg = load_nudge_config(path, env={})
        assert cfg.targets == [TARGET]
        assert "group:100" in caplog.text

    def test_unknown_keys_and_bad_values_warn(self, tmp_path, caplog):
        path = tmp_path / "nudge.json"
        path.write_text(
            json.dumps({"quiet_start": "25:00", "min_idle_seconds": "很多"}),
            encoding="utf-8",
        )
        with caplog.at_level(logging.WARNING, logger="agentcore.scheduler.nudge"):
            cfg = load_nudge_config(path, env={})
        assert cfg.quiet_start == "23:30", "非法时刻回退默认"
        assert cfg.min_idle_seconds == 30 * 60

    def test_env_tick_overrides_file(self, tmp_path):
        path = tmp_path / "nudge.json"
        path.write_text(json.dumps({"tick_seconds": 900}), encoding="utf-8")
        assert load_nudge_config(path, env={}).tick_seconds == 900
        assert (
            load_nudge_config(path, env={"AGENT_NUDGE_TICK": "120"}).tick_seconds == 120
        )
        # 低于下限要钳制而不是静默接受
        assert load_nudge_config(path, env={"AGENT_NUDGE_TICK": "1"}).tick_seconds == 30

    def test_save_is_atomic_and_private(self, tmp_path):
        path = tmp_path / "sub" / "nudge.json"
        assert save_nudge_config(path, _cfg(max_chars=40)) is True
        assert path.stat().st_mode & 0o777 == 0o600
        assert json.loads(path.read_text(encoding="utf-8"))["max_chars"] == 40


# ---------------------------------------------------------------------------
# 目标行管理


class TestRegisterTargets:
    @pytest.mark.asyncio
    async def test_add_remove_and_no_revive(self, store):
        svc, _sink, _eng = await _service(store)
        row = await _row(store)
        assert row["action"] == ACTION_NUDGE and row["user_id"] == OWNER
        assert row["enabled"] is True and row["next_run"] is None

        # 配置里删掉目标 → 停用残留行（否则照旧投递）
        svc._config = _cfg(targets=[])
        stats = await svc.register_targets()
        assert stats["removed"] == 1
        assert (await _row(store))["enabled"] is False

        # 行已停用且配置没变 → 不复活
        svc._config = _cfg()
        stats = await svc.register_targets()
        assert stats["added"] == 0 and stats["kept"] == 1
        assert (await _row(store))["enabled"] is False


# ---------------------------------------------------------------------------
# 时间守卫与首条时机


class TestTiming:
    @pytest.mark.asyncio
    async def test_first_nudge_waits_six_to_twenty_four_hours(self, store):
        last = _at(9, 0)
        await _user_said(store, "云崽起床了", last)
        svc, sink, _eng = await _service(store, rng=random.Random(7))

        out = await svc.tick(now=last + 3600)
        assert out["sent"] == 0 and sink.sent == []
        row = await _row(store)
        assert row["next_run"] is not None
        assert last + 6 * 3600 <= row["next_run"] <= last + 24 * 3600

        # 到点才发
        await svc.tick(now=row["next_run"] + 1)
        assert len(sink.sent) == 1
        assert sink.sent[0][0] == TARGET

    @pytest.mark.asyncio
    async def test_quiet_hours_suppress_and_reschedule(self, store):
        # 18:00 说话 → 6h 后是 00:00（静默）→ 必须推到次日 10:00
        await _user_said(store, "晚上好", _at(18, 0))
        svc, sink, _eng = await _service(store)
        await svc.tick(now=_at(0, 30, 1))
        assert sink.sent == []
        row = await _row(store)
        assert row["next_run"] == pytest.approx(_at(10, 0, 1))

        await svc.tick(now=_at(9, 59, 1))
        assert sink.sent == []
        await svc.tick(now=_at(10, 0, 1))
        assert len(sink.sent) == 1

    @pytest.mark.asyncio
    async def test_late_night_guard(self, store):
        # 04:04 说话 → 最早 10:04，而不是 10:00
        await _user_said(store, "还在吗", _at(4, 4))
        svc, sink, _eng = await _service(store)
        await svc.tick(now=_at(10, 0))
        assert sink.sent == []
        await svc.tick(now=_at(10, 3))
        assert sink.sent == []
        await svc.tick(now=_at(10, 4))
        assert len(sink.sent) == 1

    @pytest.mark.asyncio
    async def test_active_chat_blocks_even_when_due(self, store):
        await _user_said(store, "刚说完", _at(12, 0))
        svc, sink, _eng = await _service(store)
        await svc.tick(now=_at(12, 5))
        # 手动把下次时间拨到当下，模拟"计划已到点但你 5 分钟前还在说"
        row = await _row(store)
        await store.schedule_mark_fired(row["id"], _at(12, 5))
        out = await svc.tick(now=_at(12, 10))
        assert sink.sent == []
        assert out["skipped"] == 1
        # 31 分钟后放行
        await store.schedule_mark_fired(row["id"], _at(12, 31))
        await svc.tick(now=_at(12, 31))
        assert len(sink.sent) == 1

    @pytest.mark.asyncio
    async def test_paused_and_busy_do_not_disable_the_row(self, store):
        """暂停/止损不能把行停用——否则用户再说话也醒不过来（回归点）。"""
        await _user_said(store, "在忙", _at(12, 0))
        svc, sink, _eng = await _service(store)
        await svc.note_user_message(USER_ID, "在忙，别烦", ts=_at(12, 1))
        row = await _row(store)
        assert row["params"]["pause_until"] == pytest.approx(_at(12, 1) + 24 * 3600)
        assert row["enabled"] is True, "暂停不是停用"

        await svc.tick(now=_at(13, 0))
        assert sink.sent == []
        assert (await _row(store))["enabled"] is True

        # 暂停过期后放行（把计划时刻拨到暂停结束之后）
        row = await _row(store)
        wake = row["params"]["pause_until"]
        await store.schedule_mark_fired(row["id"], wake)
        await svc.tick(now=wake + 1)
        assert len(sink.sent) == 1


# ---------------------------------------------------------------------------
# 未回复退避（核心节奏）


class TestUnansweredBackoff:
    @pytest.mark.asyncio
    async def test_three_strikes_then_goodbye_then_stop(self, store):
        first = _at(9, 0)
        await _user_said(store, "早", first)
        svc, sink, _eng = await _service(store)

        # 第 1 条
        await svc.tick(now=first + 60)
        row = await _row(store)
        await store.schedule_mark_fired(row["id"], row["next_run"])
        await svc.tick(now=row["next_run"] + 1)
        assert len(sink.sent) == 1
        state = (await _row(store))["params"]
        assert state["unanswered"] == 1
        assert state["last_kind"] == "normal"
        # 第二条排在 +48h
        assert state["last_sent_at"] + 48 * 3600 == pytest.approx(
            (await _row(store))["next_run"], abs=2
        )

        # 第 2 条（+48h）
        row = await _row(store)
        await store.schedule_mark_fired(row["id"], row["next_run"])
        await svc.tick(now=row["next_run"] + 1)
        assert len(sink.sent) == 2
        state = (await _row(store))["params"]
        assert state["unanswered"] == 2
        # 第三条 = 第 14 天的告别，之前不发（09:00 在静默内，会被推到 10:00）
        goodbye_at = first + 14 * 86400
        row = await _row(store)
        assert goodbye_at <= row["next_run"] <= goodbye_at + 2 * 3600

        await svc.tick(now=row["next_run"] - 60)
        assert len(sink.sent) == 2, "没到第 14 天不发第三条"
        await svc.tick(now=row["next_run"])
        assert len(sink.sent) == 3
        state = (await _row(store))["params"]
        assert state["last_kind"] == "goodbye"
        assert state["unanswered"] == 3
        assert state["paused_until_user"] is True

        # 之后再也不发（等你先开口）
        await svc.tick(now=row["next_run"] + 86400)
        assert len(sink.sent) == 3

    @pytest.mark.asyncio
    async def test_reply_resets_everything(self, store):
        first = _at(9, 0)
        await _user_said(store, "早", first)
        svc, sink, _eng = await _service(store)
        await svc.tick(now=first + 60)
        row = await _row(store)
        await store.schedule_mark_fired(row["id"], row["next_run"])
        await svc.tick(now=row["next_run"] + 1)
        assert len(sink.sent) == 1

        # 你回话了 → 计数清零、倒计时从这条消息重排
        replied = _at(20, 0)
        await _user_said(store, "刚在忙那个", replied)
        out = await svc.tick(now=replied + 60)
        assert out["sent"] == 0
        state = (await _row(store))["params"]
        assert state["unanswered"] == 0 and state["paused_until_user"] is False
        row = await _row(store)
        assert replied + 6 * 3600 <= row["next_run"] <= replied + 24 * 3600

    @pytest.mark.asyncio
    async def test_daily_cap_skips_but_keeps_schedule(self, store):
        first = _at(9, 0)
        await _user_said(store, "早", first)
        svc, sink, _eng = await _service(store)
        await svc.tick(now=first + 60)
        row = await _row(store)
        await store.schedule_mark_fired(row["id"], row["next_run"])
        await svc.tick(now=row["next_run"] + 1)
        assert len(sink.sent) == 1

        # 你回话了（计数清零），但今天的名额已用满 → 到点也跳过
        await _user_said(store, "在的", _at(16, 0))
        out = await svc.tick(now=_at(16, 1))
        assert out["sent"] == 0 and len(sink.sent) == 1
        row = await _row(store)
        assert row["next_run"] == pytest.approx(_at(22, 0)), "重置后重排 6h"

        out = await svc.tick(now=_at(22, 0))
        assert len(sink.sent) == 1, "名额已用满，不再发"
        assert out["skipped"] == 1
        assert (await _row(store))["params"]["last_skip_reason"] == "daily_cap"
        assert (await _row(store))["enabled"] is True
        # 配满是按天的条件：顺延到次日 00:05，而不是每小时白跑一次
        row = await _row(store)
        assert row["next_run"] == pytest.approx(_at(0, 5, 1), abs=120)

    @pytest.mark.asyncio
    async def test_daily_cap_zero_means_unlimited(self, store):
        """daily_cap_normal=0 = 不限（与 push 的封顶口径一致）。"""
        first = _at(9, 0)
        await _user_said(store, "早", first)
        svc, sink, _eng = await _service(store, cfg=_cfg(daily_cap_normal=0))
        await svc.tick(now=first + 60)
        row = await _row(store)
        await store.schedule_mark_fired(row["id"], row["next_run"])
        await svc.tick(now=row["next_run"] + 1)
        assert len(sink.sent) == 1

        # 你回话了 → 重置后当天还能再发
        await _user_said(store, "在的", _at(16, 0))
        await svc.tick(now=_at(16, 1))
        row = await _row(store)
        await store.schedule_mark_fired(row["id"], row["next_run"])
        await svc.tick(now=row["next_run"] + 1)
        assert len(sink.sent) == 2

    @pytest.mark.asyncio
    async def test_budget_gate_skips_without_disabling(self, store, monkeypatch):
        first = _at(9, 0)
        await _user_said(store, "早", first)
        svc, sink, _eng = await _service(store)
        await svc.tick(now=first + 60)
        row = await _row(store)
        await store.schedule_mark_fired(row["id"], row["next_run"])
        monkeypatch.setattr(nudge_mod, "get_budget", _blocked_budget)
        out = await svc.tick(now=row["next_run"] + 1)
        assert sink.sent == []
        assert out["skipped"] == 1
        assert (await _row(store))["params"]["last_skip_reason"] == "budget"
        assert (await _row(store))["enabled"] is True

    @pytest.mark.asyncio
    async def test_no_session_target_is_disabled_with_alert(self, store):
        alerts = []
        svc, sink, _eng = await _service(store, on_alert=alerts.append)
        out = await svc.tick(now=_at(12, 0))
        assert sink.sent == []
        assert out["disabled"] == 1
        assert (await _row(store))["enabled"] is False
        assert alerts and "没有会话记录" in alerts[0]


# ---------------------------------------------------------------------------
# 投递三态与历史写回


class TestDelivery:
    @pytest.mark.asyncio
    async def test_sent_writes_back_assistant_history(self, store):
        first = _at(9, 0)
        await _user_said(store, "早", first)
        svc, sink, engine = await _service(store, engine=_FakeEngine(["备份还顺利吗"]))
        await svc.tick(now=first + 60)
        row = await _row(store)
        await store.schedule_mark_fired(row["id"], row["next_run"])
        await svc.tick(now=row["next_run"] + 1)

        session = await store.resolve_session(USER_ID, None)
        history = await store.get_history(session)
        assert [m["role"] for m in history] == ["user", "assistant"]
        assert history[-1]["content"] == "备份还顺利吗"
        # bot 自己的话不算"用户活跃"：沉默检测不能把自己算成用户
        assert await store.last_user_message_ts(session) == pytest.approx(first)
        # 生成时带上称呼与"最近说过什么"
        assert engine.calls[0]["context"]["nickname"] == "夏祭"

    @pytest.mark.asyncio
    async def test_uncertain_does_not_resend_or_write_history(self, store):
        first = _at(9, 0)
        await _user_said(store, "早", first)
        svc, sink, _eng = await _service(store, sink=_FakeSink(["uncertain"]))
        await svc.tick(now=first + 60)
        row = await _row(store)
        await store.schedule_mark_fired(row["id"], row["next_run"])
        await svc.tick(now=row["next_run"] + 1)

        session = await store.resolve_session(USER_ID, None)
        assert [m["role"] for m in await store.get_history(session)] == ["user"]
        state = (await _row(store))["params"]
        assert state["unanswered"] == 0, "结果不确定不计未回复"
        assert "备份" in state["recent_texts"][0], "记住这句话，避免下次原样再发"
        assert len(sink.sent) == 1, "uncertain 绝不重发"

    @pytest.mark.asyncio
    async def test_failure_backs_off_then_disables_with_alert(self, store):
        first = _at(9, 0)
        await _user_said(store, "早", first)
        alerts = []
        svc, sink, _eng = await _service(
            store,
            sink=_FakeSink(["failed"] * 10),
            cfg=_cfg(max_failures=2, retry_delay_seconds=60),
            on_alert=alerts.append,
        )
        await svc.tick(now=first + 60)
        row = await _row(store)
        await store.schedule_mark_fired(row["id"], row["next_run"])

        out = await svc.tick(now=row["next_run"] + 1)
        assert out["failed"] == 1
        sent_at = row["next_run"] + 1
        state = (await _row(store))["params"]
        assert state["failures"] == 1
        row = await _row(store)
        # 指数退避：60 * 2^0，且仍启用（失败不是停用）
        assert row["next_run"] == pytest.approx(sent_at + 60, abs=5)
        assert row["enabled"] is True

        await store.schedule_mark_fired(row["id"], row["next_run"])
        out = await svc.tick(now=row["next_run"] + 1)
        assert out["disabled"] == 1
        assert (await _row(store))["enabled"] is False
        assert alerts and "连续 2 次投递失败" in alerts[0]

    @pytest.mark.asyncio
    async def test_empty_generation_skips_without_template(self, store):
        first = _at(9, 0)
        await _user_said(store, "早", first)
        svc, sink, _eng = await _service(store, engine=_FakeEngine(["   "]))
        await svc.tick(now=first + 60)
        row = await _row(store)
        await store.schedule_mark_fired(row["id"], row["next_run"])
        out = await svc.tick(now=row["next_run"] + 1)
        assert sink.sent == [], "生成失败绝不发兜底模板（主动消息模板化就露馅）"
        assert out["skipped"] == 1
        assert (await _row(store))["params"]["last_skip_reason"] == "empty_generation"


# ---------------------------------------------------------------------------
# 管理面


class TestManagement:
    @pytest.mark.asyncio
    async def test_run_now_bypasses_guards(self, store):
        """手动触发忽略静默/晚睡/活跃保护（试语气用），但仍受生成与投递约束。"""
        await _user_said(store, "凌晨还在", _at(3, 0))
        svc, sink, _eng = await _service(store)
        result = await svc.run_now("normal")
        # 回执要能当"测试证明"：字数 + 未回复计数 + 下次最早时间
        assert result.startswith("已发送（")
        assert "字" in result and "未回复 1/3" in result
        assert "下次最早" in result
        assert len(sink.sent) == 1

    @pytest.mark.asyncio
    async def test_run_now_goodbye_uses_goodbye_prompt(self, store):
        await _user_said(store, "早", _at(9, 0))
        svc, sink, engine = await _service(store)
        await svc.run_now("goodbye")
        assert len(sink.sent) == 1
        assert "安静" in engine.calls[0]["instruction"]

    @pytest.mark.asyncio
    async def test_status_and_pause(self, store):
        await _user_said(store, "早", _at(9, 0))
        svc, _sink, _eng = await _service(store)
        await svc.note_user_message(USER_ID, "在忙", ts=_at(9, 1))
        rows = await svc.status()
        assert len(rows) == 1 and rows[0]["target"] == TARGET
        assert rows[0]["pause_until"] > 0

        count = await svc.pause(72)
        assert count == 1
        rows = await svc.status()
        assert rows[0]["last_skip_reason"] == "manual_pause"

    @pytest.mark.asyncio
    async def test_set_enabled_writes_file(self, store, tmp_path):
        path = tmp_path / "nudge.json"
        svc, _sink, _eng = await _service(store)
        svc._config_path = path
        assert svc.set_enabled(True) is True
        assert json.loads(path.read_text(encoding="utf-8"))["enabled"] is True
        assert svc._cfg().enabled is True
        assert svc.set_enabled(False) is True
        assert svc._cfg().enabled is False

    @pytest.mark.asyncio
    async def test_config_hot_reload(self, store, tmp_path):
        path = tmp_path / "nudge.json"
        save_nudge_config(path, _cfg(max_chars=60))
        svc, _sink, engine = await _service(store)
        svc._config_path = path
        assert svc._cfg().max_chars == 60

        # Web 改完 → 下一次 tick 就用新值（无需重启）
        cfg = svc._cfg()
        cfg.max_chars = 30
        save_nudge_config(path, cfg)
        assert svc._cfg().max_chars == 30

        # 文件被删：保留上一份配置，不静默回落默认
        path.unlink()
        assert svc._cfg().max_chars == 30

    @pytest.mark.asyncio
    async def test_disabled_service_does_nothing(self, store):
        await _user_said(store, "早", _at(9, 0))
        svc, sink, _eng = await _service(store, cfg=_cfg(enabled=False))
        out = await svc.tick(now=_at(12, 0))
        assert out == {
            "targets": 0,
            "sent": 0,
            "skipped": 0,
            "failed": 0,
            "disabled": 0,
        }
        assert sink.sent == []


@pytest.fixture
def store():
    return InMemoryMemoryStore()


class TestNicknameResolution:
    """称呼解析：跨会话读取 + 任何异常都退化成默认值。

    2026-10-23 线上实测两连击：① ``list_facts`` 返回 ``list[str]`` 而实现按
    dict 读 → ``/nudge run`` 直接 AttributeError 报错给用户；
    ② 称呼最可能来自**群里**一句「叫我夏祭」，按私聊会话过滤会一条都取不到。
    """

    @pytest.mark.asyncio
    async def test_nickname_comes_from_group_session_fact(self, store):
        await _user_said(store, "早", _at(9, 0))
        # 事实落在**群会话**里（真实来源：用户在群里说「叫我夏祭」）
        group_session = await store.resolve_session(USER_ID, "888")
        await store.save_fact(
            USER_ID,
            "用户昵称为夏祭",
            [0.0],
            source="user_message",
            session_id=group_session,
        )
        svc, _sink, engine = await _service(store)
        text = await svc._generate(svc._cfg(), {}, "normal", USER_ID)
        assert text, "称呼解析失败不该让整条消息发不出来"
        assert engine.calls[0]["context"]["nickname"] == "夏祭"

    @pytest.mark.asyncio
    async def test_store_failure_falls_back_to_default(self, store, monkeypatch):
        await _user_said(store, "早", _at(9, 0))
        svc, _sink, engine = await _service(store)

        async def _boom(*a, **kw):
            raise RuntimeError("db down")

        monkeypatch.setattr(store, "list_facts", _boom)
        text = await svc._generate(svc._cfg(), {}, "normal", USER_ID)
        assert text
        assert engine.calls[0]["context"]["nickname"] == "夏祭", "回落默认称呼"

    @pytest.mark.asyncio
    async def test_explicit_nickname_wins(self, store):
        await _user_said(store, "早", _at(9, 0))
        svc, _sink, engine = await _service(store, cfg=_cfg(nickname="老板"))
        await svc._generate(svc._cfg(), {}, "normal", USER_ID)
        assert engine.calls[0]["context"]["nickname"] == "老板"


# ---------------------------------------------------------------------------
# /nudge 命令面


def _admin_module():
    import importlib as _il

    import nonebot

    nonebot.init(_env_file=None, superusers={"10000"})
    return _il.import_module("plugins.qq_agent_adapter.admin")


class _EvOn:
    def get_message(self):
        return "/nudge on"

    def get_user_id(self):
        return "10000"


class TestNudgeCommand:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("/nudge", ("status", "")),
            ("/nudge ls", ("status", "")),
            ("/nudge why", ("why", "")),
            ("/nudge info", ("why", "")),
            ("/nudge run", ("run", "")),
            ("/nudge run goodbye", ("run", "goodbye")),
            ("/nudge test", ("run", "")),
            ("/nudge off", ("off", "")),
            ("/nudge stop", ("off", "")),
            ("/nudge on", ("on", "")),
            ("/nudge pause 72", ("pause", "72")),
            ("/nudge help", ("help", "")),
            ("/主动联系 off", ("off", "")),
            ("/nudge 不认识的", ("help", "")),
        ],
    )
    def test_parse_nudge_cmd(self, raw, expected):
        admin = _admin_module()
        assert admin.parse_nudge_cmd(raw) == expected

    @pytest.mark.asyncio
    async def test_non_superuser_cannot_manage(self, monkeypatch):
        from nonebot.exception import FinishedException

        admin = _admin_module()
        calls = []

        async def fake_finish(msg=None, **kw):
            calls.append(msg)
            raise FinishedException()

        monkeypatch.setattr(admin.nudge_cmd, "finish", fake_finish)
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: False)

        def _boom():
            raise AssertionError("非管理员不应触达主动联系服务")

        monkeypatch.setattr(admin, "_get_nudge", _boom)

        class _Ev:
            def get_message(self):
                return "/nudge"

            def get_user_id(self):
                return "20002"

        with pytest.raises(FinishedException):
            await admin.handle_nudge(_Ev())
        assert calls == ["只有管理员能管理主动联系。"]

    @pytest.mark.asyncio
    async def test_command_is_logged(self, monkeypatch, caplog):
        """管理命令必须留痕：线上 `/nudge run` 失败时日志一行都没有，排查全靠猜。"""
        import logging

        admin = _admin_module()

        async def fake_finish(msg=None, **kw):
            from nonebot.exception import FinishedException

            raise FinishedException()

        monkeypatch.setattr(admin.nudge_cmd, "finish", fake_finish)
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: True)

        store = InMemoryMemoryStore()
        await _user_said(store, "早", _at(9, 0))
        svc, _sink, _eng = await _service(store)
        monkeypatch.setattr(admin, "_get_nudge", lambda: svc)

        class _Ev:
            def get_message(self):
                return "/nudge run normal"

            def get_user_id(self):
                return "10000"

        from nonebot.exception import FinishedException

        with caplog.at_level(logging.INFO, logger="plugins.qq_agent_adapter.admin"):
            with pytest.raises(FinishedException):
                await admin.handle_nudge(_Ev())
        assert "[nudge-cmd] user=10000 action=run arg=normal" in caplog.text

    @pytest.mark.asyncio
    async def test_status_and_off_flows(self, monkeypatch):
        from nonebot.exception import FinishedException

        admin = _admin_module()
        calls = []

        async def fake_finish(msg=None, **kw):
            calls.append(msg)
            raise FinishedException()

        monkeypatch.setattr(admin.nudge_cmd, "finish", fake_finish)
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: True)

        store = InMemoryMemoryStore()
        await _user_said(store, "早", _at(9, 0))
        svc, _sink, _eng = await _service(store)
        monkeypatch.setattr(admin, "_get_nudge", lambda: svc)

        class _Ev:
            def __init__(self, text):
                self._text = text

            def get_message(self):
                return self._text

            def get_user_id(self):
                return "10000"

        with pytest.raises(FinishedException):
            await admin.handle_nudge(_Ev("/nudge"))
        assert "主动联系（1 个目标）" in calls[-1]
        assert TARGET in calls[-1]

        with pytest.raises(FinishedException):
            await admin.handle_nudge(_Ev("/nudge why"))
        assert "下次" in calls[-1]

        with pytest.raises(FinishedException):
            await admin.handle_nudge(_Ev("/nudge pause 72"))
        assert "已暂停 1 个目标 72 小时" in calls[-1]

        # off/on 走配置文件：路径指向 tmp，避免碰到真实 data/nudge.json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            svc._config_path = Path(tmp) / "nudge.json"
            with pytest.raises(FinishedException):
                await admin.handle_nudge(_Ev("/nudge off"))
            assert calls[-1] == "主动联系已关闭。"
            assert svc._cfg().enabled is False

    @pytest.mark.asyncio
    async def test_on_warns_when_env_switch_overrides(self, monkeypatch):
        """env 总开关优先级高于配置文件：这点必须说出来，否则开关像坏了。"""
        from nonebot.exception import FinishedException

        admin = _admin_module()
        calls = []

        async def fake_finish(msg=None, **kw):
            calls.append(msg)
            raise FinishedException()

        monkeypatch.setattr(admin.nudge_cmd, "finish", fake_finish)
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: True)
        monkeypatch.setenv("AGENT_NUDGE_ENABLED", "0")

        store = InMemoryMemoryStore()
        svc, _sink, _eng = await _service(store)
        monkeypatch.setattr(admin, "_get_nudge", lambda: svc)

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            svc._config_path = Path(tmp) / "nudge.json"
            with pytest.raises(FinishedException):
                await admin.handle_nudge(_EvOn())
            assert "AGENT_NUDGE_ENABLED=0 优先级更高" in calls[-1]

    @pytest.mark.asyncio
    async def test_disabled_service_is_explained(self, monkeypatch):
        from nonebot.exception import FinishedException

        admin = _admin_module()
        calls = []

        async def fake_finish(msg=None, **kw):
            calls.append(msg)
            raise FinishedException()

        monkeypatch.setattr(admin.nudge_cmd, "finish", fake_finish)
        monkeypatch.setattr(admin, "is_allowed", lambda ev: True)
        monkeypatch.setattr(admin, "is_superuser", lambda uid: True)
        monkeypatch.setattr(admin, "_get_nudge", lambda: None)

        class _Ev:
            def get_message(self):
                return "/nudge"

            def get_user_id(self):
                return "10000"

        with pytest.raises(FinishedException):
            await admin.handle_nudge(_Ev())
        assert calls == [
            "主动联系未启用：先在 data/nudge.json 里配置 targets（private:<QQ号>）。"
        ]
