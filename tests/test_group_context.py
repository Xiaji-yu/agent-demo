
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
