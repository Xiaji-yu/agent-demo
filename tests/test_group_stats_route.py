"""群发言统计接线测试：matcher 采集挂点 / 命令 / 冷却 / 周报 / 停机顺序。

来源：群发言榜+词云 AC 计划 §5-E。事件用真实 ``GroupMessageEvent.parse_obj``
构造（与 test_matcher.py 同款）；驱动属性用替身 driver + monkeypatch
stats_route._get_driver，不启动 NoneBot。
"""

from __future__ import annotations

import importlib

import pytest
from nonebot.adapters.onebot.v11 import GroupMessageEvent
from nonebot.exception import FinishedException

from agentcore import groupstats
from agentcore.groupstats import ranking
from agentcore.groupstats.store import InMemoryStatsStore
from plugins.qq_agent_adapter.matcher import handle_group_record

# stats_route 顶层有 on_command（import 期要读 driver 的 command_start 配置），
# 与 test_admin_import 同款：夹具内初始化 NoneBot 再 import。**不能在模块级
# init**——收集期就会跑，先把全局 driver 立起来，污染其后执行的 test_acl
# （实测：模块级 init 让 test_acl 6 条用例红）。
# 用 import_module 而不是 `from package import stats_route`：__init__.py 里有
# `stats_route = None` 作为惰性加载哨兵（与 music_route 同款），会把子模块名
# 阴影成 None（test_music_route.py 同款注释）。
stats_route = None


@pytest.fixture(scope="module", autouse=True)
def _load_stats_route():
    global stats_route
    import nonebot

    # 显式传配置并禁用 .env 读取：裸数字 SUPERUSERS 会让 pydantic 校验失败
    nonebot.init(_env_file=None, superusers={"10000"})
    stats_route = importlib.import_module("plugins.qq_agent_adapter.stats_route")


def _group_event(
    text: str = "服务器又炸了",
    *,
    self_id: int = 999,
    user_id: int = 123,
    group_id: int = 456,
    message_id: int = 1,
    segments: list | None = None,
):
    if segments is None:
        segments = [{"type": "text", "data": {"text": text}}]
    return GroupMessageEvent.parse_obj(
        {
            "time": 0,
            "self_id": self_id,
            "post_type": "message",
            "sub_type": "group",
            "user_id": user_id,
            "message_type": "group",
            "message_id": message_id,
            "group_id": group_id,
            "message": segments,
            "original_message": segments,
            "raw_message": text,
            "font": 0,
            "sender": {"user_id": user_id, "nickname": "昵称", "card": "名片"},
            "to_me": False,
            "reply": None,
            "anonymous": None,
        }
    )


@pytest.fixture
def store():
    return InMemoryStatsStore()


@pytest.fixture
def wired(monkeypatch, store):
    """开启统计 + 白名单含 456 + 装配采集器；返回 collector。"""
    monkeypatch.setenv("AGENT_GROUP_STATS", "1")
    monkeypatch.setenv("ALLOWED_GROUPS", "456")
    collector = groupstats.configure(store)
    assert collector is not None
    yield collector
    groupstats._collector = None


class _FakeMatcher:
    """finish 抛 FinishedException，与 NoneBot 真实行为一致。"""

    def __init__(self):
        self.finished = None

    async def finish(self, message=None):  # noqa: D102
        self.finished = message
        raise FinishedException


class _FakeDriver:
    def __init__(self, store=None, sink=None):
        self._agent_group_stats_store = store
        self._agent_sink = sink


class _RecordingSink:
    def __init__(self):
        self.sent = []

    async def send_once(self, target, message):
        self.sent.append((target, message))
        return "sent"


# ------------------------------------------------------- matcher 采集挂点


class TestRecorderHook:
    @pytest.mark.asyncio
    async def test_group_message_recorded(self, wired):
        await handle_group_record(_group_event())
        assert wired.pending()[0] == 1

    @pytest.mark.asyncio
    async def test_nickname_and_user_id_captured(self, wired):
        await handle_group_record(_group_event(user_id=777))
        row = list(wired._daily.values())[0]
        assert row["user_id"] == "777"
        assert row["nickname"] == "名片"

    @pytest.mark.asyncio
    async def test_forward_message_excluded_entirely(self, wired):
        """合并转发整条不采集：含 forward 段的消息连计数都不进。"""
        segs = [
            {"type": "text", "data": {"text": "你看这个"}},
            {"type": "forward", "data": {"id": "abc"}},
        ]
        await handle_group_record(_group_event(segments=segs))
        assert wired.pending() == (0, 0)

    @pytest.mark.asyncio
    async def test_forward_json_card_excluded(self, wired):
        """json 卡片形态的合并转发同样排除（复用 media.extract_forward_id）。"""
        import json

        payload = {"app": "com.tencent.multimsg", "view": "Forward", "resid": "r1"}
        segs = [{"type": "json", "data": {"data": json.dumps(payload)}}]
        await handle_group_record(_group_event(segments=segs))
        assert wired.pending() == (0, 0)

    @pytest.mark.asyncio
    async def test_reply_message_counted_text_only(self, wired):
        """引用照常计入：用户自己的 text 进统计，被引内容不进。"""
        segs = [
            {"type": "reply", "data": {"id": "42"}},
            {"type": "text", "data": {"text": "确实如此"}},
        ]
        await handle_group_record(_group_event(segments=segs))
        assert wired.pending()[0] == 1

    @pytest.mark.asyncio
    async def test_stats_off_records_nothing(self, monkeypatch, store):
        monkeypatch.delenv("AGENT_GROUP_STATS", raising=False)
        monkeypatch.setenv("ALLOWED_GROUPS", "456")
        groupstats._collector = None
        await handle_group_record(_group_event())
        assert groupstats.collector() is None

    @pytest.mark.asyncio
    async def test_context_and_stats_are_independent(self, monkeypatch, store):
        """context 关掉不影响统计（两个开关解耦，锁死挂点重构）。"""
        from plugins.qq_agent_adapter.group_context import group_context

        # group_context 是模块级单例：前几条用例可能往 456 写过，先清干净
        group_context.clear("456")
        monkeypatch.setenv("AGENT_GROUP_STATS", "1")
        monkeypatch.setenv("AGENT_GROUP_CONTEXT", "0")
        monkeypatch.setenv("ALLOWED_GROUPS", "456")
        collector = groupstats.configure(store)
        await handle_group_record(_group_event())
        assert collector.pending()[0] == 1
        assert group_context.snapshot("456") == []  # 上下文确实没记
        groupstats._collector = None


# ------------------------------------------------------- 命令


class TestCommands:
    @pytest.mark.asyncio
    async def test_acl_denied(self, monkeypatch):
        from plugins.qq_agent_adapter import acl

        monkeypatch.setattr(acl, "ALLOWED_GROUPS", {"other"})
        monkeypatch.setattr(stats_route, "_get_driver", lambda: _FakeDriver())
        m = _FakeMatcher()
        with pytest.raises(FinishedException):
            await stats_route._respond(_group_event(), m, "rank")
        assert "权限" in str(m.finished)

    @pytest.mark.asyncio
    async def test_private_rejected(self, monkeypatch):
        from plugins.qq_agent_adapter import acl

        # 用 superuser（10000，见 nb init）过 ACL 私聊闸，才能走到"私聊不服务"分支
        monkeypatch.setattr(acl, "ALLOWED_GROUPS", {"456"})
        monkeypatch.setattr(stats_route, "_get_driver", lambda: _FakeDriver())
        m = _FakeMatcher()
        with pytest.raises(FinishedException):
            await stats_route._respond(_private_event(user_id=10000), m, "rank")
        assert "私聊" in str(m.finished)

    @pytest.mark.asyncio
    async def test_not_enabled_message(self, monkeypatch):
        from plugins.qq_agent_adapter import acl

        monkeypatch.setattr(acl, "ALLOWED_GROUPS", {"456"})
        monkeypatch.setattr(stats_route, "_get_driver", lambda: _FakeDriver(None))
        m = _FakeMatcher()
        with pytest.raises(FinishedException):
            await stats_route._respond(_group_event(), m, "rank")
        assert "未开启" in str(m.finished)

    @pytest.mark.asyncio
    async def test_cooldown_blocks_second_call(self, monkeypatch, store):
        from plugins.qq_agent_adapter import acl

        monkeypatch.setattr(acl, "ALLOWED_GROUPS", {"456"})
        monkeypatch.setenv("AGENT_GROUP_STATS", "1")
        monkeypatch.setattr(stats_route, "_get_driver", lambda: _FakeDriver(store))
        monkeypatch.setattr(groupstats, "cooldown", lambda: 30)
        stats_route._cooldowns.clear()
        m1, m2 = _FakeMatcher(), _FakeMatcher()
        with pytest.raises(FinishedException):
            await stats_route._respond(_group_event(), m1, "rank")
        with pytest.raises(FinishedException):
            await stats_route._respond(_group_event(), m2, "rank")
        assert "最多刷新一次" in str(m2.finished)
        assert m1.finished != m2.finished  # 第一次走了正常分支（数据不足文案）

    @pytest.mark.asyncio
    async def test_insufficient_data_message(self, monkeypatch, store):
        from plugins.qq_agent_adapter import acl

        monkeypatch.setattr(acl, "ALLOWED_GROUPS", {"456"})
        monkeypatch.setenv("AGENT_GROUP_STATS", "1")
        monkeypatch.setattr(stats_route, "_get_driver", lambda: _FakeDriver(store))
        stats_route._cooldowns.clear()
        m = _FakeMatcher()
        with pytest.raises(FinishedException):
            await stats_route._respond(_group_event(), m, "rank")
        assert "太少" in str(m.finished)

    @pytest.mark.asyncio
    async def test_rank_sends_image(self, monkeypatch, store):
        from plugins.qq_agent_adapter import acl

        monkeypatch.setattr(acl, "ALLOWED_GROUPS", {"456"})
        monkeypatch.setenv("AGENT_GROUP_STATS", "1")
        monkeypatch.setattr(stats_route, "_get_driver", lambda: _FakeDriver(store))
        monkeypatch.setattr(groupstats, "cooldown", lambda: 0)
        stats_route._cooldowns.clear()
        await _seed_week(store, "456", users=4, msgs=30)
        m = _FakeMatcher()
        with pytest.raises(FinishedException):
            await stats_route._respond(_group_event(), m, "rank")
        seg = m.finished
        assert seg is not None and seg.type == "image"
        assert str(seg.data.get("file", "")).startswith("base64://")

    @pytest.mark.asyncio
    async def test_cloud_sends_image(self, monkeypatch, store):
        from plugins.qq_agent_adapter import acl

        monkeypatch.setattr(acl, "ALLOWED_GROUPS", {"456"})
        monkeypatch.setenv("AGENT_GROUP_STATS", "1")
        monkeypatch.setattr(stats_route, "_get_driver", lambda: _FakeDriver(store))
        monkeypatch.setattr(groupstats, "cooldown", lambda: 0)
        stats_route._cooldowns.clear()
        await _seed_week(store, "456", users=4, msgs=30)
        m = _FakeMatcher()
        with pytest.raises(FinishedException):
            await stats_route._respond(_group_event(), m, "cloud")
        assert m.finished.type == "image"

    @pytest.mark.asyncio
    async def test_this_week_window(self, monkeypatch, store):
        """「/发言榜 本周」用本周窗口（周一~今天），数据落在本周才出图。"""
        from plugins.qq_agent_adapter import acl

        monkeypatch.setattr(acl, "ALLOWED_GROUPS", {"456"})
        monkeypatch.setenv("AGENT_GROUP_STATS", "1")
        monkeypatch.setattr(stats_route, "_get_driver", lambda: _FakeDriver(store))
        monkeypatch.setattr(groupstats, "cooldown", lambda: 0)
        stats_route._cooldowns.clear()
        await _seed_week(store, "456", users=4, msgs=30, window="current")
        m = _FakeMatcher()
        with pytest.raises(FinishedException):
            await stats_route._respond(_group_event("发言榜 本周"), m, "rank")
        assert m.finished.type == "image"

    def test_wants_current_week_parsing(self):
        assert stats_route._wants_current_week(_group_event("发言榜 本周")) is True
        assert stats_route._wants_current_week(_group_event("发言榜")) is False
        assert stats_route._wants_current_week(_group_event("发言榜 上周")) is False


class TestPlainStatsTrigger:
    """免前缀入口：群里直接打「词云 / 发言榜」也出图（2026-10-26）。

    NoneBot 命令匹配是 command_start + 命令名的前缀 Trie（默认 "/"），裸
    「词云」不触发 on_command；这里锁 on_message 精确匹配入口的三条性质：
    整条相等才命中、前置条件不满足静默放行、出了图就走完整 ACL/冷却链。
    """

    def test_plain_kind_matrix(self):
        assert stats_route._plain_kind(_group_event("词云")) == "cloud"
        assert stats_route._plain_kind(_group_event("词云。")) == "cloud"
        assert stats_route._plain_kind(_group_event(" 词云 ")) == "cloud"
        assert stats_route._plain_kind(_group_event("词云本周")) == "cloud"
        assert stats_route._plain_kind(_group_event("发言榜 这周")) == "rank"
        # 非整条相等 → 不当命令，继续走聊天
        for text in ("来一个词云", "词云图", "发言榜呢", "看看词云和发言榜", ""):
            assert stats_route._plain_kind(_group_event(text)) is None, text

    def test_rule_excludes_private_and_self(self):
        assert stats_route._plain_stats_rule(_group_event("词云")) is True
        assert stats_route._plain_stats_rule(_private_event()) is False
        assert (
            stats_route._plain_stats_rule(
                _group_event("词云", self_id=123, user_id=123)
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_plain_cloud_sends_image(self, monkeypatch, store):
        from plugins.qq_agent_adapter import acl

        monkeypatch.setattr(acl, "ALLOWED_GROUPS", {"456"})
        monkeypatch.setenv("AGENT_GROUP_STATS", "1")
        monkeypatch.setattr(stats_route, "_get_driver", lambda: _FakeDriver(store))
        monkeypatch.setattr(groupstats, "cooldown", lambda: 0)
        stats_route._cooldowns.clear()
        await _seed_week(store, "456", users=4, msgs=30)
        m = _FakeMatcher()
        monkeypatch.setattr(stats_route, "plain_stats_cmd", m)
        with pytest.raises(FinishedException):
            await stats_route.handle_plain_stats(_group_event("词云"))
        assert m.finished.type == "image"

    @pytest.mark.asyncio
    async def test_plain_silent_when_stats_disabled(self, monkeypatch, store):
        """统计没开时「词云」只是普通词：静默放行给聊天，不 finish。"""
        from plugins.qq_agent_adapter import acl

        monkeypatch.setattr(acl, "ALLOWED_GROUPS", {"456"})
        monkeypatch.setenv("AGENT_GROUP_STATS", "0")
        monkeypatch.setattr(stats_route, "_get_driver", lambda: _FakeDriver(store))
        m = _FakeMatcher()
        monkeypatch.setattr(stats_route, "plain_stats_cmd", m)
        await stats_route.handle_plain_stats(_group_event("词云"))
        assert m.finished is None

    @pytest.mark.asyncio
    async def test_plain_silent_when_not_allowed(self, monkeypatch, store):
        """无权限群不因出现「词云」二字就被拒绝刷屏（静默回落聊天）。"""
        from plugins.qq_agent_adapter import acl

        monkeypatch.setattr(acl, "ALLOWED_GROUPS", {"other"})
        monkeypatch.setenv("AGENT_GROUP_STATS", "1")
        monkeypatch.setattr(stats_route, "_get_driver", lambda: _FakeDriver(store))
        m = _FakeMatcher()
        monkeypatch.setattr(stats_route, "plain_stats_cmd", m)
        await stats_route.handle_plain_stats(_group_event("词云"))
        assert m.finished is None


# ------------------------------------------------------- 周报


class TestWeeklyReport:
    @pytest.mark.asyncio
    async def test_sends_to_qualifying_groups(self, monkeypatch, store):
        from plugins.qq_agent_adapter import acl

        sink = _RecordingSink()
        monkeypatch.setattr(acl, "ALLOWED_GROUPS", {"456"})
        monkeypatch.setattr(stats_route, "ALLOWED_GROUPS", {"456"})
        monkeypatch.setattr(
            stats_route, "_get_driver", lambda: _FakeDriver(store, sink)
        )
        monkeypatch.setattr(groupstats, "enabled", lambda: True)
        monkeypatch.setattr(groupstats, "min_msgs", lambda: 20)
        monkeypatch.setattr(groupstats, "weekly_groups", lambda: frozenset({"456"}))
        await _seed_week(store, "456", users=4, msgs=30)
        await stats_route.weekly_report()
        # 榜图 + 词云两条，都发到 group:456
        assert len(sink.sent) == 2
        assert all(t == "group:456" for t, _ in sink.sent)
        assert all(m.type == "image" for _, m in sink.sent)

    @pytest.mark.asyncio
    async def test_skips_group_below_threshold(self, monkeypatch, store):
        from plugins.qq_agent_adapter import acl

        sink = _RecordingSink()
        monkeypatch.setattr(acl, "ALLOWED_GROUPS", {"456"})
        monkeypatch.setattr(stats_route, "ALLOWED_GROUPS", {"456"})
        monkeypatch.setattr(
            stats_route, "_get_driver", lambda: _FakeDriver(store, sink)
        )
        monkeypatch.setattr(groupstats, "enabled", lambda: True)
        monkeypatch.setattr(groupstats, "min_msgs", lambda: 20)
        monkeypatch.setattr(groupstats, "weekly_groups", lambda: frozenset({"456"}))
        await _seed_week(store, "456", users=4, msgs=5)  # 不足 20 条
        await stats_route.weekly_report()
        assert sink.sent == []

    @pytest.mark.asyncio
    async def test_skips_group_not_in_acl(self, monkeypatch, store):
        sink = _RecordingSink()
        monkeypatch.setattr(stats_route, "ALLOWED_GROUPS", {"999"})
        monkeypatch.setattr(
            stats_route, "_get_driver", lambda: _FakeDriver(store, sink)
        )
        monkeypatch.setattr(groupstats, "enabled", lambda: True)
        monkeypatch.setattr(groupstats, "min_msgs", lambda: 1)
        monkeypatch.setattr(groupstats, "weekly_groups", lambda: frozenset({"456"}))
        await _seed_week(store, "456", users=4, msgs=30)
        await stats_route.weekly_report()
        assert sink.sent == []

    @pytest.mark.asyncio
    async def test_disabled_noop(self, monkeypatch, store):
        sink = _RecordingSink()
        monkeypatch.setattr(
            stats_route, "_get_driver", lambda: _FakeDriver(store, sink)
        )
        monkeypatch.setattr(groupstats, "enabled", lambda: False)
        await stats_route.weekly_report()
        assert sink.sent == []

    @pytest.mark.asyncio
    async def test_one_group_failure_does_not_block_others(self, monkeypatch, store):
        """单群发送失败只记日志，后续群照发（不重试刷屏，下周一自然重试）。"""
        sink = _RecordingSink()
        calls = {"n": 0}

        async def flaky(target, message):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("send failed")
            return "sent"

        sink.send_once = flaky
        monkeypatch.setattr(stats_route, "ALLOWED_GROUPS", {"456", "789"})
        monkeypatch.setattr(
            stats_route, "_get_driver", lambda: _FakeDriver(store, sink)
        )
        monkeypatch.setattr(groupstats, "enabled", lambda: True)
        monkeypatch.setattr(groupstats, "min_msgs", lambda: 1)
        monkeypatch.setattr(
            groupstats, "weekly_groups", lambda: frozenset({"456", "789"})
        )
        await _seed_week(store, "456", users=4, msgs=30)
        await _seed_week(store, "789", users=4, msgs=30)
        await stats_route.weekly_report()  # 不抛异常即通过
        assert calls["n"] >= 3  # 456 榜图失败后，其余仍继续尝试


# ------------------------------------------------------- 辅助


def _private_event(user_id: int = 123):
    from nonebot.adapters.onebot.v11 import PrivateMessageEvent

    return PrivateMessageEvent.parse_obj(
        {
            "time": 0,
            "self_id": 999,
            "post_type": "message",
            "sub_type": "friend",
            "user_id": user_id,
            "message_type": "private",
            "message_id": 1,
            "message": [{"type": "text", "data": {"text": "发言榜"}}],
            "original_message": [{"type": "text", "data": {"text": "发言榜"}}],
            "raw_message": "发言榜",
            "font": 0,
            "sender": {"user_id": user_id, "nickname": "昵称"},
            "to_me": False,
            "reply": None,
        }
    )


async def _seed_week(
    store, group_id: str, *, users: int, msgs: int, window: str = "prev"
):
    """给一个群的某个周窗灌 users 个用户、总 msgs 条（含词频）。"""
    if window == "prev":
        start, end = ranking.previous_week()
    else:
        start, end = ranking.current_week()
    per = max(1, msgs // users)
    rows = []
    for i in range(users):
        rows.append(
            {
                "group_id": group_id,
                "user_id": f"u{i}",
                "day": start,
                "nickname": f"群友{i}",
                "msgs": per,
                "chars": per * 10,
                "hours": {19: per},
            }
        )
    await store.upsert_daily(rows)
    await store.upsert_words(
        [
            # 旧口径落库的虚词/通用词：读取时必须被内容词过滤掉
            {"group_id": group_id, "day": start, "word": "就是", "count": per * 3},
            {"group_id": group_id, "day": start, "word": "东西", "count": per * 2},
            {"group_id": group_id, "day": start, "word": "服务器", "count": per},
            {"group_id": group_id, "day": start, "word": "显卡", "count": per // 2 + 1},
            {"group_id": group_id, "day": start, "word": "老吴", "count": per // 3 + 1},
        ]
    )
    assert end >= start


# ------------------------------------------------------- 周报白名单（增量）


class TestWeeklyWhitelist:
    """周报推送白名单：空=不推（fail-closed）、只推白名单、∩ALLOWED_GROUPS。"""

    @pytest.mark.asyncio
    async def test_empty_whitelist_pushes_nothing(self, monkeypatch, store):
        """空 = 不推，且**不查库**（fail-closed + 零开销）。"""
        sink = _RecordingSink()
        monkeypatch.setattr(stats_route, "ALLOWED_GROUPS", {"456"})
        monkeypatch.setattr(
            stats_route, "_get_driver", lambda: _FakeDriver(store, sink)
        )
        monkeypatch.setattr(groupstats, "enabled", lambda: True)
        monkeypatch.setattr(groupstats, "min_msgs", lambda: 1)
        monkeypatch.setattr(groupstats, "weekly_groups", lambda: frozenset())
        await _seed_week(store, "456", users=4, msgs=30)

        queried = []

        async def spy(*a, **kw):
            queried.append(1)
            return await _orig_group_totals(*a, **kw)

        _orig_group_totals = store.group_totals
        monkeypatch.setattr(store, "group_totals", spy)
        await stats_route.weekly_report()
        assert sink.sent == []
        assert queried == []  # 白名单为空时连 group_totals 都不查

    @pytest.mark.asyncio
    async def test_only_whitelisted_groups_get_push(self, monkeypatch, store):
        from plugins.qq_agent_adapter import acl

        sink = _RecordingSink()
        monkeypatch.setattr(acl, "ALLOWED_GROUPS", {"456", "789"})
        monkeypatch.setattr(stats_route, "ALLOWED_GROUPS", {"456", "789"})
        monkeypatch.setattr(
            stats_route, "_get_driver", lambda: _FakeDriver(store, sink)
        )
        monkeypatch.setattr(groupstats, "enabled", lambda: True)
        monkeypatch.setattr(groupstats, "min_msgs", lambda: 1)
        monkeypatch.setattr(groupstats, "weekly_groups", lambda: frozenset({"456"}))
        await _seed_week(store, "456", users=4, msgs=30)
        await _seed_week(store, "789", users=4, msgs=30)
        await stats_route.weekly_report()
        assert {t for t, _ in sink.sent} == {"group:456"}

    @pytest.mark.asyncio
    async def test_whitelisted_but_not_in_acl_skipped_with_warning(
        self, monkeypatch, store, caplog
    ):
        from plugins.qq_agent_adapter import acl

        sink = _RecordingSink()
        monkeypatch.setattr(acl, "ALLOWED_GROUPS", {"456"})
        monkeypatch.setattr(stats_route, "ALLOWED_GROUPS", {"456"})
        monkeypatch.setattr(
            stats_route, "_get_driver", lambda: _FakeDriver(store, sink)
        )
        monkeypatch.setattr(groupstats, "enabled", lambda: True)
        monkeypatch.setattr(groupstats, "min_msgs", lambda: 1)
        monkeypatch.setattr(
            groupstats, "weekly_groups", lambda: frozenset({"456", "789"})
        )
        with caplog.at_level("WARNING"):
            await _seed_week(store, "789", users=4, msgs=30)
            await stats_route.weekly_report()
        assert sink.sent == []
        assert "ALLOWED_GROUPS" in caplog.text

    @pytest.mark.asyncio
    async def test_whitelist_does_not_gate_commands(self, monkeypatch, store):
        """白名单只管推送：白名单外的群 /发言榜 照常出图（采集≠推送）。"""
        from plugins.qq_agent_adapter import acl

        monkeypatch.setattr(acl, "ALLOWED_GROUPS", {"456", "789"})
        monkeypatch.setattr(stats_route, "_get_driver", lambda: _FakeDriver(store))
        monkeypatch.setenv("AGENT_GROUP_STATS", "1")
        monkeypatch.setattr(groupstats, "cooldown", lambda: 0)
        monkeypatch.setattr(groupstats, "weekly_groups", lambda: frozenset({"456"}))
        stats_route._cooldowns.clear()
        await _seed_week(store, "789", users=4, msgs=30)
        m = _FakeMatcher()
        with pytest.raises(FinishedException):
            await stats_route._respond(_group_event(group_id=789), m, "rank")
        assert m.finished.type == "image"


# ------------------------------------------------------- 个人点评 skill


class _FakeLLM:
    def __init__(self, content="锐评内容", raises=None):
        self.content = content
        self.raises = raises
        self.calls = []

    async def chat(self, messages, max_tokens=None):
        self.calls.append(messages)
        if self.raises:
            raise self.raises
        return {"choices": [{"message": {"content": self.content}}]}


class TestReviewSkill:
    @pytest.fixture
    def registry(self):
        from agentcore.skills.permissions import PermissionChecker
        from agentcore.skills.registry import SkillRegistry

        return SkillRegistry(
            permission_checker=PermissionChecker(
                superusers={"10000"}, default_permission="public"
            )
        )

    @pytest.fixture
    def wired(self, monkeypatch, store):
        """返回 (llm, sink)；已接管 driver 与开关，调用方负责注册 skill。"""
        llm = _FakeLLM()
        sink = _RecordingSink()
        monkeypatch.setattr(
            stats_route, "_get_driver", lambda: _FakeDriver(store, sink)
        )
        monkeypatch.setenv("AGENT_GROUP_STATS", "1")
        monkeypatch.setattr(groupstats, "review_cooldown", lambda: 0)
        return llm, sink

    async def _seed_user(self, store, msgs=15):
        """给 456 群灌一个 msgs 条的用户 + 两个垫背用户（排名用）。"""
        from agentcore.groupstats import ranking

        start, _ = ranking.previous_week()
        await store.upsert_daily(
            [
                {
                    "group_id": "456",
                    "user_id": "u1",
                    "day": start,
                    "nickname": "群友甲",
                    "msgs": msgs,
                    "chars": msgs * 8,
                    "hours": {23: msgs},
                },
                {
                    "group_id": "456",
                    "user_id": "u2",
                    "day": start,
                    "nickname": "群友乙",
                    "msgs": 5,
                    "chars": 40,
                    "hours": {20: 5},
                },
                {
                    "group_id": "456",
                    "user_id": "u3",
                    "day": start,
                    "nickname": "群友丙",
                    "msgs": 2,
                    "chars": 16,
                    "hours": {9: 2},
                },
            ]
        )
        await store.upsert_words(
            [
                {"group_id": "456", "day": start, "word": "服务器", "count": 9},
                {"group_id": "456", "day": start, "word": "显卡", "count": 4},
            ]
        )
        return start

    @pytest.mark.asyncio
    async def test_schema_has_no_user_param(self, registry, wired, store):
        """越权面归零：schema 不得出现 user_id（只能看自己）。"""
        llm, _ = wired
        stats_route.register_group_review_skill(registry, store, llm)
        schema = registry.skills["my_group_review"].params_schema
        assert "user_id" not in (schema.get("properties") or {})
        assert schema.get("required") == []

    @pytest.mark.asyncio
    async def test_insufficient_data_no_llm(self, registry, wired, store):
        """低于 10 条：回文案、不调 LLM、不出卡（恰好 10 条会评）。"""
        llm, sink = wired
        stats_route.register_group_review_skill(registry, store, llm)
        await self._seed_user(store, msgs=9)
        out = await registry.execute("my_group_review", user_id="u1", group_id="456")
        assert "发言较少" in out and "咱不评价" in out
        assert llm.calls == [] and sink.sent == []

    @pytest.mark.asyncio
    async def test_generate_then_cache_zero_llm(self, registry, wired, store):
        """首次生成并落库；二次查看命中缓存、零 LLM 调用。"""
        llm, sink = wired
        stats_route.register_group_review_skill(registry, store, llm)
        start = await self._seed_user(store, msgs=15)
        await registry.execute("my_group_review", user_id="u1", group_id="456")
        assert len(llm.calls) == 1
        assert len(sink.sent) == 1 and sink.sent[0][1].type == "image"
        row = await store.get_review("456", start, "u1")
        assert row["text"] == "锐评内容"
        assert row["stats"]["msgs"] == 15 and row["stats"]["rank"] == 1

        out2 = await registry.execute("my_group_review", user_id="u1", group_id="456")
        assert len(llm.calls) == 1  # 缓存命中，没再调
        assert len(sink.sent) == 2  # 卡片照发
        assert "看图" in out2

    @pytest.mark.asyncio
    async def test_private_chat_rejected(self, registry, wired, store):
        llm, sink = wired
        stats_route.register_group_review_skill(registry, store, llm)
        out = await registry.execute("my_group_review", user_id="u1", group_id=None)
        assert "群功能" in out
        assert llm.calls == [] and sink.sent == []

    @pytest.mark.asyncio
    async def test_disabled_returns_text(self, registry, monkeypatch, store):
        llm = _FakeLLM()
        sink = _RecordingSink()
        monkeypatch.setattr(
            stats_route, "_get_driver", lambda: _FakeDriver(store, sink)
        )
        monkeypatch.delenv("AGENT_GROUP_STATS", raising=False)
        stats_route.register_group_review_skill(registry, store, llm)
        out = await registry.execute("my_group_review", user_id="u1", group_id="456")
        assert "未开启" in out
        assert llm.calls == [] and sink.sent == []

    @pytest.mark.asyncio
    async def test_cooldown_blocks_second_trigger(self, registry, monkeypatch, store):
        llm = _FakeLLM()
        sink = _RecordingSink()
        monkeypatch.setattr(
            stats_route, "_get_driver", lambda: _FakeDriver(store, sink)
        )
        monkeypatch.setenv("AGENT_GROUP_STATS", "1")
        monkeypatch.setattr(groupstats, "review_cooldown", lambda: 30)
        stats_route._review_cooldowns.clear()
        stats_route.register_group_review_skill(registry, store, llm)
        await self._seed_user(store, msgs=15)
        await registry.execute("my_group_review", user_id="u1", group_id="456")
        out = await registry.execute("my_group_review", user_id="u1", group_id="456")
        assert "最多一次" in out
        assert len(llm.calls) == 1  # 第二次被冷却挡住，没花 LLM

    @pytest.mark.asyncio
    async def test_sink_failure_falls_back_to_text(self, registry, wired, store):
        """发送 failed → 降级纯文本（内容不丢）。"""
        llm, sink = wired
        stats_route.register_group_review_skill(registry, store, llm)
        await self._seed_user(store, msgs=15)

        async def failed(target, message):
            return "failed"

        sink.send_once = failed
        out = await registry.execute("my_group_review", user_id="u1", group_id="456")
        assert "你的群周报" in out and "锐评：锐评内容" in out

    @pytest.mark.asyncio
    async def test_uncertain_does_not_resend_text(self, registry, wired, store):
        """uncertain → 不再发文本副本（避免内容重复投递）。"""
        llm, sink = wired
        stats_route.register_group_review_skill(registry, store, llm)
        await self._seed_user(store, msgs=15)
        sent = []

        async def uncertain(target, message):
            sent.append(target)
            return "uncertain"

        sink.send_once = uncertain
        out = await registry.execute("my_group_review", user_id="u1", group_id="456")
        assert len(sent) == 1
        assert "不确定" in out
        assert "锐评：锐评内容" not in out  # 没有发第二份内容

    @pytest.mark.asyncio
    async def test_llm_failure_still_sends_facts_card_no_cache(
        self, registry, monkeypatch, store
    ):
        """LLM 挂了也出事实卡；且不落缓存（下次允许重试）。"""
        llm = _FakeLLM(raises=RuntimeError("llm down"))
        sink = _RecordingSink()
        monkeypatch.setattr(
            stats_route, "_get_driver", lambda: _FakeDriver(store, sink)
        )
        monkeypatch.setenv("AGENT_GROUP_STATS", "1")
        monkeypatch.setattr(groupstats, "review_cooldown", lambda: 0)
        stats_route.register_group_review_skill(registry, store, llm)
        start = await self._seed_user(store, msgs=15)
        out = await registry.execute("my_group_review", user_id="u1", group_id="456")
        assert "看图" in out
        assert len(sink.sent) == 1  # 事实卡仍发出
        assert await store.get_review("456", start, "u1") is None  # 未缓存

    @pytest.mark.asyncio
    async def test_budget_gate_facts_card_no_llm(self, registry, monkeypatch, store):
        """预算熔断：不调 LLM、出事实卡、不缓存（闸门内快速返回）。"""
        from agentcore.groupstats import review as review_mod

        class _BlockedBudget:
            def chat_blocked(self):
                return True, "cap"

        monkeypatch.setattr(review_mod, "get_budget", lambda: _BlockedBudget())
        llm = _FakeLLM()
        sink = _RecordingSink()
        monkeypatch.setattr(
            stats_route, "_get_driver", lambda: _FakeDriver(store, sink)
        )
        monkeypatch.setenv("AGENT_GROUP_STATS", "1")
        monkeypatch.setattr(groupstats, "review_cooldown", lambda: 0)
        stats_route.register_group_review_skill(registry, store, llm)
        await self._seed_user(store, msgs=15)
        out = await registry.execute("my_group_review", user_id="u1", group_id="456")
        assert "看图" in out
        assert llm.calls == []
        assert len(sink.sent) == 1


# ------------------------------------------------------- 归档增强（G3）


class _FakeArchive:
    """假原文归档 store：记录调用次数，可编排数据/失败。"""

    def __init__(self, texts=None, kind_counts=None, reply=0, boom=False):
        self._texts = texts if texts is not None else []
        self._kinds = kind_counts or {}
        self._reply = reply
        self._boom = boom
        self.calls: list[str] = []

    async def user_texts(self, gid, uid, start, end, limit):
        self.calls.append("user_texts")
        if self._boom:
            raise RuntimeError("archive down")
        return self._texts

    async def kind_count(self, gid, uid, start, end, kind):
        self.calls.append(f"kind:{kind}")
        if self._boom:
            raise RuntimeError("archive down")
        return self._kinds.get(kind, 0)

    async def reply_count(self, gid, ids, start, end):
        self.calls.append("reply_count")
        if self._boom:
            raise RuntimeError("archive down")
        return self._reply


def _archive_driver(store, sink, archive):
    return _FakeDriver(store, sink, archive=archive)


class _FakeDriverWithArchive(_FakeDriver):
    def __init__(self, store=None, sink=None, archive=None):
        super().__init__(store, sink)
        self._agent_group_archive_store = archive


class TestReviewArchiveEnrichment(TestReviewSkill):
    """继承 TestReviewSkill 的 registry/wired/_seed_user。"""

    @pytest.mark.asyncio
    async def test_five_facts_in_prompt_and_snapshot(
        self, registry, monkeypatch, store
    ):
        """AC D19：归档开启时 5 项增强全进 prompt，并随快照落库。"""
        from agentcore.groupstats import ranking

        llm = _FakeLLM()
        sink = _RecordingSink()
        start, _ = ranking.previous_week()
        archive = _FakeArchive(
            texts=[
                {"message_id": "m2", "ts": start.toordinal() + 100, "text": "嘻嘻嘻嘻"},
                {
                    "message_id": "m1",
                    "ts": start.toordinal() + 50,
                    "text": "服务器又炸了",
                },
                {
                    "message_id": "m3",
                    "ts": start.toordinal() + 10,
                    "text": "我们觉得这个",
                },
            ],
            kind_counts={"forward": 23, "image": 12},
            reply=4,
        )
        monkeypatch.setattr(
            stats_route,
            "_get_driver",
            lambda: _FakeDriverWithArchive(store, sink, archive),
        )
        monkeypatch.setenv("AGENT_GROUP_STATS", "1")
        monkeypatch.setattr(groupstats, "review_cooldown", lambda: 0)
        stats_route.register_group_review_skill(registry, store, llm)
        await self._seed_user(store, msgs=15)

        await registry.execute("my_group_review", user_id="u1", group_id="456")
        prompt = llm.calls[0][1]["content"]
        # own_words：嘻嘻/服务器各 1 次，按 (-次数, 词) 排序
        assert "本人本周高频词：嘻嘻、服务器" in prompt
        for marker in (
            "合并转发次数：23 次",
            "发图次数：12 条",
            "消息被引用次数：4 次",
        ):
            assert marker in prompt, marker
        # quote：「服务器又炸了」与「我们觉得这个」等长（6 字）→ 取最早 ts 的
        assert "代表性原句：「我们觉得这个」" in prompt
        # 停用词过滤与词云同口径（own_words 行内不含"我们"）
        own_line = prompt.split("本人本周高频词：")[1].split("\n")[0]
        assert "我们" not in own_line
        # 快照落库含增强字段（缓存命中可直接渲染）
        row = await store.get_review("456", start, "u1")
        assert row["stats"]["forwards"] == 23
        assert row["stats"]["own_words"] == ["嘻嘻", "服务器"]
        assert row["stats"]["quote"] == "我们觉得这个"
        # 查询次数有界（无 N+1 / 无全表扫描）
        assert len(archive.calls) <= 5

    @pytest.mark.asyncio
    async def test_quote_picks_earliest_among_longest(
        self, registry, monkeypatch, store
    ):
        """确定性：等长取最早 ts（AC D19 tie-break）。"""
        from agentcore.groupstats import ranking

        llm = _FakeLLM()
        sink = _RecordingSink()
        start, _ = ranking.previous_week()
        archive = _FakeArchive(
            texts=[
                # ts 降序：等长时 >= 更新 ⇒ 最终留最早的那条
                {
                    "message_id": "new",
                    "ts": start.toordinal() + 999,
                    "text": "嘻嘻嘻嘻",
                },
                {"message_id": "old", "ts": start.toordinal() + 1, "text": "啊啊啊啊"},
            ],
            kind_counts={},
            reply=0,
        )
        monkeypatch.setattr(
            stats_route,
            "_get_driver",
            lambda: _FakeDriverWithArchive(store, sink, archive),
        )
        monkeypatch.setenv("AGENT_GROUP_STATS", "1")
        monkeypatch.setattr(groupstats, "review_cooldown", lambda: 0)
        stats_route.register_group_review_skill(registry, store, llm)
        await self._seed_user(store, msgs=15)
        await registry.execute("my_group_review", user_id="u1", group_id="456")
        prompt = llm.calls[0][1]["content"]
        assert "代表性原句：「啊啊啊啊」" in prompt

    @pytest.mark.asyncio
    async def test_no_archive_no_enhancement_lines(self, registry, monkeypatch, store):
        """AC D21：归档关闭时 prompt 无任何增强行。"""
        llm = _FakeLLM()
        sink = _RecordingSink()
        monkeypatch.setattr(
            stats_route, "_get_driver", lambda: _FakeDriver(store, sink)
        )
        monkeypatch.setenv("AGENT_GROUP_STATS", "1")
        monkeypatch.setattr(groupstats, "review_cooldown", lambda: 0)
        stats_route.register_group_review_skill(registry, store, llm)
        await self._seed_user(store, msgs=15)
        await registry.execute("my_group_review", user_id="u1", group_id="456")
        prompt = llm.calls[0][1]["content"]
        for marker in ("高频词", "转发次数", "发图次数", "被引用次数", "代表性原句"):
            assert marker not in prompt

    @pytest.mark.asyncio
    async def test_archive_failure_degrades_to_base(self, registry, monkeypatch, store):
        """归档查询炸了：退回基础事实，点评照常出卡（增益不拖垮主流程）。"""
        llm = _FakeLLM()
        sink = _RecordingSink()
        archive = _FakeArchive(boom=True)
        monkeypatch.setattr(
            stats_route,
            "_get_driver",
            lambda: _FakeDriverWithArchive(store, sink, archive),
        )
        monkeypatch.setenv("AGENT_GROUP_STATS", "1")
        monkeypatch.setattr(groupstats, "review_cooldown", lambda: 0)
        stats_route.register_group_review_skill(registry, store, llm)
        await self._seed_user(store, msgs=15)
        out = await registry.execute("my_group_review", user_id="u1", group_id="456")
        assert "看图" in out
        assert len(llm.calls) == 1  # 基础事实仍然生成了
        prompt = llm.calls[0][1]["content"]
        assert "高频词" not in prompt and "转发次数" not in prompt


# ------------------------------------------------------- H2：渲染卸载线程


class TestRenderOffEventLoop:
    """REVIEW-1e0f5ae..a0a6bb4 H2：三处 PIL 渲染必须卸载出事件循环。

    旧实现同步调用 render_*_png：80 词词云实测冻结事件循环 853ms（心跳
    50ms），群友可用 /发言榜 /词云 /群评 直接触发、周报 cron 放大。行为级
    断言：渲染函数运行的线程 ident ≠ 事件循环线程 ident；同步渲染期间
    心跳任务仍在跳动。
    """

    def _spy_render(self, monkeypatch, name, ret=None, delay=0.0):
        """把渲染函数换成记录执行线程 ident 的替身（参数纯数据，替换安全）。"""
        import threading
        import time as _time

        idents: list[int] = []

        def fake(*a, **k):
            idents.append(threading.get_ident())
            if delay:
                _time.sleep(delay)
            return ret if ret is not None else b"png"

        monkeypatch.setattr(stats_route, name, fake)
        return idents

    @pytest.mark.asyncio
    async def test_cloud_render_runs_off_loop(self, monkeypatch, store):
        await _seed_week(store, "456", users=4, msgs=30)
        idents = self._spy_render(monkeypatch, "render_cloud_png")
        loop_ident = threading_ident()
        png, err = await stats_route._cloud_png(
            store, "456", *ranking.previous_week(), title="t"
        )
        assert png == b"png" and err is None
        assert idents and idents[0] != loop_ident, "渲染必须不在事件循环线程上跑"

    @pytest.mark.asyncio
    async def test_rank_render_runs_off_loop(self, monkeypatch, store):
        await _seed_week(store, "456", users=4, msgs=30)
        idents = self._spy_render(monkeypatch, "render_rank_png")
        loop_ident = threading_ident()
        png, err = await stats_route._rank_png(
            store,
            "456",
            *ranking.previous_week(),
            title="t",
            min_users=0,
            min_msgs=0,
        )
        assert png == b"png" and err is None
        assert idents and idents[0] != loop_ident

    @pytest.mark.asyncio
    async def test_review_render_runs_off_loop(self, monkeypatch, store):
        sink = _RecordingSink()
        monkeypatch.setattr(
            stats_route, "_get_driver", lambda: _FakeDriver(store, sink)
        )
        idents = self._spy_render(monkeypatch, "render_review_png")
        loop_ident = threading_ident()
        start, end = ranking.previous_week()
        await stats_route._send_review_card("456", start, end, {"msgs": 5}, "评语")
        assert idents and idents[0] != loop_ident

    @pytest.mark.asyncio
    async def test_words_limit_reaches_render(self, monkeypatch, store):
        """L6（REVIEW-1e0f5ae..a0a6bb4）：AGENT_GROUP_STATS_WORDS 贯通到渲染。"""

        await _seed_week(store, "456", users=4, msgs=30)
        captured: dict = {}

        def fake_render(title, words, max_words=80):
            captured["max_words"] = max_words
            captured["n"] = len(words)
            return b"png"

        monkeypatch.setattr(stats_route, "render_cloud_png", fake_render)
        monkeypatch.setattr(groupstats, "words", lambda: 120)
        png, err = await stats_route._cloud_png(
            store, "456", *ranking.previous_week(), title="t"
        )
        assert png == b"png" and err is None
        assert captured["max_words"] == 120, "配置的词数上限必须传到渲染层"

    @pytest.mark.asyncio
    async def test_cloud_filters_stored_function_words(self, monkeypatch, store):
        """QQ 词云读取旧数据也要过滤虚词，不能只有 web 面过滤。"""
        await _seed_week(store, "456", users=4, msgs=30)
        captured: dict = {}

        def fake_render(title, words, max_words=80):
            captured["words"] = [w for w, _ in words]
            return b"png"

        monkeypatch.setattr(stats_route, "render_cloud_png", fake_render)
        png, err = await stats_route._cloud_png(
            store, "456", *ranking.previous_week(), title="t"
        )
        assert png == b"png" and err is None
        assert "服务器" in captured["words"]
        assert "就是" not in captured["words"]
        assert "东西" not in captured["words"]

    @pytest.mark.asyncio
    async def test_heartbeat_keeps_ticking_during_render(self, monkeypatch, store):
        """决定性证据（评审复现口径）：同步渲染期间心跳任务不再停跳。"""
        import asyncio

        await _seed_week(store, "456", users=4, msgs=30)
        # 渲染替身同步睡 0.3s：旧实现（循环内直接跑）会让心跳全程停跳
        self._spy_render(monkeypatch, "render_cloud_png", delay=0.3)
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        hb = asyncio.create_task(heartbeat())
        try:
            await stats_route._cloud_png(
                store, "456", *ranking.previous_week(), title="t"
            )
        finally:
            hb.cancel()
        # 0.3s / 0.02s ≈ 15 tick；渲染阻塞循环时为 0。宽松阈值防慢机抖动。
        assert ticks >= 3, f"渲染期间事件循环被冻结（ticks={ticks}）"


def threading_ident() -> int:
    import threading

    return threading.get_ident()
