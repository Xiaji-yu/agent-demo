"""采集器：群消息 → 内存聚合 → 定期 flush 到 store。

为什么中间要一层内存聚合：每条群消息直接写库会把 PG 连接池打满（活跃群
秒级十几条）。采集在**事件循环里同步完成**（纯 dict 累加，无 await 无 IO，
单事件循环下天然原子——与 group_context 环形缓冲同款论证），flush 按周期
（默认 30s）批量 upsert，一次几百行。

flush 失败不丢数据：先换出待写批次再写，失败（事务回滚）后合并回内存，
与下一批一起重试。upsert 语义是「+=」，重试安全的前提是**事务原子**——
PgStatsStore 的 upsert_daily/upsert_words 都在 conn.transaction() 内。
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime

from agentcore import tz
from agentcore.groupstats.store import StatsStore
from agentcore.groupstats.tokenize import cut

logger = logging.getLogger(__name__)

_MAX_NICKNAME = 32  # 名片快照截断（与 group_context.PER_LINE_CHARS 同思路）


def _day_of(ts: float | None) -> date:
    if ts is None:
        return tz.today_date()
    return datetime.fromtimestamp(ts, tz.zoneinfo()).date()


class StatsCollector:
    """进程内聚合器。record() 同步、O(1)；flush() 异步、可失败重试。"""

    def __init__(
        self,
        store: StatsStore,
        *,
        allowed_groups: frozenset[str] | set[str] = frozenset(),
        retention_days: int = 35,
        extra_stopwords: frozenset[str] | set[str] = frozenset(),
    ):
        self.store = store
        self._allowed = frozenset(allowed_groups or ())
        self._retention_days = max(1, int(retention_days))
        self._extra_stopwords = frozenset(extra_stopwords or ())
        self._daily: dict[tuple[str, str, date], dict] = {}
        self._words: dict[tuple[str, date, str], int] = {}
        self._last_retention_day: date | None = None

    # ----------------------------------------------------------- 采集（同步）

    def record(
        self,
        group_id: str,
        user_id: str,
        nickname: str,
        text: str,
        ts: float | None = None,
    ) -> None:
        """累加一条群消息。非白名单群/空正文直接跳过（调用方不过滤时这里是硬闸）。

        白名单语义与 acl 同键同口径（M1，REVIEW-1e0f5ae..a0a6bb4）：**空集 =
        不采集**（fail-closed，与 acl「空集合表示没有群允许」一致）——旧实现
        空集放行等于全量采集所有群消息，与同键的聊天面语义相反。
        """
        gid = str(group_id or "")
        uid = str(user_id or "")
        if not gid or not uid:
            return
        if gid not in self._allowed:
            return
        body = (text or "").strip()
        if not body:
            return
        moment = _day_of(ts)
        hour = datetime.fromtimestamp(
            time.time() if ts is None else ts, tz.zoneinfo()
        ).hour
        key = (gid, uid, moment)
        row = self._daily.get(key)
        if row is None:
            row = {
                "group_id": gid,
                "user_id": uid,
                "day": moment,
                "nickname": "",
                "msgs": 0,
                "chars": 0,
                "hours": {},
            }
            self._daily[key] = row
        if nickname:
            row["nickname"] = str(nickname).strip()[:_MAX_NICKNAME]
        row["msgs"] += 1
        row["chars"] += len(body)
        row["hours"][hour] = row["hours"].get(hour, 0) + 1
        for word in cut(body, self._extra_stopwords):
            wkey = (gid, moment, word)
            self._words[wkey] = self._words.get(wkey, 0) + 1

    # ----------------------------------------------------------- 落库（异步）

    async def flush(self) -> None:
        """把内存批量写进 store；失败合并回内存等下一批（不丢、不双计）。"""
        if not self._daily and not self._words:
            return
        daily_rows = list(self._daily.values())
        word_rows = [
            {"group_id": gid, "day": day, "word": word, "count": count}
            for (gid, day, word), count in self._words.items()
        ]
        # 先换出再写：await 期间新到的消息进新批次，不与本批混淆
        self._daily = {}
        self._words = {}
        try:
            await self.store.upsert_daily(daily_rows)
            await self.store.upsert_words(word_rows)
        except Exception:
            # 事务回滚 ⇒ 库里没有本批 ⇒ 合并回去安全（+= 语义重试不双计）
            self._merge_back(daily_rows, word_rows)
            logger.exception("群统计 flush 失败，已合并回内存等下一批重试")
            return
        await self._maybe_retention()

    def _merge_back(self, daily_rows: list[dict], word_rows: list[dict]) -> None:
        for r in daily_rows:
            key = (r["group_id"], r["user_id"], r["day"])
            cur = self._daily.setdefault(
                key,
                {
                    "group_id": key[0],
                    "user_id": key[1],
                    "day": key[2],
                    "nickname": "",
                    "msgs": 0,
                    "chars": 0,
                    "hours": {},
                },
            )
            if r.get("nickname"):
                cur["nickname"] = r["nickname"]
            cur["msgs"] += r["msgs"]
            cur["chars"] += r["chars"]
            for h, n in (r.get("hours") or {}).items():
                cur["hours"][h] = cur["hours"].get(h, 0) + n
        for r in word_rows:
            key = (r["group_id"], r["day"], r["word"])
            self._words[key] = self._words.get(key, 0) + r["count"]

    async def _maybe_retention(self) -> None:
        """retention 清理：**每天最多一次**（DELETE 虽走 day 索引也别 30s 一次）。

        在 flush 内直接 await：与 upsert 同一异步上下文，失败同样只记日志
        （不合并回内存——那是写入批次的事，清理失败下个自然日自然重试）。
        """
        today = tz.today_date()
        if self._last_retention_day == today:
            return
        self._last_retention_day = today
        cutoff = date.fromordinal(today.toordinal() - self._retention_days)
        try:
            await self.store.delete_before(cutoff)
        except Exception:
            logger.exception("群统计 retention 清理失败（下个自然日重试）")

    # ----------------------------------------------------------- 观测（测试）

    def pending(self) -> tuple[int, int]:
        """待落库的 (daily 行数, 词条数)——测试与 /status 诊断用。"""
        return len(self._daily), len(self._words)
