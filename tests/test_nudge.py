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
import time
from pathlib import Path

import pytest

from agentcore import tz
from agentcore.memory.store import InMemoryMemoryStore
from agentcore.scheduler import nudge as nudge_mod
from agentcore.scheduler.events import (
    EVENT_FOLLOWUP_TASK,
    EVENT_TASK,
    extract_birthday,
    extract_events,
    triggers_for,
)
from agentcore.scheduler.hotspot import BREAKING_TASK, HOT_TASK
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


def _item(
    title="PHP 出现远程代码执行漏洞",
    url="https://php.net/a",
    date=None,
    snippet="官方提前发布修复版本",
    source="php.net",
):
    return {
        "title": title,
        "url": url,
        "source": source,
        "snippet": snippet,
        "date": date if date is not None else tz.day_key(),
    }


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
    """确定性随机：``uniform`` 恒取下限、``random`` 恒为 0、``choice`` 取首个。

    用来断言"最早 6 小时"这类边界，并让本周类别/候选抽取可复现。
    """

    def uniform(self, a, b):
        return a

    def random(self):
        return 0.0

    def choice(self, seq):
        return seq[0]


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


class _FakeLLM:
    """评分用桩：默认把所有候选打成 9 分。"""

    def __init__(self, content='[{"i":0,"score":9,"reason":"大事","category":"tech"}]'):
        self.content = content
        self.calls = []

    async def chat(self, messages, tools=None, max_tokens=None):
        self.calls.append({"messages": messages, "max_tokens": max_tokens})
        return {"choices": [{"message": {"content": self.content}}]}


async def _service(
    store,
    *,
    sink=None,
    engine=None,
    cfg=None,
    on_alert=None,
    seed=7,
    rng=None,
    llm=None,
):
    """默认用 _MinRng：first delay 恒为 6h，时序断言才可复现。"""
    sink = sink or _FakeSink()
    engine = engine or _FakeEngine()
    svc = NudgeService(
        store,
        sink,
        engine,
        llm=llm,
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


@pytest.fixture(autouse=True)
def _no_real_hotspot(monkeypatch, tmp_path):
    """默认不真的轮询：测试打到真实搜索/LLM 既花钱又不稳定。

    需要验证轮询本身的用例在自己的函数里再 monkeypatch 覆盖即可。
    """

    async def _no_search(query, max_results=None):
        return []

    async def _no_score(llm, items):
        return []

    monkeypatch.setattr(nudge_mod, "search_items", _no_search)
    monkeypatch.setattr(nudge_mod, "score_candidates", _no_score)
    monkeypatch.setenv("AGENT_NUDGE_HOTSPOT_FILE", str(tmp_path / "hot.json"))


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


# ---------------------------------------------------------------------------
# 热点（P2）：轮询 → 候选池 → 爆点抢占 → 配额


class _FakeSearch:
    """search_items 桩：按查询词返回预置结果。"""

    def __init__(self, by_query: dict):
        self.by_query = by_query
        self.calls: list[str] = []

    async def __call__(self, query, max_results=None):
        self.calls.append(query)
        return list(self.by_query.get(query, []))


def _cand(**over):
    base = {
        "hash": "h1",
        "title": "PHP 出现远程代码执行漏洞",
        "url": "https://php.net/a",
        "source": "php.net",
        "date": tz.day_key(),
        "snippet": "官方提前发布修复版本",
        "category": "tech",
        "score": 9,
        "reason": "与安全领域高度相关",
        "kind": "breaking",
        "found_at": time.time(),
    }
    base.update(over)
    return base


async def _prime(store, ts: float) -> None:
    """把 last_user_seen 推到 ts，跳过新行首 tick 必然走的 reset 分支。

    否则第一次 tick 只做"重置倒计时并返回"，爆点/候选要等下一个 tick 才被看到
    ——测试里就表现为"明明有候选却没发"。
    """
    row = await _row(store)
    if row is None:
        return
    state = dict(row.get("params") or {})
    state["last_user_seen"] = ts
    await store.schedule_set_params(row["id"], state)


async def _svc_with_hot(
    store, *, hot=None, cfg=None, sink=None, engine=None, path=None, llm=None
):
    svc, sink, engine = await _service(
        store,
        sink=sink,
        engine=engine,
        cfg=cfg or _cfg(hot_topics=True),
        llm=llm or _FakeLLM(),
    )
    if hot is not None:
        svc._hot = hot
    if path is not None:
        svc._hot_path = lambda: Path(path)
    # 默认把"已见水位"推到当下：这些用例关心的是候选/爆点，不是首 tick 的 reset
    await _prime(store, time.time())
    return svc, sink, engine


class TestHotspotPolling:
    @pytest.mark.asyncio
    async def test_poll_fills_pool_and_respects_interval(self, store, monkeypatch):
        svc, _sink, _eng = await _svc_with_hot(store, llm=_FakeLLM())
        search = _FakeSearch({"AI 大模型": [_item()]})
        monkeypatch.setattr(nudge_mod, "search_items", search)
        monkeypatch.setattr(nudge_mod, "score_candidates", _fake_score)

        now = _at(12, 0)
        out = await svc.tick(now=now)
        assert search.calls, "首次 tick 应立刻轮询一次"
        assert len(svc._hot["pending"]) == 1
        assert svc._hot["polls"]["n"] == 1
        assert svc._hot["last_poll"] == now
        assert out["sent"] == 0

        # 没到间隔不轮询
        search.calls.clear()
        await svc.tick(now=now + 60)
        assert search.calls == []

        # 到间隔再轮询
        await svc.tick(now=now + 4 * 3600 + 1)
        assert len(search.calls) >= 1

    @pytest.mark.asyncio
    async def test_poll_cap_per_day(self, store, monkeypatch):
        svc, _sink, _eng = await _svc_with_hot(
            store,
            hot={
                "last_poll": 0.0,
                "polls": {"day": tz.day_key(), "n": 15},
                "pending": [],
                "mentioned": {},
            },
        )
        monkeypatch.setattr(
            nudge_mod, "search_items", _FakeSearch({"AI 大模型": [_item()]})
        )
        await svc.tick(now=_at(12, 0))
        assert svc._hot["polls"]["n"] == 15, "达到当日轮询上限后不再检索"

    @pytest.mark.asyncio
    async def test_hot_topics_off_means_no_polling(self, store, monkeypatch):
        svc, _sink, _eng = await _svc_with_hot(store, cfg=_cfg(hot_topics=False))
        search = _FakeSearch({"AI 大模型": [_item()]})
        monkeypatch.setattr(nudge_mod, "search_items", search)
        await svc.tick(now=_at(12, 0))
        assert search.calls == []

    @pytest.mark.asyncio
    async def test_search_failure_does_not_break_tick(self, store, monkeypatch):
        svc, sink, _eng = await _svc_with_hot(store, llm=_FakeLLM())

        async def _boom(query, max_results=None):
            raise RuntimeError("tavily down")

        monkeypatch.setattr(nudge_mod, "search_items", _boom)
        out = await svc.tick(now=_at(12, 0))
        assert out["targets"] == 1, "检索失败不该影响主动联系本身"

    @pytest.mark.asyncio
    async def test_state_file_roundtrip(self, store, tmp_path):
        path = tmp_path / "hot.json"
        svc, _sink, _eng = await _svc_with_hot(store, path=path)
        svc._hot["pending"] = [_cand()]
        svc._hot["mentioned"] = {"old": time.time() - 3600}
        svc._save_hot()
        assert path.stat().st_mode & 0o777 == 0o600

        other, _s2, _e2 = await _service(store, cfg=_cfg(hot_topics=True))
        other._hot_path = lambda: Path(path)
        other._load_hot()
        assert other._hot["pending"][0]["title"] == _cand()["title"]
        assert "old" in other._hot["mentioned"]

        # 损坏文件按空处理，不抛
        path.write_text("{ not json", encoding="utf-8")
        other._load_hot()
        assert other._hot["pending"] == []


class TestHotspotPick:
    def test_take_breaking_prefers_highest_score(self):
        svc = _svc(_store_stub())
        svc._hot = {
            "pending": [_cand(hash="a", score=8), _cand(hash="b", score=10)],
            "mentioned": {},
        }
        assert svc._take_breaking(_cfg(), time.time())["hash"] == "b"

    def test_take_breaking_skips_stale_and_recent(self):
        now = time.time()
        svc = _svc(_store_stub())
        svc._hot = {
            "pending": [
                _cand(hash="a", date=tz.day_key(-3 * 86400)),  # 旧闻
                _cand(hash="b"),  # 24h 内提到过
            ],
            "mentioned": {"b": now - 3600},
        }
        assert svc._take_breaking(_cfg(), now) is None

    def test_take_breaking_allows_after_24h(self):
        now = time.time()
        svc = _svc(_store_stub())
        svc._hot = {"pending": [_cand(hash="b")], "mentioned": {"b": now - 25 * 3600}}
        assert svc._take_breaking(_cfg(), now)["hash"] == "b"

    def test_take_candidate_skips_recently_mentioned(self):
        now = time.time()
        svc = _svc(_store_stub())
        svc._hot = {
            "pending": [_cand(hash="a", kind="candidate", score=7)],
            "mentioned": {"a": now - 10 * 86400},  # 10 天前提到过
        }
        assert svc._take_candidate(_cfg(hot_dedupe_days=90), now) is None, (
            "90 天内提到过的不再当普通候选"
        )
        # 爆点不受 90 天限制（24h 规则即可）
        svc._hot["pending"][0]["kind"] = "breaking"
        assert svc._take_breaking(_cfg(), now)["hash"] == "a"

    def test_take_candidate_empty_pool(self):
        svc = _svc(_store_stub())
        svc._hot = {"pending": [], "mentioned": {}}
        assert svc._take_candidate(_cfg(), time.time()) is None

    def test_consume_moves_to_mentioned(self):
        svc = _svc(_store_stub())
        svc._hot = {"pending": [_cand()], "mentioned": {}}
        svc._config_path = Path("/nonexistent/nudge.json")
        svc._consume(_cand(), time.time())
        assert svc._hot["pending"] == []
        assert svc._hot["mentioned"]["h1"] > 0


def _store_stub():
    """_take_* 不碰 store，给个最小替身即可。"""

    class _S:
        pass

    return _S()


async def _fake_score(llm, items):
    """score_candidates 桩：全部打 9 分当爆点。"""
    out = []
    for it in items:
        row = dict(it)
        row.update({"score": 9, "reason": "大事", "kind": "breaking"})
        out.append(row)
    return out


def _svc(store):
    from agentcore.scheduler.nudge import NudgeService

    return NudgeService(
        store,
        _FakeSink(),
        _FakeEngine(),
        config=_cfg(hot_topics=True),
        config_path="/nonexistent/nudge.json",
        rng=random.Random(3),
    )


class TestBreakingTakeover:
    @pytest.mark.asyncio
    async def test_breaking_ignores_first_delay_but_not_guards(self, store):
        """爆点是事件驱动：不被"6–24h 第一次"挡住；静默/晚睡仍挡住。"""
        await _user_said(store, "早", _at(9, 0))
        svc, sink, engine = await _svc_with_hot(
            store,
            hot={"pending": [_cand()], "mentioned": {}, "last_poll": 0.0, "polls": {}},
        )
        # 刚排过下一次（6–24h 后），但爆点立刻放行
        row = await _row(store)
        await store.schedule_mark_fired(row["id"], _at(20, 0))
        out = await svc.tick(now=_at(15, 0))
        assert len(sink.sent) == 1
        assert out["sent"] == 1
        assert engine.calls[0]["instruction"] == BREAKING_TASK
        assert "PHP" in engine.calls[0]["extra_data"]
        assert (await _row(store))["params"]["counts"]["breaking"] == 1
        assert svc._hot["pending"] == [], "发过就消费掉"

    @pytest.mark.asyncio
    async def test_quiet_hours_queue_the_breaking_to_morning(self, store):
        """凌晨的爆点不发送也不丢弃：等静默结束自然重试。"""
        await _user_said(store, "凌晨还在", _at(2, 0))
        svc, sink, _eng = await _svc_with_hot(
            store,
            hot={
                "pending": [_cand()],
                "mentioned": {},
                "last_poll": _at(2, 30),
                "polls": {},
            },
        )
        out = await svc.tick(now=_at(2, 30))
        assert sink.sent == []
        assert out["skipped"] == 1
        assert svc._hot["pending"], "候选必须留着，等早上再发"

        await svc.tick(now=_at(10, 0))
        assert len(sink.sent) == 1

    @pytest.mark.asyncio
    async def test_breaking_daily_cap_and_override(self, store):
        await _user_said(store, "早", _at(9, 0))
        svc, sink, _eng = await _svc_with_hot(
            store,
            hot={
                "pending": [_cand(hash="a", score=9), _cand(hash="b", score=8)],
                "mentioned": {},
                "last_poll": 0.0,
                "polls": {},
            },
        )
        row = await _row(store)
        await store.schedule_mark_fired(row["id"], _at(15, 0))
        await svc.tick(now=_at(15, 0))
        assert len(sink.sent) == 1

        # 当天第二条爆点：8 分不够破例 → 跳过；换 9 分 → 放行（总上限 3 → 4）
        svc._hot["pending"] = [_cand(hash="b", score=8)]
        await svc.tick(now=_at(16, 0))
        assert len(sink.sent) == 1
        assert (await _row(store))["params"]["last_skip_reason"] == "breaking_cap"

        svc._hot["pending"] = [_cand(hash="c", score=9)]
        await svc.tick(now=_at(17, 0))
        assert len(sink.sent) == 2, "≥9 分可破例"

    @pytest.mark.asyncio
    async def test_normal_nudge_uses_hot_candidate(self, store):
        await _user_said(store, "早", _at(9, 0))
        svc, sink, engine = await _svc_with_hot(
            store,
            hot={
                "pending": [_cand(kind="candidate", score=7)],
                "mentioned": {},
                "last_poll": 0.0,
                "polls": {},
            },
        )
        row = await _row(store)
        await store.schedule_mark_fired(row["id"], _at(15, 0))
        await svc.tick(now=_at(15, 0))
        assert len(sink.sent) == 1
        assert engine.calls[0]["instruction"] == HOT_TASK
        assert "PHP" in engine.calls[0]["extra_data"]
        assert (await _row(store))["params"]["counts"]["normal"] == 1

    @pytest.mark.asyncio
    async def test_no_candidate_means_plain_chat(self, store):
        await _user_said(store, "早", _at(9, 0))
        svc, sink, engine = await _svc_with_hot(
            store,
            hot={"pending": [], "mentioned": {}, "last_poll": _at(15, 0), "polls": {}},
        )
        await _prime(store, _at(9, 0))
        row = await _row(store)
        await store.schedule_mark_fired(row["id"], _at(15, 0))
        await svc.tick(now=_at(15, 0))
        assert len(sink.sent) == 1
        assert engine.calls[0]["extra_data"] == ""
        assert engine.calls[0]["instruction"] != HOT_TASK

    def test_hotspot_status(self):
        svc = _svc(_store_stub())
        svc._hot = {
            "pending": [_cand(), _cand(hash="x", kind="candidate")],
            "mentioned": {},
            "last_poll": 123.0,
            "polls": {"n": 2},
        }
        st = svc.hotspot_status()
        assert st["pending"] == 2 and st["breaking"] == 1
        assert st["polls_today"] == 2 and st["last_poll"] == 123.0
        assert st["week"] in ("tech", "general")


# ---------------------------------------------------------------------------
# 记忆事件（P3）


def _ev_cfg(**over):
    cfg = _cfg(memory_events=True)
    for key, value in over.items():
        setattr(cfg, key, value)
    return cfg


class TestEventExtraction:
    """确定性抽取：宁可漏，不可把过去的事/随口一说当成承诺。"""

    def test_future_event_extracted(self):
        now = time.time()
        out = extract_events("明天上午10点有个面试", now)
        assert len(out) == 1
        assert out[0]["kind"] == "interview"
        assert out[0]["quote"] == "明天上午10点有个面试"

    def test_past_and_recurring_rejected(self):
        now = time.time()
        assert extract_events("上周面试完了", now) == []
        assert extract_events("昨天面完试了", now) == []
        assert extract_events("每天9点提醒我喝水", now) == []
        assert extract_events("10分钟后提醒我", now) == []
        assert extract_events("会议取消了，明天不去了", now) == []
        assert extract_events("年底前要上线", now) == [], "没有日粒度时间词 → 不猜"

    def test_past_marker_in_the_same_clause_rejects(self):
        """这些小句里**有事件词**，只有过去/否定标记能毙掉它们。"""
        now = time.time()
        assert extract_events("面试已经结束了", now) == []
        assert extract_events("会议取消了", now) == []
        assert extract_events("上线推迟了", now) == []

    def test_negation_in_the_next_clause_rejects(self):
        """否定/过去标记常落在下一句："明天面试，已经结束了"。"""
        now = time.time()
        assert extract_events("明天面试，已经结束了", now) == []
        assert extract_events("明天开会，会议取消了", now) == []
        # 正常的仍然要抽到
        assert extract_events("昨天加班到很晚，明天还要面试", now) != []

    def test_recurring_with_event_word_rejected(self):
        now = time.time()
        assert extract_events("每天9点开会", now) == []
        assert extract_events("每周一都要开会", now) == []

    def test_past_today_is_dropped(self):
        """今天但已过的时刻：追问没有意义。"""
        assert extract_events("今天9点有个面试", _at(12, 0)) == [], "已过的时刻"
        assert extract_events("今天下午3点开会", _at(16, 0)) == [], "已过的时刻"
        # 还没到的今天时刻要留
        assert extract_events("今天下午3点开会", _at(12, 0)) != []
        assert extract_events("今天下午3点开会", _at(9, 0)) != []

    def test_past_marker_does_not_block_a_distant_future_one(self):
        """ "昨天加班到很晚，明天还要面试"里的昨天不该连坐。"""
        now = time.time()
        out = extract_events("昨天加班到很晚，明天还要面试", now)
        assert [e["kind"] for e in out] == ["interview"]

    def test_time_word_and_event_word_must_share_a_clause(self):
        now = time.time()
        assert extract_events("明天天气不错。我今天很开心", now) == [], "没有事件词"
        assert extract_events("明天。面试", now) == [], "时间词与事件词不同句"

    def test_lookahead_window(self):
        now = time.time()
        assert extract_events("2027年12月1号截止", now, lookahead_days=60) == []
        assert extract_events("11月15号截止", now, lookahead_days=60) != [], (
            "30 多天内的仍要抽到"
        )

    def test_custom_event_words(self):
        now = time.time()
        words = {"复盘": "review"}
        assert (
            extract_events("明天下午复盘", now, event_words=words)[0]["kind"]
            == "review"
        )

    def test_triggers_by_kind(self):
        at = _at(10, 0, 2)  # 两天后的 10:00
        ts = triggers_for("interview", at)
        assert len(ts) == 2, "面试类前一晚 + 当天早上各问一次"
        assert ts[0] < ts[1]
        assert len(triggers_for("deadline", at)) == 1
        assert len(triggers_for("travel", at)) == 1


class TestBirthday:
    def test_from_facts(self):
        now = time.time()
        found = extract_birthday(["用户的生日是8月12号"], now)
        assert found is not None and found["kind"] == "birthday"
        assert found["quote"] == "用户的生日是8月12号"

    def test_noise_not_matched(self):
        now = time.time()
        assert extract_birthday(["用户知晓名为云崽的对象当日不过生日"], now) is None
        assert extract_birthday([], now) is None
        assert extract_birthday(["用户生日是13月40号"], now) is None

    def test_third_party_birthday_not_matched(self):
        """别人的生日不能当成主人的（facts 里真有这种第三人称记录）。"""
        now = time.time()
        assert extract_birthday(["用户的朋友生日是3月5日"], now) is None
        assert extract_birthday(["群成员夏祭的生日是3月5日"], now) is None
        assert extract_birthday(["我的生日是8月12号"], now) is not None


class TestEventFlow:
    @pytest.mark.asyncio
    async def test_event_wins_over_normal_and_breaking(self, store):
        await _user_said(store, "明天上午10点有个面试", _at(9, 0))
        svc, sink, engine = await _svc_with_hot(
            store,
            cfg=_ev_cfg(),
            hot={
                "pending": [_cand()],
                "mentioned": {},
                "last_poll": _at(15, 0),
                "polls": {},
            },
        )
        # 手动把事件的触发点拨到当下
        row = await _row(store)
        state = dict(row.get("params") or {})
        state["events"] = [
            {
                "id": "e1",
                "kind": "interview",
                "at": _at(10, 0, 1),
                "quote": "明天上午10点有个面试",
                "triggers": [_at(15, 0)],
                "asked": 0,
                "last_asked_at": 0,
                "followed_up": False,
                "done": False,
            }
        ]
        await store.schedule_set_params(row["id"], state)
        await store.schedule_mark_fired(row["id"], _at(15, 0))

        out = await svc.tick(now=_at(15, 0))
        assert len(sink.sent) == 1
        assert engine.calls[0]["instruction"] == EVENT_TASK
        assert "明天上午10点有个面试" in engine.calls[0]["extra_data"]
        assert (await _row(store))["params"]["counts"]["event"] == 1
        assert out["sent"] == 1
        # 事件发过就消耗掉，热点候选还留着
        assert len(svc._hot["pending"]) == 1

    @pytest.mark.asyncio
    async def test_event_respects_quiet_hours(self, store):
        await _user_said(store, "凌晨还在", _at(2, 0))
        svc, sink, _eng = await _svc_with_hot(store, cfg=_ev_cfg())
        row = await _row(store)
        state = dict(row.get("params") or {})
        state["events"] = [
            {
                "id": "e1",
                "kind": "meeting",
                "at": _at(10, 0, 1),
                "quote": "明天10点开会",
                "triggers": [_at(2, 30)],
                "asked": 0,
                "last_asked_at": 0,
                "followed_up": False,
                "done": False,
            }
        ]
        await store.schedule_set_params(row["id"], state)
        out = await svc.tick(now=_at(2, 30))
        assert sink.sent == []
        assert out["skipped"] == 1
        # 事件没被消费：早上会再问
        assert (await _row(store))["params"]["events"]

        await svc.tick(now=_at(10, 0))
        assert len(sink.sent) == 1

    @pytest.mark.asyncio
    async def test_event_daily_cap(self, store):
        await _user_said(store, "早", _at(9, 0))
        svc, sink, _eng = await _svc_with_hot(store, cfg=_ev_cfg())

        async def _set_events(triggers):
            row = await _row(store)
            state = dict(row.get("params") or {})
            state["events"] = [
                {
                    "id": f"e{i}",
                    "kind": "meeting",
                    "at": _at(10, 0, 1),
                    "quote": "明天开会",
                    "triggers": [t],
                    "asked": 0,
                    "last_asked_at": 0,
                    "followed_up": False,
                    "done": False,
                }
                for i, t in enumerate(triggers)
            ]
            await store.schedule_set_params(row["id"], state)
            await store.schedule_mark_fired(row["id"], _at(15, 0))

        await _set_events([_at(15, 0)])
        await svc.tick(now=_at(15, 0))
        assert len(sink.sent) == 1

        # 当天第二个事件 → 跳过
        await _set_events([_at(16, 0)])
        await svc.tick(now=_at(16, 0))
        assert len(sink.sent) == 1
        assert (await _row(store))["params"]["last_skip_reason"] == "event_cap"

    @pytest.mark.asyncio
    async def test_followup_once_then_done(self, store):
        # 你最后一条消息必须早于"我问过"的时刻，否则算你已经回过了
        await _user_said(store, "明天上午10点有个面试", _at(9, 0) - 26 * 3600)
        svc, sink, engine = await _svc_with_hot(store, cfg=_ev_cfg())
        row = await _row(store)
        state = dict(row.get("params") or {})
        asked_at = _at(15, 0) - 25 * 3600
        state["events"] = [
            {
                "id": "e1",
                "kind": "interview",
                "at": _at(10, 0, 1),
                "quote": "明天上午10点有个面试",
                "triggers": [],
                "asked": 1,
                "last_asked_at": asked_at,
                "followed_up": False,
                "done": False,
            }
        ]
        await store.schedule_set_params(row["id"], state)
        await store.schedule_mark_fired(row["id"], _at(15, 0))
        await svc.tick(now=_at(15, 0))
        assert len(sink.sent) == 1
        assert engine.calls[0]["instruction"] == EVENT_FOLLOWUP_TASK

        # 追问只一次：再 tick 时事件已结案，不会再发第二次追问
        # （这一 tick 会因为测试里拨过 next_run 而发一条普通搭话，那不是追问）
        await store.schedule_mark_fired(row["id"], _at(16, 0))
        await svc.tick(now=_at(16, 0))
        assert engine.calls[-1]["instruction"] != EVENT_FOLLOWUP_TASK
        assert (await _row(store))["params"].get("events") == []
        assert "e1" in (await _row(store))["params"].get("events_done", [])

    @pytest.mark.asyncio
    async def test_user_reply_marks_event_done(self, store):
        """你在事件之后开过口 → 不再追问（_take_event 直接标记完成）。"""
        await _user_said(store, "明天上午10点有个面试", _at(9, 0))
        svc, sink, _eng = await _svc_with_hot(store, cfg=_ev_cfg())
        row = await _row(store)
        state = dict(row.get("params") or {})
        state["events"] = [
            {
                "id": "e1",
                "kind": "interview",
                "at": _at(10, 0, 1),
                "quote": "明天上午10点有个面试",
                "triggers": [],
                "asked": 1,
                "last_asked_at": _at(15, 0),
                "followed_up": False,
                "done": False,
            }
        ]
        await store.schedule_set_params(row["id"], state)
        # 你在 20:00 开口了（晚于 last_asked_at）
        await _user_said(store, "面完了", _at(20, 0))
        await _prime(store, _at(20, 0))
        await svc.tick(now=_at(20, 30))
        assert sink.sent == []
        # 下一轮扫描会把结案的它清出池子并记进 events_done
        await svc.tick(now=_at(20, 31))
        params = (await _row(store))["params"]
        assert all(e["id"] != "e1" for e in params.get("events", [])), (
            "你回过了，这条不再追问"
        )
        assert "e1" in (params.get("events_done") or [])

    @pytest.mark.asyncio
    async def test_scan_events_from_history(self, store):
        """增量扫描：从用户消息里抽出事件并落 state。"""
        await _user_said(store, "明天上午10点有个面试", _at(9, 0))
        svc, _sink, _eng = await _svc_with_hot(store, cfg=_ev_cfg())
        row = await _row(store)
        await svc._scan_events_for(row, svc._cfg(), _at(9, 30))
        state = (await _row(store))["params"]
        assert state["last_scan_msg_id"] > 0
        assert [e["kind"] for e in state["events"]] == ["interview"]
        assert state["events"][0]["triggers"], "触发点已排好"

        # 再扫一次不会重复入池
        await svc._scan_events_for(row, svc._cfg(), _at(9, 31))
        state = (await _row(store))["params"]
        assert len(state["events"]) == 1

    @pytest.mark.asyncio
    async def test_scan_events_off_by_default(self, store):
        await _user_said(store, "明天上午10点有个面试", _at(9, 0))
        svc, _sink, _eng = await _svc_with_hot(store, cfg=_cfg(memory_events=False))
        row = await _row(store)
        await svc._scan_events_for(row, svc._cfg(), _at(9, 30))
        assert (await _row(store))["params"].get("events") in (None, [])

    def test_prune_and_cap(self):
        from agentcore.scheduler.nudge import NudgeService

        cfg = _ev_cfg(event_max_pending=2)
        now = time.time()
        events = [
            {"id": "a", "at": now + 3600, "done": False, "triggers": [now]},
            {"id": "b", "at": now + 7200, "done": False, "triggers": [now]},
            {"id": "c", "at": now + 10800, "done": False, "triggers": [now]},
            {"id": "d", "at": now - 3 * 86400, "done": False, "triggers": []},
            {"id": "e", "at": now + 3600, "done": True, "triggers": []},
        ]
        kept, done = NudgeService._prune_events(cfg, events, now, [])
        assert [e["id"] for e in kept] == ["a", "b"], "按上限截断，丢完成与过期"
        assert set(done) >= {"d", "e"}, "完成的要记进 events_done，否则会被重新抽出"

    def test_event_status(self):
        svc = _svc(_store_stub())
        state = {
            "events": [
                {
                    "kind": "interview",
                    "at": 100.0,
                    "quote": "面试",
                    "triggers": [90.0],
                    "asked": 0,
                    "followed_up": False,
                    "done": False,
                },
                {
                    "kind": "meeting",
                    "at": 200.0,
                    "quote": "开会",
                    "triggers": [],
                    "asked": 1,
                    "followed_up": True,
                    "done": False,
                },
            ]
        }
        out = svc.event_status(state)
        assert [e["kind"] for e in out] == ["interview", "meeting"]
        assert out[1]["followed_up"] is True
