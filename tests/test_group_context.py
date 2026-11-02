from agentcore.tz import day_key, fmt_ts
from agentcore.tz import now as tz_now
from plugins.qq_agent_adapter.group_context import (
    GroupContextBuffer,
    context_enabled,
    context_lines,
)


class TestGroupContextBuffer:
    def test_record_and_snapshot_order(self):
        buf = GroupContextBuffer(max_lines=10)
        buf.record("g1", "小明", "晚上一起打游戏吗", message_id="1")
        buf.record("g1", "小红", "我加班", message_id="2")
        rows = buf.snapshot("g1")
        assert [r["who"] for r in rows] == ["小明", "小红"]
        assert rows[0]["text"] == "晚上一起打游戏吗"

    def test_snapshot_rows_carry_time_same_day(self):
        """每行带发送时间（同日 HH:MM）——跨成员接话题时模型要能分清
        片段里谁在几分钟前说的（窗口调大到小时级后的必要标注）。"""
        buf = GroupContextBuffer(max_lines=10)
        moment = tz_now().replace(hour=14, minute=30, second=0, microsecond=0)
        wall = moment.timestamp()
        buf.record("g1", "小明", "晚上一起打游戏吗", message_id="1", wall=wall)
        rows = buf.snapshot("g1")
        assert rows[0]["time"] == fmt_ts(wall, "%H:%M")

    def test_snapshot_time_cross_day_uses_full_form(self):
        """跨日的片段行给 MM-DD HH:MM（与引擎历史前缀同款约定），
        裸 HH:MM 会被模型当成今天的时间（2026-10-26 教训）。"""
        buf = GroupContextBuffer(max_lines=10)
        wall = tz_now().timestamp() - 3 * 86400  # 三天前，必然跨日
        assert day_key(wall) != day_key()  # 前置：确实跨日
        if fmt_ts(wall, "%Y") != fmt_ts(tz_now().timestamp(), "%Y"):
            # 元旦前后跑测试时「三天前」跨了年——换成年内固定日期，
            # 保证本用例钉的是「跨日同年」分支（跨年分支另有专项用例）
            wall = tz_now().replace(month=2, day=1, hour=9, minute=5).timestamp()
            assert day_key(wall) != day_key()
        buf.record("g1", "小明", "很久以前的话", message_id="1", wall=wall)
        rows = buf.snapshot("g1")
        assert rows[0]["time"] == fmt_ts(wall, "%m-%d %H:%M")

    def test_snapshot_time_cross_year_includes_year(self):
        """M11（REVIEW-63f9444..858afcc）：跨年的片段行必须带年份。

        旧实现只有两档——跨年夜缓冲里的「12-31 23:50」渲染成裸 MM-DD，
        模型会当成今年的未来时间（引擎历史前缀 L7 同款事故，三档对齐）。
        """
        buf = GroupContextBuffer(max_lines=10)
        now = tz_now()
        wall = now.replace(
            year=now.year - 1, month=12, day=31, hour=23, minute=50
        ).timestamp()
        buf.record("g1", "小明", "去年跨年夜的话", message_id="1", wall=wall)
        rows = buf.snapshot("g1")
        assert rows[0]["time"] == fmt_ts(wall, "%Y-%m-%d %H:%M")
        assert rows[0]["time"].startswith(str(now.year - 1)), (
            "跨年时间必须带年份，否则会被当成今年的未来时间"
        )

    def test_snapshot_time_missing_wall_keeps_row(self):
        """异常路径没有 wall（理论不出现，防御旧数据）→ time 为空串，
        行本身不能丢。"""
        buf = GroupContextBuffer(max_lines=10)
        buf.record("g1", "小明", "没有墙钟时间", message_id="1")
        buf._data["g1"][0]["wall"] = None
        rows = buf.snapshot("g1")
        assert rows[0]["text"] == "没有墙钟时间" and rows[0]["time"] == ""

    def test_exclude_current_message(self):
        buf = GroupContextBuffer(max_lines=10)
        buf.record("g1", "小明", "他们在聊什么", message_id="9")
        buf.record("g1", "小红", "在聊游戏", message_id="10")
        rows = buf.snapshot("g1", exclude_message_id="10")
        assert [r["text"] for r in rows] == ["他们在聊什么"]

    def test_image_placeholder_and_skip_empty(self):
        buf = GroupContextBuffer(max_lines=10)
        buf.record("g1", "小明", "", message_id="1", has_image=True)
        buf.record("g1", "小红", "", message_id="2")  # 无正文无图 -> 不入
        rows = buf.snapshot("g1")
        assert len(rows) == 1
        assert rows[0]["text"] == "[图片]"

    def test_image_marker_kept_when_text_present(self):
        """图片消息常带一句「看这个」：不能因为正文非空就把图片信息丢掉。

        旧实现只在正文为空时写 ``[图片]``，模型从群上下文里完全看不出这条
        消息带图（用户不手动引用时图片就被静默忽略）。
        """
        buf = GroupContextBuffer(max_lines=10)
        buf.record("g1", "小明", "看这个", message_id="1", has_image=True, user_id="u1")
        rows = buf.snapshot("g1")
        assert rows[0]["text"] == "看这个 [图片]"

    def test_latest_image_message_same_user_only(self):
        buf = GroupContextBuffer(max_lines=10, ttl=10.0)
        buf.record(
            "g1",
            "小明",
            "我先发图",
            message_id="1",
            has_image=True,
            user_id="u1",
            now=100.0,
        )
        buf.record(
            "g1",
            "小红",
            "我也发",
            message_id="2",
            has_image=True,
            user_id="u2",
            now=101.0,
        )
        buf.record(
            "g1", "小明", "这张图是什么", message_id="3", user_id="u1", now=102.0
        )
        assert buf.latest_image_message("g1", "u1", now=105.0) == "1"
        assert buf.latest_image_message("g1", "u2", now=105.0) == "2"
        assert buf.latest_image_message("g1", "u3", now=105.0) is None
        assert (
            buf.latest_image_message("g1", "u1", exclude_message_id="1", now=105.0)
            is None
        )
        assert buf.latest_image_message("g1", "u1", now=120.0) is None  # TTL 过期

    def test_max_lines_ring(self):
        buf = GroupContextBuffer(max_lines=3)
        for i in range(6):
            buf.record("g1", "u", f"m{i}", message_id=str(i))
        rows = buf.snapshot("g1")
        assert [r["text"] for r in rows] == ["m3", "m4", "m5"]

    def test_limit(self):
        buf = GroupContextBuffer(max_lines=10)
        for i in range(5):
            buf.record("g1", "u", f"m{i}", message_id=str(i))
        rows = buf.snapshot("g1", limit=2)
        assert [r["text"] for r in rows] == ["m3", "m4"]

    def test_ttl_expiry(self):
        buf = GroupContextBuffer(max_lines=10, ttl=10.0)
        buf.record("g1", "u", "旧消息", message_id="1", now=100.0)
        assert buf.snapshot("g1", now=105.0)  # 未过期
        assert buf.snapshot("g1", now=200.0) == []  # 过期清空

    def test_char_budget_drops_oldest(self):
        buf = GroupContextBuffer(max_lines=10, max_chars=50)
        buf.record("g1", "u", "老" * 30, message_id="1")
        buf.record("g1", "u", "新" * 30, message_id="2")
        rows = buf.snapshot("g1")
        assert len(rows) == 1 and rows[0]["text"].startswith("新")

    def test_per_group_isolation(self):
        buf = GroupContextBuffer(max_lines=10)
        buf.record("g1", "u", "群1", message_id="1")
        buf.record("g2", "u", "群2", message_id="1")
        assert [r["text"] for r in buf.snapshot("g1")] == ["群1"]
        assert [r["text"] for r in buf.snapshot("g2")] == ["群2"]

    def test_long_line_truncated(self):
        buf = GroupContextBuffer(max_lines=10)
        buf.record("g1", "u", "x" * 500, message_id="1")
        assert len(buf.snapshot("g1")[0]["text"]) <= 200

    def test_clear(self):
        buf = GroupContextBuffer(max_lines=10)
        buf.record("g1", "u", "hi", message_id="1")
        buf.clear("g1")
        assert buf.snapshot("g1") == []


class TestContextConfig:
    def test_enabled_by_default(self, monkeypatch):
        monkeypatch.delenv("AGENT_GROUP_CONTEXT", raising=False)
        assert context_enabled()

    def test_can_disable(self, monkeypatch):
        for v in ["0", "false", "off", "no"]:
            monkeypatch.setenv("AGENT_GROUP_CONTEXT", v)
            assert not context_enabled()

    def test_lines_env(self, monkeypatch):
        monkeypatch.setenv("AGENT_GROUP_CONTEXT_LINES", "3")
        assert context_lines() == 3
        monkeypatch.setenv("AGENT_GROUP_CONTEXT_LINES", "bad")
        assert context_lines() == 10
