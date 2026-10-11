"""群消息原文归档：分类/缓冲/落库/清理/reader/对账。

来源：全量群消息归档 AC §A/B/C。缓冲逻辑用内存假 store（无 PG 依赖也能跑）；
PG 独有面（DDL/索引查询/JSON 往返）在 TEST_DATABASE_URL 存在时执行，TRUNCATE
隔离。AC §D（点评增强）在 test_group_stats_review.py / route 测试里。
"""

import datetime as dt
import os

import pytest
import pytest_asyncio

from agentcore import tz
from agentcore.groupstats import config as gs_config
from agentcore.groupstats.raw import (
    RawArchiveBuffer,
    classify_kind,
    recompute_window,
    reply_to_of,
)

_DAY = dt.date(2026, 10, 6)


class _Seg:
    def __init__(self, type_, data=None):
        self.type = type_
        self.data = data if data is not None else {}


class TestClassify:
    def test_priority_forward_over_all(self):
        assert (
            classify_kind(has_forward=True, has_image=True, is_reply=True) == "forward"
        )

    def test_image_over_reply(self):
        assert (
            classify_kind(has_forward=False, has_image=True, is_reply=True) == "image"
        )

    def test_reply_over_text(self):
        assert (
            classify_kind(has_forward=False, has_image=False, is_reply=True) == "reply"
        )

    def test_plain_text(self):
        assert (
            classify_kind(has_forward=False, has_image=False, is_reply=False) == "text"
        )


class TestReplyTo:
    def test_first_reply_id(self):
        segs = [
            _Seg("text"),
            _Seg("reply", {"id": "1001"}),
            _Seg("reply", {"id": "1002"}),
        ]
        assert reply_to_of(segs) == "1001"

    def test_message_id_key(self):
        assert reply_to_of([_Seg("reply", {"message_id": "77"})]) == "77"

    def test_no_reply(self):
        assert reply_to_of([_Seg("text"), _Seg("image")]) == ""

    def test_bad_data_shape(self):
        assert reply_to_of([_Seg("reply", "not-a-dict")]) == ""
        assert reply_to_of([_Seg("reply", {"id": ""})]) == ""

    def test_empty_segs(self):
        assert reply_to_of(None) == "" and reply_to_of([]) == ""


class _FakeStore:
    """记录 insert/delete 调用；可编排失败。"""

    def __init__(self, fail_inserts=0):
        self.rows: list[dict] = []
        self.fail_inserts = fail_inserts
        self.deleted_before: list[dt.date] = []

    async def insert_rows(self, rows):
        if self.fail_inserts > 0:
            self.fail_inserts -= 1
            raise RuntimeError("db down")
        self.rows.extend(rows)

    async def delete_before(self, day):
        self.deleted_before.append(day)


def _ts(hour: int = 12) -> float:
    day = tz.today_date()
    return dt.datetime.combine(day, dt.time(hour), tzinfo=tz.zoneinfo()).timestamp()


def _rec(buf: RawArchiveBuffer, **over):
    base = dict(
        group_id="456",
        user_id="u1",
        nickname="群友甲",
        message_id="m1",
        text="服务器又炸了",
        ts=_ts(),
        has_image=False,
        forward_id="",
        reply_to="",
    )
    base.update(over)
    buf.record(**base)


class TestBuffer:
    def test_record_classifies_and_counts(self):
        buf = RawArchiveBuffer(_FakeStore(), allowed_groups={"456"})
        _rec(buf, text="你好世界", has_image=True)
        row = buf._rows[0]
        assert row["kind"] == "image"
        assert row["chars"] == 4
        assert row["text"] == "你好世界"
        assert row["day"] == tz.today_date()

    def test_non_allowed_group_skipped(self):
        buf = RawArchiveBuffer(_FakeStore(), allowed_groups={"456"})
        _rec(buf, group_id="789")
        assert buf.pending() == 0

    def test_forward_row_kept_with_id(self):
        """合并转发照常归档（kind=forward，存 id 不取内容）。"""
        buf = RawArchiveBuffer(_FakeStore(), allowed_groups={"456"})
        _rec(buf, text="你看这个", forward_id="f-123")
        row = buf._rows[0]
        assert row["kind"] == "forward" and row["forward_id"] == "f-123"
        assert row["text"] == "你看这个"  # 只存发送者自己的文字

    def test_message_id_fallback_when_missing(self):
        buf = RawArchiveBuffer(_FakeStore(), allowed_groups={"456"})
        _rec(buf, message_id="")
        assert buf._rows[0]["message_id"].startswith("noids-")

    def test_missing_ids_skipped(self):
        buf = RawArchiveBuffer(_FakeStore(), allowed_groups={"456"})
        _rec(buf, group_id="")
        _rec(buf, user_id="")
        assert buf.pending() == 0

    def test_cap_drops_oldest(self):
        store = _FakeStore()
        buf = RawArchiveBuffer(store, max_rows=3, allowed_groups={"456"})
        for i in range(5):
            _rec(buf, message_id=f"m{i}", text=f"消息{i}")
        assert buf.pending() == 3
        assert [r["message_id"] for r in buf._rows] == ["m2", "m3", "m4"]

    @pytest.mark.asyncio
    async def test_flush_inserts_and_clears(self):
        store = _FakeStore()
        buf = RawArchiveBuffer(store, allowed_groups={"456"})
        _rec(buf)
        await buf.flush()
        assert buf.pending() == 0
        assert len(store.rows) == 1

    @pytest.mark.asyncio
    async def test_flush_empty_is_noop(self):
        store = _FakeStore()
        buf = RawArchiveBuffer(store, allowed_groups={"456"})
        await buf.flush()
        assert store.rows == []

    @pytest.mark.asyncio
    async def test_flush_failure_merges_back_in_order(self):
        """失败合并回队首（保序），与下批一起重试；事务回滚 ⇒ 不双写。"""
        store = _FakeStore(fail_inserts=1)
        buf = RawArchiveBuffer(store, allowed_groups={"456"})
        _rec(buf, message_id="m1")
        _rec(buf, message_id="m2")
        await buf.flush()  # 失败 → 合并回
        assert buf.pending() == 2
        assert [r["message_id"] for r in buf._rows] == ["m1", "m2"]
        await buf.flush()  # 重试成功
        assert buf.pending() == 0
        assert [r["message_id"] for r in store.rows] == ["m1", "m2"]

    @pytest.mark.asyncio
    async def test_prune_runs_once_per_day(self):
        store = _FakeStore()
        buf = RawArchiveBuffer(store, retention_days=30, allowed_groups={"456"})
        _rec(buf)
        await buf.flush()
        _rec(buf, message_id="m2")
        await buf.flush()
        assert len(store.deleted_before) == 1  # 同一天不重复 DELETE
        cutoff = store.deleted_before[0]
        assert cutoff == dt.date.fromordinal(tz.today_date().toordinal() - 30)

    @pytest.mark.asyncio
    async def test_record_never_raises(self):
        """record 同步且任何异常不外溢（挂点在事件循环热路径上）。"""
        buf = RawArchiveBuffer(_FakeStore(), allowed_groups={"456"})
        buf.record(
            group_id=None, user_id=None, nickname="", message_id="", text=None, ts=None
        )  # 全坏输入
        assert buf.pending() == 0


class TestRecompute:
    def test_per_user_and_words(self):
        rows = [
            {"user_id": "u1", "text": "服务器又炸了"},
            {"user_id": "u1", "text": "服务器服务器"},
            {"user_id": "u2", "text": "哈哈哈"},
            {"user_id": "u3", "text": ""},  # 空文本不计（与 stats 口径一致）
            {"user_id": "u3", "text": None},
        ]
        out = recompute_window(rows)
        assert out["per_user"]["u1"] == {"msgs": 2, "chars": 6 + 6}
        assert out["per_user"]["u2"] == {"msgs": 1, "chars": 3}
        assert "u3" not in out["per_user"]
        assert out["words"]["服务器"] == 3

    def test_stopwords_filtered_same_as_cloud(self):
        out = recompute_window(
            [{"user_id": "u1", "text": "我们觉得这个服务器什么毛病"}]
        )
        assert "服务器" in out["words"]
        for bad in ("我们", "觉得", "这个", "什么"):
            assert bad not in out["words"]


class TestArchiveConfig:
    def test_defaults(self, monkeypatch):
        for key in (
            "AGENT_GROUP_ARCHIVE",
            "AGENT_GROUP_ARCHIVE_RETENTION_DAYS",
            "AGENT_GROUP_ARCHIVE_FLUSH_SECONDS",
            "AGENT_GROUP_ARCHIVE_BUFFER_MAX",
        ):
            monkeypatch.delenv(key, raising=False)
        assert gs_config.archive_enabled() is False
        assert gs_config.archive_retention_days() == 30
        assert gs_config.archive_flush_seconds() == 30
        assert gs_config.archive_buffer_max() == 50000

    @pytest.mark.parametrize("raw", ["1", "true", "on"])
    def test_enabled_truthy(self, monkeypatch, raw):
        monkeypatch.setenv("AGENT_GROUP_ARCHIVE", raw)
        assert gs_config.archive_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "off"])
    def test_enabled_falsy(self, monkeypatch, raw):
        monkeypatch.setenv("AGENT_GROUP_ARCHIVE", raw)
        assert gs_config.archive_enabled() is False

    def test_dirty_int_fallback(self, monkeypatch):
        monkeypatch.setenv("AGENT_GROUP_ARCHIVE_RETENTION_DAYS", "abc")
        assert gs_config.archive_retention_days() == 30
        monkeypatch.setenv("AGENT_GROUP_ARCHIVE_BUFFER_MAX", "10")
        assert gs_config.archive_buffer_max() == 1000  # 钳下限


# --------------------------------------------------------------- PG 面

_PG_URL = os.getenv("TEST_DATABASE_URL") or ""


@pytest.mark.skipif(not _PG_URL, reason="TEST_DATABASE_URL not set")
class TestPgStore:
    @pytest_asyncio.fixture
    async def store(self):
        from agentcore.groupstats.raw import RawArchiveStore

        s = RawArchiveStore(_PG_URL)
        await s.init()
        yield s
        async with s.pool.acquire() as conn:
            await conn.execute("TRUNCATE group_messages_raw")
        await s.aclose()

    def _row(self, mid, uid="u1", day=_DAY, hour=12, text="服务器又炸了", **over):
        row = {
            "group_id": "456",
            "message_id": mid,
            "user_id": uid,
            "ts": dt.datetime.combine(
                day, dt.time(hour), tzinfo=tz.zoneinfo()
            ).timestamp(),
            "day": day,
            "nickname": "群友甲",
            "kind": "text",
            "text": text,
            "chars": len(text),
            "reply_to": "",
            "forward_id": "",
        }
        row.update(over)
        return row

    @pytest.mark.asyncio
    async def test_ddl_creates_indexes(self, store):
        async with store.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT indexname FROM pg_indexes WHERE tablename='group_messages_raw'"
            )
        names = {r["indexname"] for r in rows}
        for idx in (
            "gmraw_gut_idx",
            "gmraw_gt_idx",
            "gmraw_day_idx",
            "gmraw_reply_idx",
        ):
            assert idx in names, idx

    @pytest.mark.asyncio
    async def test_insert_idempotent(self, store):
        await store.insert_rows([self._row("m1")])
        await store.insert_rows([self._row("m1")])  # 同 PK 再插
        rows = await store.query("456", None, _DAY, _DAY, 10)
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_query_by_user_and_group(self, store):
        await store.insert_rows(
            [
                self._row("m1", uid="u1", hour=10),
                self._row("m2", uid="u2", hour=11),
                self._row("m3", uid="u1", day=_DAY + dt.timedelta(days=1)),
            ]
        )
        by_user = await store.query("456", "u1", _DAY, _DAY, 10)
        assert [r["message_id"] for r in by_user] == ["m1"]
        by_group = await store.query("456", None, _DAY, _DAY, 10)
        assert len(by_group) == 2
        # ts 降序
        assert by_group[0]["message_id"] == "m2"

    @pytest.mark.asyncio
    async def test_query_nonpositive_limit_is_empty(self, store):
        await store.insert_rows([self._row("m1")])
        assert await store.query("456", None, _DAY, _DAY, 0) == []
        assert await store.query("456", None, _DAY, _DAY, -1) == []

    @pytest.mark.asyncio
    async def test_kind_count_and_reply_count(self, store):
        await store.insert_rows(
            [
                self._row("m1", kind="forward", forward_id="f1"),
                self._row("m2", kind="image", text=""),
                self._row("m3", uid="u2", kind="reply", reply_to="m1"),
                self._row("m4", uid="u3", kind="reply", reply_to="m1"),
                self._row("m5", uid="u4", kind="reply", reply_to="m2"),
            ]
        )
        assert await store.kind_count("456", "u1", _DAY, _DAY, "forward") == 1
        assert await store.kind_count("456", "u1", _DAY, _DAY, "image") == 1
        assert await store.kind_count("456", "u1", _DAY, _DAY, "text") == 0
        # m1 被引 2 次；m2 被引 1 次；空 ids → 0
        assert await store.reply_count("456", ["m1"], _DAY, _DAY) == 2
        assert await store.reply_count("456", ["m1", "m2"], _DAY, _DAY) == 3
        assert await store.reply_count("456", [], _DAY, _DAY) == 0

    @pytest.mark.asyncio
    async def test_delete_before(self, store):
        old = dt.date(2026, 1, 1)
        await store.insert_rows([self._row("old", day=old), self._row("new")])
        await store.delete_before(_DAY)
        assert await store.query("456", None, old, old, 10) == []
        assert len(await store.query("456", None, _DAY, _DAY, 10)) == 1

    @pytest.mark.asyncio
    async def test_recompute_matches_live_stats(self, store):
        """C17：归档原文重算 == 实时聚合（同一批消息两条路径）。"""
        from agentcore.groupstats.collector import StatsCollector
        from agentcore.groupstats.store import InMemoryStatsStore

        messages = [
            ("u1", "服务器又炸了"),
            ("u1", "服务器服务器"),
            ("u1", ""),  # 空文本：stats 不计，归档也不进对账
            ("u2", "哈哈哈"),
        ]
        rows = [
            self._row(f"m{i}", uid=uid, text=text)
            for i, (uid, text) in enumerate(messages)
        ]
        await store.insert_rows(rows)

        stats = InMemoryStatsStore()
        col = StatsCollector(stats, allowed_groups={"456"})
        ts = dt.datetime.combine(_DAY, dt.time(12), tzinfo=tz.zoneinfo()).timestamp()
        for uid, text in messages:
            col.record("456", uid, "群友甲", text, ts=ts)
        await col.flush()

        raw_rows = await store.query("456", None, _DAY, _DAY, 100)
        out = recompute_window(raw_rows)
        live = await stats.user_ranking("456", _DAY, _DAY, 10)
        live_map = {r["user_id"]: r["msgs"] for r in live}
        recomputed = {u: v["msgs"] for u, v in out["per_user"].items()}
        assert recomputed == live_map
        # 词频也对得上
        freq = await stats.word_freq("456", _DAY, _DAY, 10)
        live_words = dict(freq)
        assert out["words"]["服务器"] == live_words["服务器"]
