"""出站投递日志（P2）：agentcore/outbox.py 的记账/认账 + Sink 三态接线。

由来：对照 dsh-superagent 的 outbox/receipts——那边出站有持久回执，重启后逐条
认账。本仓出站铁律是「uncertain 绝不重发」，所以这里只记账 + 启动通知，
**绝不做重发**（重发 = 用户收两遍）。

变异锚点（改坏实现必须红）：
1. record 不落盘 → take_uncertain 拿不到条目，用例红
2. take_uncertain 不删行 → 二次调用仍返回同一批（重复打扰），用例红
3. recent 非正不返空 → 违反「非正 limit 返空」不变量，用例红
4. _truncate 不截断 → 超过 MAX_LINES 仍全量返回，用例红
5. Sink 的 uncertain 出口不记账 → 悬空投递重启后无从查证，用例红
6. _send_pending_notice 不消费 → 每次重连重复通知，用例红
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentcore import outbox
from plugins.qq_agent_adapter.sink import Sink


@pytest.fixture(scope="module")
def ON():
    """outbox_notice 模块（模块级 on_bot_connect 需要 driver，故先 init）。"""
    import nonebot

    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init(_env_file=None, superusers={"10000"})
    import plugins.qq_agent_adapter.outbox_notice as mod

    return mod


class _FakeThrottle:
    """Sink 只调 acquire；真实限流器在 outbound 里，与本用例无关。"""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def acquire(self, target: str) -> None:
        self.seen.append(target)


class _FakeBot:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.sent: list[tuple[str, object]] = []

    async def _send(self, target: str, user_id: int, message: object) -> None:
        self.sent.append((target, message))
        if self.error is not None:
            raise self.error

    async def send_private_msg(self, user_id: int, message: object) -> None:
        await self._send(f"private:{user_id}", user_id, message)

    async def send_group_msg(self, group_id: int, message: object) -> None:
        await self._send(f"group:{group_id}", group_id, message)


def _sink(bot) -> Sink:
    sink = Sink(throttle=_FakeThrottle())
    sink._bots = lambda: [bot]  # type: ignore[method-assign]
    return sink


@pytest.fixture(autouse=True)
def _isolate_outbox(monkeypatch, tmp_path):
    """日志关进 tmp：否则用例把 data/state/outbox.jsonl 写进仓库（CI 也会）。"""
    monkeypatch.setenv("AGENT_OUTBOX_FILE", str(tmp_path / "outbox.jsonl"))


class TestJournal:
    def test_record_and_recent_roundtrip(self):
        outbox.record(
            "private:10000", outbox.OUTCOME_UNCERTAIN, ref="r-1", detail="TimeoutError"
        )
        rows = outbox.recent(10)
        assert len(rows) == 1
        row = rows[0]
        assert row["target"] == "private:10000"
        assert row["outcome"] == "uncertain"
        assert row["ref"] == "r-1"
        assert row["detail"] == "TimeoutError"
        assert isinstance(row["ts"], float)

    def test_recent_non_positive_returns_empty(self):
        outbox.record("group:1", outbox.OUTCOME_SENT)
        assert outbox.recent(0) == []
        assert outbox.recent(-5) == []

    def test_record_keeps_no_message_body(self):
        """只存字段不存正文：正文绝不允许进任何可备份文件。"""
        outbox.record("private:1", outbox.OUTCOME_SENT, ref="r", detail="d")
        raw = Path(os.environ["AGENT_OUTBOX_FILE"]).read_text(encoding="utf-8")
        assert '"message"' not in raw
        assert '"text"' not in raw
        assert '"body"' not in raw

    def test_take_uncertain_consumes_one_shot(self):
        outbox.record("private:1", outbox.OUTCOME_UNCERTAIN, ref="a")
        outbox.record("private:2", outbox.OUTCOME_FAILED, ref="b")
        outbox.record("private:3", outbox.OUTCOME_UNCERTAIN, ref="c")
        taken = outbox.take_uncertain()
        assert [e["ref"] for e in taken] == ["a", "c"]
        # 一次性交接：再取为空，且 failed 行仍在（不影响其他认账）
        assert outbox.take_uncertain() == []
        assert [e["ref"] for e in outbox.recent(10)] == ["b"]

    def test_take_uncertain_without_file(self):
        assert outbox.take_uncertain() == []

    def test_bad_lines_do_not_block_others(self):
        path = Path(os.environ["AGENT_OUTBOX_FILE"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json\n", encoding="utf-8")
        outbox.record("private:9", outbox.OUTCOME_UNCERTAIN, ref="ok")
        assert [e["ref"] for e in outbox.take_uncertain()] == ["ok"]

    def test_truncate_keeps_latest(self, monkeypatch):
        monkeypatch.setattr(outbox, "MAX_LINES", 3)
        for i in range(6):
            outbox.record("private:1", outbox.OUTCOME_SENT, ref=f"r{i}")
        rows = outbox.recent(100)
        assert [e["ref"] for e in rows] == ["r3", "r4", "r5"]


class TestSinkJournal:
    @pytest.mark.asyncio
    async def test_sent_is_recorded(self):
        bot = _FakeBot()
        assert await _sink(bot).send_once("private:10000", "hi", ref="r-1") == "sent"
        rows = outbox.recent(10)
        assert [r["outcome"] for r in rows] == ["sent"]
        assert rows[0]["ref"] == "r-1"

    @pytest.mark.asyncio
    async def test_uncertain_is_recorded_with_error_type(self):
        bot = _FakeBot(TimeoutError("read timeout"))
        assert await _sink(bot).send_once("group:7", "hi", ref="job-1") == "uncertain"
        rows = outbox.recent(10)
        assert [r["outcome"] for r in rows] == ["uncertain"]
        assert rows[0]["ref"] == "job-1"
        assert rows[0]["detail"] == "TimeoutError"

    @pytest.mark.asyncio
    async def test_failed_is_recorded(self):
        bot = _FakeBot(RuntimeError("boom"))
        assert await _sink(bot).send_once("private:1", "hi", ref="r") == "failed"
        rows = outbox.recent(10)
        assert [r["outcome"] for r in rows] == ["failed"]
        assert rows[0]["detail"] == "RuntimeError"

    @pytest.mark.asyncio
    async def test_bad_target_is_recorded(self):
        assert await _sink(_FakeBot()).send_once("nope", "hi", ref="r") == "failed"
        rows = outbox.recent(10)
        assert rows[0]["detail"] == "bad target"

    @pytest.mark.asyncio
    async def test_no_bot_is_recorded(self):
        sink = Sink(throttle=_FakeThrottle())
        sink._bots = lambda: []  # type: ignore[method-assign]
        assert await sink.send_once("private:1", "hi") == "failed"
        assert outbox.recent(10)[0]["detail"] == "no bot"


class TestStartupNotice:
    def test_notice_text_lists_ref_target_and_detail(self, ON):
        text = ON.pending_notice_text(
            [
                {
                    "ts": 0.0,
                    "target": "private:10000",
                    "outcome": "uncertain",
                    "ref": "r-12",
                    "detail": "TimeoutError",
                },
                {
                    "ts": 0.0,
                    "target": "group:888",
                    "outcome": "uncertain",
                    "ref": "",
                    "detail": "",
                },
            ]
        )
        assert "2 条" in text
        assert "不重发" in text
        assert "r-12 → private:10000（TimeoutError）" in text
        assert "group:888" in text

    def test_notice_text_folds_beyond_max_listed(self, ON, monkeypatch):
        monkeypatch.setattr(ON, "_MAX_LISTED", 2)
        entries = [
            {
                "ts": 0.0,
                "target": f"private:{i}",
                "outcome": "uncertain",
                "ref": f"r{i}",
                "detail": "",
            }
            for i in range(5)
        ]
        text = ON.pending_notice_text(entries)
        assert "…等共 5 条" in text
        assert "r3" in text and "r4" in text
        assert "r0" not in text

    @pytest.mark.asyncio
    async def test_send_pending_notice_notifies_each_superuser_once(
        self, ON, monkeypatch
    ):
        outbox.record("private:10000", outbox.OUTCOME_UNCERTAIN, ref="r-12")
        monkeypatch.setenv("SUPERUSERS", '["111", "222"]')
        bot = _FakeBot()
        await ON._send_pending_notice(bot)
        assert [t for t, _ in bot.sent] == ["private:111", "private:222"]
        # 一次性：认账即消费，重连不重复打扰
        bot2 = _FakeBot()
        await ON._send_pending_notice(bot2)
        assert bot2.sent == []

    @pytest.mark.asyncio
    async def test_send_pending_notice_without_superusers(self, ON, monkeypatch):
        outbox.record("private:10000", outbox.OUTCOME_UNCERTAIN, ref="r-12")
        monkeypatch.setenv("SUPERUSERS", "")
        bot = _FakeBot()
        await ON._send_pending_notice(bot)
        assert bot.sent == []

    @pytest.mark.asyncio
    async def test_send_pending_notice_skips_bad_admin_id(self, ON, monkeypatch):
        outbox.record("private:10000", outbox.OUTCOME_UNCERTAIN, ref="r-12")
        monkeypatch.setenv("SUPERUSERS", '["abc", "111"]')
        bot = _FakeBot()
        await ON._send_pending_notice(bot)
        assert [t for t, _ in bot.sent] == ["private:111"]

    def test_first_create_is_0600(self, tmp_path, monkeypatch):
        """M12（2026-11 评审）：首建必须 0600。

        旧实现 `open(path, "a")` 走 umask 先落 0644，对同机其它本地用户可读——
        文件里有 target（真实 QQ/群号）与投递计数。同 commit 的 notify_budget
        已是「创建即 0600」，这里是漏掉的那一处。
        """
        import stat

        target = tmp_path / "state" / "outbox.jsonl"
        monkeypatch.setenv("AGENT_OUTBOX_FILE", str(target))
        outbox.record("private:10000", outbox.OUTCOME_SENT, ref="r-1")
        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == 0o600, f"首建权限应为 0600，实际 {oct(mode)}"

    @pytest.mark.asyncio
    async def test_notice_failure_keeps_the_ledger(self, ON, monkeypatch):
        """M14（2026-11 评审）：通知全失败时悬空账目必须保留。

        旧流程「读走即删 → 再通知」：发送抛错时账目已从文件里消失，而它存在的
        全部意义就是让人知道「有条投递结果不确定」——当场蒸发。
        """
        outbox.record("private:10000", outbox.OUTCOME_UNCERTAIN, ref="r-12")
        monkeypatch.setenv("SUPERUSERS", '["111"]')

        class _BrokenBot:
            @property
            def sent(self):
                return []

            async def send_private_msg(self, **kw):
                raise RuntimeError("protocol down")

        await ON._send_pending_notice(_BrokenBot())
        # 没通知成功 → 不销账，下次启动还能认领
        assert outbox.peek_uncertain() != []

        # 协议恢复后重试即可销账
        bot = _FakeBot()
        await ON._send_pending_notice(bot)
        assert [t for t, _ in bot.sent] == ["private:111"]
        assert outbox.peek_uncertain() == []

    def test_peek_and_drop_are_separate(self):
        """peek 不删、drop 才删：两阶段的前提。"""
        outbox.record("private:10000", outbox.OUTCOME_UNCERTAIN, ref="r-1")
        outbox.record("private:10000", outbox.OUTCOME_SENT, ref="r-2")
        assert len(outbox.peek_uncertain()) == 1
        assert len(outbox.peek_uncertain()) == 1, "peek 不应删除"
        assert outbox.drop_uncertain() == 1
        assert outbox.peek_uncertain() == []
        assert outbox.drop_uncertain() == 0
        # sent 行不受影响
        assert [r["ref"] for r in outbox.recent(10)] == ["r-2"]
