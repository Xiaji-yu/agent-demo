"""agentcore/diagnostics：进程内诊断事件缓冲（web 总览「最近事件」的数据源）。

纪律：只存标量摘要、**不存消息正文**（与仓库 M4 同源）——用例里也刻意用
无内容的字段，别把这条测试本身变成泄露点。
"""

from agentcore.diagnostics import _MAX_EVENTS, clear, recent, record


def setup_function():
    clear()


def teardown_function():
    clear()


class TestDiagnostics:
    def test_record_and_recent_newest_first(self):
        record("a", x=1)
        record("b", x=2)
        items = recent()
        assert [i["kind"] for i in items] == ["b", "a"], "最新在前"
        assert items[0]["x"] == 2
        assert items[0]["ts"] > 0

    def test_limit(self):
        for i in range(5):
            record("k", i=i)
        assert len(recent(limit=2)) == 2
        assert [i["i"] for i in recent(limit=2)] == [4, 3]
        assert recent(limit=0) == []

    def test_ring_evicts_oldest(self):
        for i in range(_MAX_EVENTS + 5):
            record("k", i=i)
        items = recent(limit=_MAX_EVENTS)
        assert len(items) == _MAX_EVENTS
        assert items[0]["i"] == _MAX_EVENTS + 4, "最旧的应被淘汰"

    def test_clear(self):
        record("k")
        clear()
        assert recent() == []
