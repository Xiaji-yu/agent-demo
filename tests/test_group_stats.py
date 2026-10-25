"""群发言统计测试：分词 / 采集 / 榜单计算 / 存储双实现 / 配置。

来源：群发言榜+词云 AC 计划 §5-A/B。行为断言在内存与 PG 两实现上参数化双跑
（PG 用例在无 TEST_DATABASE_URL 时跳过）；PG 独有面（DDL、JSONB 往返）也在此
文件尾部，不新开 test_pg_groupstats 批次文件（AGENTS §2.7）。
"""

import datetime as dt
import os

import pytest
import pytest_asyncio

from agentcore import tz
from agentcore.groupstats import config as gs_config
from agentcore.groupstats import ranking, tokenize
from agentcore.groupstats.collector import StatsCollector
from agentcore.groupstats.store import InMemoryStatsStore, open_store


def _ts_today(hour: int = 19) -> float:
    """今天 hour 点的 Unix 时间戳（Asia/Shanghai）。

    collector 测试必须用「今天」而不是固定历史日期：flush 会触发 retention，
    2024 年的固定时间戳会被当成过期数据清掉（那是生产正确行为）。
    """
    day = tz.today_date()
    return dt.datetime.combine(day, dt.time(hour), tzinfo=tz.zoneinfo()).timestamp()


# --------------------------------------------------------------- 分词


class TestTokenize:
    def test_cut_keeps_meaningful_words(self):
        words = tokenize.cut("刚买了台新服务器，兄弟们觉得怎么样")
        assert "服务器" in words
        assert "兄弟" in words
        # 疑问代词/心理动词属于虚词，不再进词云（内容词口径）
        assert "怎么样" not in words
        assert "觉得" not in words

    def test_content_pos_filter_drops_function_words(self):
        """只留内容词：虚词/英文功能词/通用词不进词云（2026-10-25 词云口径）。"""
        text = (
            "就是 直接 然后 域名 东西 感觉 还有 ai 多少 这么 主要 之前 只是 "
            "显卡 群友 喜欢 便宜 其实 by many really 服务器 人格 突然 "
            "我要 看看 不到 人家 只会"
        )
        words = tokenize.cut(text)
        for good in ("域名", "显卡", "群友", "ai", "便宜", "服务器", "人格"):
            assert good in words, good
        for bad in (
            "就是",
            "直接",
            "然后",
            "东西",
            "感觉",
            "还有",
            "多少",
            "这么",
            "主要",
            "之前",
            "只是",
            "喜欢",
            "其实",
            "by",
            "many",
            "really",
            "突然",
            "我要",
            "看看",
            "不到",
            "人家",
            "只会",
        ):
            assert bad not in words, bad

    def test_filter_words_for_stored_old_data(self):
        """读取旧数据同口径过滤：虚词/通用词/英文功能词不占 Top-N。"""
        raw = [
            "就是",
            "服务器",
            "真的",
            "突然",
            "显卡",
            "东西",
            "ai",
            "by",
            "many",
            "人格",
        ]
        assert tokenize.filter_words(raw) == ["服务器", "显卡", "ai", "人格"]
        assert tokenize.filter_words(raw, limit=2) == ["服务器", "显卡"]
        assert tokenize.filter_words(raw, limit=0) == []

    def test_pos_unavailable_falls_back_to_plain_cut(self, monkeypatch):
        monkeypatch.setattr(tokenize, "_POS_READY", False)
        words = tokenize.cut("突然 服务器")
        assert "服务器" in words
        assert "突然" in words, "无词性模型时只靠停用词过滤，不能把词全杀掉"

    def test_prewarm_initializes_without_error_and_is_idempotent(self, monkeypatch):
        """M2（REVIEW-1e0f5ae..a0a6bb4）：预热真的完成词典加载且可重复调用。

        用 jieba 的 initialized 标志做确定性断言（首切耗时会因进程内其他
        用例已触发过初始化而测不出差异——全局状态顺序依赖）。
        缺 jieba 的环境 prewarm 是空操作、不抛异常。
        """
        tokenize.prewarm()
        tokenize.prewarm()  # 幂等：重复调用不抛错
        if not tokenize._JIEBA_READY:
            return  # 降级 bigram 的环境没有词典加载语义
        import jieba

        tok = jieba.dt
        monkeypatch.setattr(tok, "initialized", 0)
        tokenize.prewarm()
        assert tok.initialized, "预热必须完成词典加载（否则首条消息撞 0.8s 初始化）"

    def test_user_dict_merges_miscut_phrase(self):
        # jieba 默认把「合并转发」切成 合并/转发；用户词典补齐为整词
        assert "合并转发" in tokenize.cut("谁发了个合并转发看看")

    def test_repetition_preserved(self):
        # 截图的「哈哈哈哈哈」是一个词而不是一串哈
        words = tokenize.cut("哈哈哈哈哈哈哈哈")
        assert words and set(words) == {"哈哈哈哈"}

    def test_stopwords_filtered(self):
        words = tokenize.cut("我们觉得这个服务器什么毛病")
        assert "服务器" in words
        for bad in ("我们", "觉得", "这个", "什么"):
            assert bad not in words

    def test_single_char_and_digits_dropped(self):
        assert tokenize.cut("我") == []
        assert tokenize.cut("2024") == []
        assert tokenize.cut("5201314") == []

    def test_ascii_word_kept(self):
        assert "deepseek" in tokenize.cut("deepseek 真好用")
        assert "OK" in tokenize.cut("OK的")

    def test_url_cq_at_stripped(self):
        words = tokenize.cut("看这个 https://example.com/x 不错")
        assert "不错" in words
        assert not any("http" in w or "example" in w for w in words)
        words = tokenize.cut("[CQ:image,url=http://x] 图不错")
        assert "不错" in words
        assert not any(w.lower() in ("cq", "image", "url", "http") for w in words)
        words = tokenize.cut("@张三 在吗")
        assert "张三" in words
        assert "@" not in "".join(words)

    def test_extra_stopwords_env(self):
        assert tokenize.cut("服务器", extra_stopwords=frozenset({"服务器"})) == []

    def test_empty_text(self):
        assert tokenize.cut("") == []
        assert tokenize.cut("   ") == []

    def test_bigram_fallback_without_jieba(self, monkeypatch):
        monkeypatch.setattr(tokenize, "_JIEBA_READY", False)
        words = tokenize.cut("服务器又炸了")
        assert words  # 降级不等于没词
        assert "服务器" not in words  # bigram 切不出整词
        assert all(len(w) == 2 for w in words if not w.isascii())
        assert "服务" in words


# --------------------------------------------------------------- 采集器


@pytest.fixture
def collector():
    return StatsCollector(InMemoryStatsStore(), allowed_groups={"g1"})


class TestCollector:
    def test_non_allowed_group_skipped(self, collector):
        collector.record("g2", "u1", "小明", "服务器又炸了")
        assert collector.pending() == (0, 0)

    def test_empty_allowlist_collects_nothing(self):
        """M1（REVIEW-1e0f5ae..a0a6bb4）：空白名单 = 不采集（fail-closed）。

        旧实现空集放行 = 全量采集所有群，与同键 ALLOWED_GROUPS 在聊天面的
        「空集合表示没有群允许」（acl.py）语义相反，且静默落库全部群数据。
        """
        col = StatsCollector(InMemoryStatsStore(), allowed_groups=set())
        col.record("999999", "u1", "小明", "服务器又炸了")
        col.record("g1", "u1", "小明", "服务器又炸了")
        assert col.pending() == (0, 0)

    def test_empty_text_skipped(self, collector):
        collector.record("g1", "u1", "小明", "   ")
        assert collector.pending() == (0, 0)

    def test_missing_ids_skipped(self, collector):
        collector.record("", "u1", "小明", "hello")
        collector.record("g1", "", "小明", "hello")
        assert collector.pending() == (0, 0)

    def test_aggregates_msgs_chars_hours_words(self, collector):
        ts = _ts_today(19)
        collector.record("g1", "u1", "小明", "服务器又炸了", ts=ts)
        collector.record("g1", "u1", "小明", "真的炸了", ts=ts)
        daily, words = collector.pending()
        assert daily == 1
        assert words > 0
        row = list(collector._daily.values())[0]
        assert row["msgs"] == 2
        assert row["chars"] == len("服务器又炸了") + len("真的炸了")
        assert sum(row["hours"].values()) == 2
        assert row["nickname"] == "小明"

    def test_nickname_latest_wins(self, collector):
        collector.record("g1", "u1", "旧名", "在吗", ts=_ts_today(19))
        collector.record("g1", "u1", "新名", "在吗", ts=_ts_today(19))
        row = list(collector._daily.values())[0]
        assert row["nickname"] == "新名"

    @pytest.mark.asyncio
    async def test_flush_with_nul_nickname_does_not_poison_batch(self, collector):
        """2026-10-20 线上事故回归：白名单群一条含 NUL(0x00) 的群名片让
        executemany 整批失败（PG text 不接受 NUL）⇒ 合并回内存反复重试 ⇒
        全部群统计停写 95 分钟。端到端锁死：脏名片经 record→flush 后落库
        值必须剥净，且不影响同批其他行的写入。"""
        today = tz.today_date()
        collector.record("g1", "u1", "小\x00明\x07", "服务器又炸了", ts=_ts_today(19))
        collector.record("g1", "u2", "小红", "真的炸了", ts=_ts_today(19))
        await collector.flush()
        assert collector.pending() == (0, 0)
        assert await collector.store.nicknames("g1", today, today) == {
            "u1": "小明",
            "u2": "小红",
        }
        rows = await collector.store.user_ranking("g1", today, today, 5)
        assert {r["user_id"] for r in rows} == {"u1", "u2"}

    def test_words_aggregate_across_msgs(self, collector):
        ts = 1728200000.0
        collector.record("g1", "u1", "小明", "服务器服务器", ts=ts)
        collector.record("g1", "u2", "小红", "服务器", ts=ts)
        counts = [c for (g, d, w), c in collector._words.items() if w == "服务器"]
        assert counts == [3]

    @pytest.mark.asyncio
    async def test_flush_writes_and_clears(self, collector):
        today = tz.today_date()
        collector.record("g1", "u1", "小明", "服务器又炸了", ts=_ts_today(19))
        await collector.flush()
        assert collector.pending() == (0, 0)
        rows = await collector.store.user_ranking("g1", today, today, 5)
        assert rows[0]["msgs"] == 1
        freq = await collector.store.word_freq("g1", today, today, 10)
        assert any(w == "服务器" for w, _ in freq)

    @pytest.mark.asyncio
    async def test_flush_empty_is_noop(self, collector):
        await collector.flush()  # 不抛异常即可
        assert collector.pending() == (0, 0)

    @pytest.mark.asyncio
    async def test_flush_failure_merges_back_no_double_count(self, collector):
        class FailingStore(InMemoryStatsStore):
            def __init__(self):
                super().__init__()
                self.fail = True

            async def upsert_daily(self, rows):
                if self.fail:
                    raise RuntimeError("db down")
                return await super().upsert_daily(rows)

            async def upsert_words(self, rows):
                if self.fail:
                    raise RuntimeError("db down")
                return await super().upsert_words(rows)

        store = FailingStore()
        col = StatsCollector(store, allowed_groups={"g1"})
        col.record("g1", "u1", "小明", "服务器又炸了", ts=_ts_today(19))
        await col.flush()  # 失败 → 合并回内存
        assert col.pending() == (1, 1)
        store.fail = False
        await col.flush()  # 重试成功
        today = tz.today_date()
        rows = await store.user_ranking("g1", today, today, 5)
        assert rows[0]["msgs"] == 1  # 没有双计
        assert col.pending() == (0, 0)

    @pytest.mark.asyncio
    async def test_retention_deletes_old_days(self, collector):
        old = dt.date(2024, 1, 1)
        today = tz.today_date()
        await collector.store.upsert_daily(
            [
                {
                    "group_id": "g1",
                    "user_id": "u1",
                    "day": old,
                    "nickname": "小明",
                    "msgs": 5,
                    "chars": 10,
                    "hours": {},
                }
            ]
        )
        await collector.store.upsert_words(
            [{"group_id": "g1", "day": old, "word": "服务器", "count": 5}]
        )
        col = StatsCollector(collector.store, allowed_groups={"g1"}, retention_days=1)
        col.record("g1", "u1", "小明", "在吗", ts=_ts_today(19))
        await col.flush()  # 顺带触发 retention
        assert await collector.store.user_ranking("g1", old, old, 5) == []
        assert await collector.store.word_freq("g1", old, old, 5) == []
        # 当天的数据必须保留（retention 只清旧不清新）
        rows = await collector.store.user_ranking("g1", today, today, 5)
        assert rows[0]["msgs"] == 1

    @pytest.mark.asyncio
    async def test_retention_runs_once_per_day(self, collector):
        class CountingStore(InMemoryStatsStore):
            def __init__(self):
                super().__init__()
                self.deletes = 0

            async def delete_before(self, day):
                self.deletes += 1
                return await super().delete_before(day)

        store = CountingStore()
        col = StatsCollector(store, allowed_groups={"g1"})
        col.record("g1", "u1", "小明", "在吗", ts=_ts_today(19))
        await col.flush()
        col.record("g1", "u1", "小明", "在吗", ts=_ts_today(19))
        await col.flush()
        assert store.deletes == 1  # 同一天不重复 DELETE


# --------------------------------------------------------------- 榜单计算


class TestRanking:
    def test_week_bounds_monday_start(self):
        assert ranking.week_bounds(dt.date(2025, 10, 8)) == (
            dt.date(2025, 10, 6),
            dt.date(2025, 10, 12),
        )
        # 周日属于同一周（不提前切）
        assert ranking.week_bounds(dt.date(2025, 10, 12)) == (
            dt.date(2025, 10, 6),
            dt.date(2025, 10, 12),
        )

    def test_previous_week_cross_month(self):
        start, end = ranking.previous_week(now=dt.date(2025, 10, 1))
        assert (start, end) == (dt.date(2025, 9, 22), dt.date(2025, 9, 28))
        assert ranking.fmt_range(start, end) == "9/22 ~ 9/28"

    def test_current_week_upto_today(self):
        start, end = ranking.current_week(now=dt.date(2025, 10, 8))
        assert (start, end) == (dt.date(2025, 10, 6), dt.date(2025, 10, 8))

    def test_build_leaderboard_new_and_delta(self):
        current = [
            {"user_id": "u1", "msgs": 100, "chars": 500},
            {"user_id": "u2", "msgs": 50, "chars": 200},
            {"user_id": "u3", "msgs": 30, "chars": 100},
        ]
        previous = [
            {"user_id": "u1", "msgs": 80, "chars": 400},
            {"user_id": "u3", "msgs": 20, "chars": 90},
            {"user_id": "u9", "msgs": 5, "chars": 10},
        ]
        entries, total = ranking.build_leaderboard(current, previous, 3)
        assert total == 180
        assert [e.user_id for e in entries] == ["u1", "u2", "u3"]
        assert entries[0].delta == 0  # 上周也是第 1
        assert entries[1].is_new and entries[1].delta is None
        assert entries[2].delta == -1  # 上周第 2 → 本周第 3
        assert entries[0].pct == pytest.approx(100 / 180 * 100)

    def test_build_leaderboard_topn_zero_returns_empty(self):
        entries, total = ranking.build_leaderboard(
            [{"user_id": "u1", "msgs": 10, "chars": 5}], [], 0
        )
        assert entries == [] and total == 0
        entries, total = ranking.build_leaderboard(
            [{"user_id": "u1", "msgs": 10, "chars": 5}], [], -3
        )
        assert entries == []

    def test_peak_hour(self):
        assert ranking.peak_hour({19: 430, 20: 100}) == (19, 430)
        assert ranking.peak_hour({}) is None
        assert ranking.peak_hour({0: 0}) == (0, 0)
        # 并列时取较早小时（确定性）
        assert ranking.peak_hour({19: 5, 3: 5}) == (3, 5)
        assert ranking.fmt_peak((19, 430)) == "19:00 前后（430 条）"
        assert ranking.fmt_peak(None) == "数据不足"


# --------------------------------------------------------------- 存储双实现

_PG_URL = os.getenv("TEST_DATABASE_URL") or ""


def _stores():
    yield "memory", InMemoryStatsStore()
    if _PG_URL:
        from agentcore.groupstats.store import PgStatsStore

        yield "pg", PgStatsStore(_PG_URL)


@pytest_asyncio.fixture(params=[p[0] for p in _stores()])
async def any_store(request):
    from agentcore.groupstats.store import PgStatsStore

    if request.param == "pg":
        store = PgStatsStore(_PG_URL)
    else:
        store = InMemoryStatsStore()
    await store.init()
    yield store
    # PG 用例是 TRUNCATE 级隔离；内存实现随用例丢弃
    if request.param == "pg":
        async with store.pool.acquire() as conn:
            await conn.execute("TRUNCATE group_msg_daily, group_word_daily")
        await store.aclose()


_DAY = dt.date(2025, 10, 6)
_DAY2 = dt.date(2025, 10, 7)


def _row(user_id, day=_DAY, msgs=1, chars=10, nickname="", hours=None):
    return {
        "group_id": "g1",
        "user_id": user_id,
        "day": day,
        "nickname": nickname,
        "msgs": msgs,
        "chars": chars,
        "hours": hours or {},
    }


class TestStoreContract:
    """两实现必须语义一致（upsert 是 += 语义，flush 重试安全依赖它）。"""

    @pytest.mark.asyncio
    async def test_upsert_accumulates(self, any_store):
        await any_store.upsert_daily([_row("u1", msgs=2, chars=5)])
        await any_store.upsert_daily([_row("u1", msgs=3, chars=7)])
        rows = await any_store.user_ranking("g1", _DAY, _DAY, 5)
        assert rows == [{"user_id": "u1", "msgs": 5, "chars": 12}]

    @pytest.mark.asyncio
    async def test_ranking_order_and_tiebreak(self, any_store):
        await any_store.upsert_daily(
            [
                _row("u1", msgs=10),
                _row("u2", msgs=10),  # 并列 → user_id 升序
                _row("u3", msgs=30),
            ]
        )
        rows = await any_store.user_ranking("g1", _DAY, _DAY, 5)
        assert [r["user_id"] for r in rows] == ["u3", "u1", "u2"]

    @pytest.mark.asyncio
    async def test_ranking_nonpositive_topn_is_empty(self, any_store):
        await any_store.upsert_daily([_row("u1", msgs=10)])
        assert await any_store.user_ranking("g1", _DAY, _DAY, 0) == []
        assert await any_store.user_ranking("g1", _DAY, _DAY, -1) == []

    @pytest.mark.asyncio
    async def test_ranking_window_bounds(self, any_store):
        await any_store.upsert_daily([_row("u1", day=_DAY, msgs=1)])
        await any_store.upsert_daily([_row("u1", day=_DAY2, msgs=2)])
        rows = await any_store.user_ranking("g1", _DAY2, _DAY2, 5)
        assert rows[0]["msgs"] == 2  # 窗口外不含

    @pytest.mark.asyncio
    async def test_nicknames_latest_day_wins(self, any_store):
        await any_store.upsert_daily([_row("u1", day=_DAY, nickname="旧名")])
        await any_store.upsert_daily([_row("u1", day=_DAY2, nickname="新名")])
        assert await any_store.nicknames("g1", _DAY, _DAY2) == {"u1": "新名"}

    @pytest.mark.asyncio
    async def test_nickname_control_chars_stripped(self, any_store):
        """2026-10-20 线上事故回归：PG text 列不接受 NUL(0x00)，含 NUL 的
        群名片曾把整批 flush 毒化（见 collector 同题端到端用例）。用户可控
        文本落库前必须剥控制字符，两实现同口径。"""
        await any_store.upsert_daily([_row("u1", nickname="小\x00明\x07")])
        assert await any_store.nicknames("g1", _DAY, _DAY) == {"u1": "小明"}

    @pytest.mark.asyncio
    async def test_nickname_only_control_chars_keeps_previous(self, any_store):
        """剥净后为空的名片不覆盖旧值——与 PG 的
        ``CASE WHEN excluded.nickname <> ''`` 同语义，双实现必须一致。"""
        await any_store.upsert_daily([_row("u1", nickname="旧名")])
        await any_store.upsert_daily([_row("u1", nickname="\x00\x00")])
        assert await any_store.nicknames("g1", _DAY, _DAY) == {"u1": "旧名"}

    @pytest.mark.asyncio
    async def test_word_control_chars_stripped(self, any_store):
        """分词结果同样可能含 NUL（消息正文用户可控），词频落库前剥净。"""
        await any_store.upsert_words(
            [{"group_id": "g1", "day": _DAY, "word": "服务\x00器", "count": 3}]
        )
        assert await any_store.word_freq("g1", _DAY, _DAY, 10) == [("服务器", 3)]

    @pytest.mark.asyncio
    async def test_hour_histogram_merges(self, any_store):
        await any_store.upsert_daily(
            [_row("u1", day=_DAY, hours={19: 2}), _row("u2", day=_DAY, hours={19: 3})]
        )
        await any_store.upsert_daily([_row("u1", day=_DAY2, hours={19: 1, 20: 4})])
        hist = await any_store.hour_histogram("g1", _DAY, _DAY2)
        assert hist == {19: 6, 20: 4}

    @pytest.mark.asyncio
    async def test_word_freq_limit_and_order(self, any_store):
        await any_store.upsert_words(
            [
                {"group_id": "g1", "day": _DAY, "word": "服务器", "count": 5},
                {"group_id": "g1", "day": _DAY, "word": "炸弹", "count": 9},
                {"group_id": "g1", "day": _DAY2, "word": "服务器", "count": 2},
            ]
        )
        freq = await any_store.word_freq("g1", _DAY, _DAY2, 10)
        assert freq == [("炸弹", 9), ("服务器", 7)]
        assert await any_store.word_freq("g1", _DAY, _DAY2, 0) == []
        assert await any_store.word_freq("g1", _DAY, _DAY2, 1) == [("炸弹", 9)]

    @pytest.mark.asyncio
    async def test_group_totals(self, any_store):
        await any_store.upsert_daily(
            [_row("u1", msgs=2), _row("u2", msgs=3), _row("u3", day=_DAY2, msgs=1)]
        )
        totals = await any_store.group_totals(_DAY, _DAY2)
        assert totals == [{"group_id": "g1", "msgs": 6, "users": 3}]

    @pytest.mark.asyncio
    async def test_delete_before(self, any_store):
        await any_store.upsert_daily([_row("u1", day=dt.date(2025, 9, 1), msgs=1)])
        await any_store.upsert_daily([_row("u1", day=_DAY, msgs=1)])
        await any_store.upsert_words(
            [{"group_id": "g1", "day": dt.date(2025, 9, 1), "word": "旧词", "count": 1}]
        )
        await any_store.delete_before(_DAY)
        assert (
            await any_store.user_ranking(
                "g1", dt.date(2025, 9, 1), dt.date(2025, 9, 1), 5
            )
            == []
        )
        assert (
            await any_store.word_freq("g1", dt.date(2025, 9, 1), dt.date(2025, 9, 1), 5)
            == []
        )
        rows = await any_store.user_ranking("g1", _DAY, _DAY, 5)
        assert rows[0]["msgs"] == 1


# --------------------------------------------------------------- 配置


class TestConfig:
    def test_enabled_default_off(self, monkeypatch):
        monkeypatch.delenv("AGENT_GROUP_STATS", raising=False)
        assert gs_config.enabled() is False

    @pytest.mark.parametrize("raw", ["1", "true", "on", "TRUE", "Yes"])
    def test_enabled_truthy(self, monkeypatch, raw):
        monkeypatch.setenv("AGENT_GROUP_STATS", raw)
        assert gs_config.enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "off", "no"])
    def test_enabled_falsy(self, monkeypatch, raw):
        monkeypatch.setenv("AGENT_GROUP_STATS", raw)
        assert gs_config.enabled() is False

    def test_dirty_int_falls_back_default(self, monkeypatch):
        monkeypatch.setenv("AGENT_GROUP_STATS_FLUSH_SECONDS", "abc")
        assert gs_config.flush_seconds() == 30

    def test_topn_and_words_clamped(self, monkeypatch):
        monkeypatch.setenv("AGENT_GROUP_STATS_TOP_N", "0")
        assert gs_config.top_n() == 1
        monkeypatch.setenv("AGENT_GROUP_STATS_TOP_N", "99")
        assert gs_config.top_n() == 20
        monkeypatch.setenv("AGENT_GROUP_STATS_WORDS", "3")
        assert gs_config.words() == 10
        monkeypatch.setenv("AGENT_GROUP_STATS_WORDS", "9999")
        assert gs_config.words() == 500

    def test_cron_default_and_disable(self, monkeypatch):
        monkeypatch.delenv("AGENT_GROUP_STATS_CRON", raising=False)
        assert gs_config.cron() == "0 9 * * 1"
        monkeypatch.setenv("AGENT_GROUP_STATS_CRON", "")
        assert gs_config.cron() == ""

    def test_extra_stopwords_parsing(self, monkeypatch):
        monkeypatch.setenv("AGENT_GROUP_STATS_STOPWORDS", "服务器, 显卡 ,")
        assert gs_config.extra_stopwords() == frozenset({"服务器", "显卡"})

    def test_allowed_groups_same_parsing_as_acl(self, monkeypatch):
        """口径锁：agentcore 不能 import plugins.acl（分层铁律），但解析规则
        必须与 acl.ALLOWED_GROUPS 一致——否则白名单两边对不上，统计会记到
        ACL 不认的群（fail-open 方向）。"""
        from plugins.qq_agent_adapter.acl import _load_list

        monkeypatch.delenv("AGENT_GROUP_STATS_ALLOWED_GROUPS", raising=False)
        monkeypatch.setenv("ALLOWED_GROUPS", "111, 222 ,333")
        assert gs_config.allowed_groups() == _load_list("ALLOWED_GROUPS")

    def test_stats_allowed_groups_can_narrow_scope(self, monkeypatch):
        """统计白名单可收窄到 ALLOWED_GROUPS 的子集（只采点名的群）。"""
        monkeypatch.setenv("ALLOWED_GROUPS", "111,222,333")
        monkeypatch.setenv("AGENT_GROUP_STATS_ALLOWED_GROUPS", "111, 333 ,")
        assert gs_config.allowed_groups() == frozenset({"111", "333"})

    def test_stats_allowed_groups_never_escape_acl(self, monkeypatch, caplog):
        """越界群号必须被忽略：统计范围不能大于 ACL 白名单（fail-closed）。"""
        import logging

        monkeypatch.setenv("ALLOWED_GROUPS", "111,222")
        monkeypatch.setenv("AGENT_GROUP_STATS_ALLOWED_GROUPS", "222,999")
        with caplog.at_level(logging.WARNING, logger="agentcore.groupstats"):
            assert gs_config.allowed_groups() == frozenset({"222"})
        assert "999" in caplog.text

    def test_stats_allowed_groups_rejects_non_ascii(self, monkeypatch, caplog):
        """全角数字永远匹配不上群号：告警剔除，不能静默进白名单。"""
        import logging

        monkeypatch.setenv("ALLOWED_GROUPS", "111,１２３")
        monkeypatch.setenv("AGENT_GROUP_STATS_ALLOWED_GROUPS", "111,１２３")
        with caplog.at_level(logging.WARNING, logger="agentcore.groupstats"):
            assert gs_config.allowed_groups() == frozenset({"111"})
        assert "１２３" in caplog.text

    def test_stats_allowed_groups_empty_falls_back_to_acl(self, monkeypatch):
        """留空 = 继承 ALLOWED_GROUPS（与加这个键之前的行为一致）。"""
        monkeypatch.setenv("ALLOWED_GROUPS", "111,222")
        monkeypatch.setenv("AGENT_GROUP_STATS_ALLOWED_GROUPS", "  ")
        assert gs_config.allowed_groups() == frozenset({"111", "222"})


# --------------------------------------------------------------- 模块入口


class TestModuleEntry:
    def test_record_noop_when_not_configured(self, monkeypatch):
        monkeypatch.setenv("AGENT_GROUP_STATS", "1")
        import agentcore.groupstats as gs

        assert gs.collector() is None
        gs.record("g1", "u1", "小明", "服务器又炸了")  # 不抛异常

    def test_configure_and_record(self, monkeypatch):
        monkeypatch.setenv("AGENT_GROUP_STATS", "1")
        monkeypatch.setenv("ALLOWED_GROUPS", "g1")
        import agentcore.groupstats as gs

        store = InMemoryStatsStore()
        col = gs.configure(store)
        assert col is not None and gs.collector() is col
        gs.record("g1", "u1", "小明", "服务器又炸了", ts=_ts_today(19))
        assert col.pending()[0] == 1
        gs.record("g9", "u1", "小明", "不在白名单")  # 被采集器拦下
        assert col.pending()[0] == 1
        gs._collector = None  # 复位，避免污染其他用例

    def test_configure_disabled(self, monkeypatch):
        monkeypatch.delenv("AGENT_GROUP_STATS", raising=False)
        import agentcore.groupstats as gs

        assert gs.configure(InMemoryStatsStore()) is None
        assert gs.collector() is None
        gs._collector = None

    def test_configure_empty_allowlist_warns(self, monkeypatch, caplog):
        """M1：开关开而白名单空 → WARNING 点名 ALLOWED_GROUPS（fail-closed 提醒）。"""
        import logging

        monkeypatch.setenv("AGENT_GROUP_STATS", "1")
        monkeypatch.delenv("ALLOWED_GROUPS", raising=False)
        import agentcore.groupstats as gs

        with caplog.at_level(logging.WARNING, logger="agentcore.groupstats"):
            col = gs.configure(InMemoryStatsStore())
        assert col is not None
        assert any("ALLOWED_GROUPS" in r.message for r in caplog.records)
        gs._collector = None

    @pytest.mark.asyncio
    async def test_open_store_memory_without_url(self):
        store = await open_store("")
        assert isinstance(store, InMemoryStatsStore)
        await store.init()  # 幂等
