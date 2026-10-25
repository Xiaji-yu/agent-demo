"""群统计存储：PG 双表 + 内存降级，语义一致（参数化测试锁死）。

两张表（独立 DDL，不进内存 store 的迁移基线）：

- ``group_msg_daily``  每用户每日一条：条数/字数/小时直方图/名片快照
- ``group_word_daily`` 每群每日每词一条：词频计数

**不存消息原文**——这是本功能与 group_context「绝不落库」决裂后的隐私收敛
（opt-in + 仅白名单群 + 只存聚合，BACKLOG.md 有行为变更记录）。

``hours`` 用 JSONB 稀疏直方图（``{"19": 430}``）而非 24 元素数组：PG 数组
逐元素相加要写 24 段 SQL，而 jsonb ``||`` 对同键是**覆盖不是求和**——所以
合并必须在应用层做（读-改-写）。flush 是进程内单写者（单事件循环串行 await），
读-改-写无竞态；asyncpg JSONB 出库是 str 的已知坑（§5 tool_calls 同款），
出库一律反序列化。
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from datetime import date

import asyncpg

logger = logging.getLogger(__name__)

# 自建表（AGENTS.md §4 的登记例外 / BACKLOG §2.8）：不受 schema_migrations
# 的校验和与版本守卫覆盖。表集合由 test_stats_tables_match_embedded_ddl 锁住，
# 新增/改名必须同时更新 memory/schema.py 的 STATS_TABLES。
DDL = """
CREATE TABLE IF NOT EXISTS group_msg_daily (
    group_id   TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    day        DATE NOT NULL,
    nickname   TEXT NOT NULL DEFAULT '',
    msg_count  INTEGER NOT NULL DEFAULT 0,
    char_count INTEGER NOT NULL DEFAULT 0,
    hours      JSONB NOT NULL DEFAULT '{}',
    PRIMARY KEY (group_id, user_id, day)
);
CREATE TABLE IF NOT EXISTS group_word_daily (
    group_id TEXT NOT NULL,
    day      DATE NOT NULL,
    word     TEXT NOT NULL,
    count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (group_id, day, word)
);
-- LLM 个人点评缓存：按 (群, 周窗, 用户) 一条；stats 是生成时的事实快照，
-- 缓存命中直接渲染、零重算（保证「评语与它依据的数据」同源）
CREATE TABLE IF NOT EXISTS group_reviews (
    group_id   TEXT NOT NULL,
    week_start DATE NOT NULL,
    user_id    TEXT NOT NULL,
    text       TEXT NOT NULL,
    stats      JSONB NOT NULL DEFAULT '{}',
    PRIMARY KEY (group_id, week_start, user_id)
);
-- retention 每日 DELETE 按 day 过滤；PK 前缀是 group_id，day 单列没索引可走
CREATE INDEX IF NOT EXISTS group_msg_daily_day_idx ON group_msg_daily(day);
CREATE INDEX IF NOT EXISTS group_word_daily_day_idx ON group_word_daily(day);
CREATE INDEX IF NOT EXISTS group_reviews_week_idx ON group_reviews(group_id, week_start);
"""


def _parse_stats(value) -> dict:
    """stats 列 → dict；容忍 PG 出库为 str 的已知坑（tool_calls 同款）与坏数据。"""
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            logger.warning("group_reviews.stats 不是合法 JSON，按空处理")
            return {}
    return value if isinstance(value, dict) else {}


def _parse_hours(value) -> dict[int, int]:
    """hours 列 → {小时: 计数}；容忍 PG 出库为 str 的已知坑与坏数据。"""
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            logger.warning("group_msg_daily.hours 不是合法 JSON，按空处理")
            return {}
    if not isinstance(value, dict):
        return {}
    out: dict[int, int] = {}
    for k, v in value.items():
        try:
            out[int(k)] = out.get(int(k), 0) + int(v)
        except (TypeError, ValueError):
            continue
    return out


# 用户可控文本（群名片/分词结果）落库前剥掉控制字符：PG 的 text 列不接受
# NUL(0x00)（asyncpg CharacterNotInRepertoireError），而名片来自协议端、
# 完全用户可控——2026-10-20 一条含 NUL 的群名片把全部白名单群的 flush 毒化
# 95 分钟（executemany 整批失败 ⇒ 合并回内存反复重试 ⇒ 所有群统计停写）。
# 保留 \t\n\r（PG 允许），口径与 loop/engine.py、workspace/fs.py 同款。
_CONTROL_RE = re.compile(r"[\x00-\x08\x0e-\x1f\x7f]")


def _clean_text(value: str) -> str:
    """剥掉控制字符（含 NUL）；其余原样返回。落库边界单点，双实现共用。"""
    return _CONTROL_RE.sub("", value or "")


class StatsStore(ABC):
    """存储抽象。rows 形状见 upsert_daily。"""

    @abstractmethod
    async def init(self) -> None: ...

    @abstractmethod
    async def aclose(self) -> None: ...

    @abstractmethod
    async def upsert_daily(self, rows: list[dict]) -> None:
        """累加每日聚合行。row = {group_id, user_id, day(date), nickname,
        msgs, chars, hours({int:int} 增量)}。重复 upsert 同一行必须双倍累计
        （flush 重试安全依赖此语义）。"""

    @abstractmethod
    async def upsert_words(self, rows: list[dict]) -> None:
        """累加词频行。row = {group_id, day(date), word, count(增量)}。"""

    @abstractmethod
    async def delete_before(self, day: date) -> None: ...

    @abstractmethod
    async def user_ranking(
        self, group_id: str, start: date, end: date, top_n: int
    ) -> list[dict]:
        """窗口内每用户合计，按条数降序。**top_n<=0 返回空列表**（铁律：
        非正 limit = 空结果，不是"去掉最后 N 条"）。"""

    @abstractmethod
    async def nicknames(self, group_id: str, start: date, end: date) -> dict[str, str]:
        """窗口内每 user_id 的**最后一天**名片快照。"""

    @abstractmethod
    async def hour_histogram(
        self, group_id: str, start: date, end: date
    ) -> dict[int, int]:
        """窗口内 24 小时直方图合并值。"""

    @abstractmethod
    async def word_freq(
        self, group_id: str, start: date, end: date, limit: int
    ) -> list[tuple[str, int]]:
        """窗口内词频降序；limit<=0 返回空列表。"""

    @abstractmethod
    async def group_totals(self, start: date, end: date) -> list[dict]:
        """窗口内每群合计（Web 只读页用）：[{group_id, msgs, users}]。"""

    @abstractmethod
    async def window_total(self, group_id: str, start: date, end: date) -> int:
        """窗口内该群**全部群友**条数——榜单百分比的分母（不是 Top N 之和）。"""

    @abstractmethod
    async def user_window_stats(
        self, group_id: str, user_id: str, start: date, end: date
    ) -> dict:
        """单用户窗口合计 + 群内排名。

        返回 ``{"msgs","chars","days","rank","users"}``：rank = 「条数比我高的
        人数 + 1」（不拉全量榜单，2000 人群也只一次聚合查询）；无数据返回全零、
        rank=0（调用方据此走「数据不足」分支）。
        """

    @abstractmethod
    async def upsert_review(
        self, group_id: str, week_start: date, user_id: str, text: str, stats: dict
    ) -> None:
        """写入/覆盖一条个人点评缓存（同键覆盖，不累计）。"""

    @abstractmethod
    async def get_review(
        self, group_id: str, week_start: date, user_id: str
    ) -> dict | None:
        """取缓存的个人点评：``{"text","stats"}`` 或 None。"""


class InMemoryStatsStore(StatsStore):
    """无 PG 时的降级实现：进程内有效，重启清零（flush 语义不变）。"""

    def __init__(self) -> None:
        self._daily: dict[tuple[str, str, date], dict] = {}
        self._words: dict[tuple[str, date, str], int] = {}
        self._reviews: dict[tuple[str, date, str], dict] = {}

    async def init(self) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def upsert_daily(self, rows: list[dict]) -> None:
        for r in rows:
            key = (str(r["group_id"]), str(r["user_id"]), r["day"])
            cur = self._daily.get(key)
            if cur is None:
                cur = {
                    "group_id": key[0],
                    "user_id": key[1],
                    "day": key[2],
                    "nickname": "",
                    "msgs": 0,
                    "chars": 0,
                    "hours": {},
                }
                self._daily[key] = cur
            # 先剥控制字符再判空/截断：剥净后为空的名片不覆盖旧值
            # （与 PgStatsStore 的 CASE WHEN excluded.nickname <> '' 同语义）
            nick = _clean_text(str(r.get("nickname") or ""))
            if nick:
                cur["nickname"] = nick[:32]
            cur["msgs"] += int(r.get("msgs") or 0)
            cur["chars"] += int(r.get("chars") or 0)
            for h, n in (r.get("hours") or {}).items():
                h = int(h)
                cur["hours"][h] = cur["hours"].get(h, 0) + int(n)

    async def upsert_words(self, rows: list[dict]) -> None:
        for r in rows:
            key = (str(r["group_id"]), r["day"], _clean_text(str(r["word"])))
            self._words[key] = self._words.get(key, 0) + int(r.get("count") or 0)

    async def delete_before(self, day: date) -> None:
        for key in [k for k in self._daily if k[2] < day]:
            del self._daily[key]
        for key in [k for k in self._words if k[1] < day]:
            del self._words[key]
        for key in [k for k in self._reviews if k[1] < day]:
            del self._reviews[key]

    async def user_ranking(
        self, group_id: str, start: date, end: date, top_n: int
    ) -> list[dict]:
        if top_n <= 0:
            return []
        agg: dict[str, dict] = {}
        for (gid, uid, day), row in self._daily.items():
            if gid != group_id or not (start <= day <= end):
                continue
            cur = agg.setdefault(uid, {"user_id": uid, "msgs": 0, "chars": 0})
            cur["msgs"] += row["msgs"]
            cur["chars"] += row["chars"]
        return sorted(agg.values(), key=lambda r: (-r["msgs"], r["user_id"]))[:top_n]

    async def nicknames(self, group_id: str, start: date, end: date) -> dict[str, str]:
        out: dict[str, str] = {}
        # 先按天升序、同天后者覆盖：最终保留窗口内最后一天的名片
        for (gid, uid, day), row in sorted(
            self._daily.items(), key=lambda kv: (kv[0][1], kv[0][2])
        ):
            if gid != group_id or not (start <= day <= end):
                continue
            if row["nickname"]:
                out[uid] = row["nickname"]
        return out

    async def hour_histogram(
        self, group_id: str, start: date, end: date
    ) -> dict[int, int]:
        out: dict[int, int] = {}
        for (gid, _uid, day), row in self._daily.items():
            if gid != group_id or not (start <= day <= end):
                continue
            for h, n in row["hours"].items():
                out[h] = out.get(h, 0) + n
        return out

    async def word_freq(
        self, group_id: str, start: date, end: date, limit: int
    ) -> list[tuple[str, int]]:
        if limit <= 0:
            return []
        agg: dict[str, int] = {}
        for (gid, day, word), count in self._words.items():
            if gid != group_id or not (start <= day <= end):
                continue
            agg[word] = agg.get(word, 0) + count
        return sorted(agg.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]

    async def group_totals(self, start: date, end: date) -> list[dict]:
        agg: dict[str, dict] = {}
        for (gid, uid, day), row in self._daily.items():
            if not (start <= day <= end):
                continue
            cur = agg.setdefault(gid, {"group_id": gid, "msgs": 0, "users": set()})
            cur["msgs"] += row["msgs"]
            cur["users"].add(uid)
        out = [
            {"group_id": gid, "msgs": v["msgs"], "users": len(v["users"])}
            for gid, v in agg.items()
        ]
        return sorted(out, key=lambda r: (-r["msgs"], r["group_id"]))

    async def window_total(self, group_id: str, start: date, end: date) -> int:
        return sum(
            row["msgs"]
            for (gid, _uid, day), row in self._daily.items()
            if gid == group_id and start <= day <= end
        )

    async def user_window_stats(
        self, group_id: str, user_id: str, start: date, end: date
    ) -> dict:
        mine = {"msgs": 0, "chars": 0, "days": 0}
        totals: dict[str, int] = {}
        for (gid, uid, day), row in self._daily.items():
            if gid != group_id or not (start <= day <= end):
                continue
            totals[uid] = totals.get(uid, 0) + row["msgs"]
            if uid == user_id:
                mine["msgs"] += row["msgs"]
                mine["chars"] += row["chars"]
                mine["days"] += 1
        rank = 1 + sum(1 for v in totals.values() if v > mine["msgs"])
        return {
            "msgs": mine["msgs"],
            "chars": mine["chars"],
            "days": mine["days"],
            "rank": rank if mine["msgs"] else 0,
            "users": len(totals),
        }

    async def upsert_review(
        self, group_id: str, week_start: date, user_id: str, text: str, stats: dict
    ) -> None:
        self._reviews[(str(group_id), week_start, str(user_id))] = {
            "text": str(text or ""),
            "stats": dict(stats or {}),
        }

    async def get_review(
        self, group_id: str, week_start: date, user_id: str
    ) -> dict | None:
        row = self._reviews.get((str(group_id), week_start, str(user_id)))
        if row is None:
            return None
        return {"text": row["text"], "stats": dict(row["stats"])}


class PgStatsStore(StatsStore):
    """M1+：PostgreSQL 持久化。DDL 幂等（CREATE TABLE IF NOT EXISTS）。"""

    def __init__(self, db_url: str):
        self.db_url = db_url
        self.pool: asyncpg.Pool | None = None

    async def init(self) -> None:
        # 与 PgMemoryStore.init 同款：重复调用复用池（否则旧池泄漏）
        if self.pool is not None:
            return
        self.pool = await asyncpg.create_pool(self.db_url, min_size=1, max_size=3)
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(DDL)
        except Exception:
            # 池先建、DDL 后跑：失败留非 None 池会让重试首行直接 return，
            # DDL 永远不补跑（半成品 store）
            await self.pool.close()
            self.pool = None
            raise

    async def aclose(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    async def upsert_daily(self, rows: list[dict]) -> None:
        if not rows or self.pool is None:
            return
        # hours 要在应用层与库里现值合并（jsonb || 是同键覆盖不是求和），
        # 所以先按 (group_id, day) 批量读回，合并后整行覆写。
        gids = sorted({str(r["group_id"]) for r in rows})
        days = sorted({r["day"] for r in rows})
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                existing = await conn.fetch(
                    "SELECT group_id, user_id, day, hours FROM group_msg_daily "
                    "WHERE group_id = ANY($1::text[]) AND day = ANY($2::date[])",
                    gids,
                    days,
                )
                merged: dict[tuple[str, str, date], dict[int, int]] = {
                    (str(r["group_id"]), str(r["user_id"]), r["day"]): _parse_hours(
                        r["hours"]
                    )
                    for r in existing
                }
                payload = []
                for r in rows:
                    key = (str(r["group_id"]), str(r["user_id"]), r["day"])
                    hours = dict(merged.get(key) or {})
                    for h, n in (r.get("hours") or {}).items():
                        h = int(h)
                        hours[h] = hours.get(h, 0) + int(n)
                    payload.append(
                        (
                            key[0],
                            key[1],
                            key[2],
                            _clean_text(str(r.get("nickname") or ""))[:32],
                            int(r.get("msgs") or 0),
                            int(r.get("chars") or 0),
                            json.dumps({str(h): n for h, n in sorted(hours.items())}),
                        )
                    )
                await conn.executemany(
                    "INSERT INTO group_msg_daily "
                    "(group_id, user_id, day, nickname, msg_count, char_count, hours) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb) "
                    "ON CONFLICT (group_id, user_id, day) DO UPDATE SET "
                    "  nickname = CASE WHEN excluded.nickname <> '' "
                    "                 THEN excluded.nickname "
                    "                 ELSE group_msg_daily.nickname END, "
                    "  msg_count  = group_msg_daily.msg_count + excluded.msg_count, "
                    "  char_count = group_msg_daily.char_count + excluded.char_count, "
                    "  hours = $7::jsonb",
                    payload,
                )

    async def upsert_words(self, rows: list[dict]) -> None:
        if not rows or self.pool is None:
            return
        payload = [
            (
                str(r["group_id"]),
                r["day"],
                _clean_text(str(r["word"])),
                int(r.get("count") or 0),
            )
            for r in rows
        ]
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.executemany(
                    "INSERT INTO group_word_daily (group_id, day, word, count) "
                    "VALUES ($1,$2,$3,$4) "
                    "ON CONFLICT (group_id, day, word) DO UPDATE SET "
                    "  count = group_word_daily.count + excluded.count",
                    payload,
                )

    async def delete_before(self, day: date) -> None:
        if self.pool is None:
            return
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM group_msg_daily WHERE day < $1", day)
            await conn.execute("DELETE FROM group_word_daily WHERE day < $1", day)
            await conn.execute("DELETE FROM group_reviews WHERE week_start < $1", day)

    async def user_ranking(
        self, group_id: str, start: date, end: date, top_n: int
    ) -> list[dict]:
        if top_n <= 0 or self.pool is None:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id, SUM(msg_count)::int AS msgs, "
                "       SUM(char_count)::int AS chars "
                "FROM group_msg_daily "
                "WHERE group_id=$1 AND day BETWEEN $2 AND $3 "
                "GROUP BY user_id ORDER BY msgs DESC, user_id ASC LIMIT $4",
                group_id,
                start,
                end,
                top_n,
            )
        return [dict(r) for r in rows]

    async def nicknames(self, group_id: str, start: date, end: date) -> dict[str, str]:
        if self.pool is None:
            return {}
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT ON (user_id) user_id, nickname "
                "FROM group_msg_daily "
                "WHERE group_id=$1 AND day BETWEEN $2 AND $3 AND nickname <> '' "
                "ORDER BY user_id, day DESC",
                group_id,
                start,
                end,
            )
        return {str(r["user_id"]): str(r["nickname"]) for r in rows}

    async def hour_histogram(
        self, group_id: str, start: date, end: date
    ) -> dict[int, int]:
        if self.pool is None:
            return {}
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT hours FROM group_msg_daily "
                "WHERE group_id=$1 AND day BETWEEN $2 AND $3",
                group_id,
                start,
                end,
            )
        out: dict[int, int] = {}
        for r in rows:
            for h, n in _parse_hours(r["hours"]).items():
                out[h] = out.get(h, 0) + n
        return out

    async def word_freq(
        self, group_id: str, start: date, end: date, limit: int
    ) -> list[tuple[str, int]]:
        if limit <= 0 or self.pool is None:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT word, SUM(count)::int AS c FROM group_word_daily "
                "WHERE group_id=$1 AND day BETWEEN $2 AND $3 "
                "GROUP BY word ORDER BY c DESC, word ASC LIMIT $4",
                group_id,
                start,
                end,
                limit,
            )
        return [(str(r["word"]), int(r["c"])) for r in rows]

    async def group_totals(self, start: date, end: date) -> list[dict]:
        if self.pool is None:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT group_id, SUM(msg_count)::int AS msgs, "
                "       COUNT(DISTINCT user_id)::int AS users "
                "FROM group_msg_daily WHERE day BETWEEN $1 AND $2 "
                "GROUP BY group_id ORDER BY msgs DESC, group_id ASC",
                start,
                end,
            )
        return [dict(r) for r in rows]

    async def window_total(self, group_id: str, start: date, end: date) -> int:
        if self.pool is None:
            return 0
        async with self.pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT COALESCE(SUM(msg_count), 0)::int FROM group_msg_daily "
                "WHERE group_id=$1 AND day BETWEEN $2 AND $3",
                group_id,
                start,
                end,
            )
        return int(value or 0)

    async def user_window_stats(
        self, group_id: str, user_id: str, start: date, end: date
    ) -> dict:
        if self.pool is None:
            return {"msgs": 0, "chars": 0, "days": 0, "rank": 0, "users": 0}
        # 一次 CTE 同时拿本人合计/活跃天数/排名/群活跃人数——2000 人群也是
        # 一次分组聚合，不拉全量榜单进应用层
        row = await self.pool.fetchrow(
            """
            WITH totals AS (
                SELECT user_id,
                       SUM(msg_count)::int  AS msgs,
                       SUM(char_count)::int AS chars,
                       COUNT(*)::int         AS days
                FROM group_msg_daily
                WHERE group_id=$1 AND day BETWEEN $2 AND $3
                GROUP BY user_id
            )
            SELECT
                (SELECT msgs  FROM totals WHERE user_id=$4) AS msgs,
                (SELECT chars FROM totals WHERE user_id=$4) AS chars,
                (SELECT days  FROM totals WHERE user_id=$4) AS days,
                (SELECT COUNT(*)+1 FROM totals
                  WHERE msgs > COALESCE((SELECT msgs FROM totals WHERE user_id=$4), 0)
                ) AS rank,
                (SELECT COUNT(*) FROM totals) AS users
            """,
            group_id,
            start,
            end,
            user_id,
        )
        if row is None:
            return {"msgs": 0, "chars": 0, "days": 0, "rank": 0, "users": 0}
        msgs = int(row["msgs"] or 0)
        return {
            "msgs": msgs,
            "chars": int(row["chars"] or 0),
            "days": int(row["days"] or 0),
            "rank": int(row["rank"] or 0) if msgs else 0,
            "users": int(row["users"] or 0),
        }

    async def upsert_review(
        self, group_id: str, week_start: date, user_id: str, text: str, stats: dict
    ) -> None:
        if self.pool is None:
            return
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO group_reviews (group_id, week_start, user_id, text, stats) "
                "VALUES ($1,$2,$3,$4,$5::jsonb) "
                "ON CONFLICT (group_id, week_start, user_id) DO UPDATE SET "
                "  text = excluded.text, stats = excluded.stats",
                str(group_id),
                week_start,
                str(user_id),
                str(text or ""),
                json.dumps(stats or {}, ensure_ascii=False),
            )

    async def get_review(
        self, group_id: str, week_start: date, user_id: str
    ) -> dict | None:
        if self.pool is None:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT text, stats FROM group_reviews "
                "WHERE group_id=$1 AND week_start=$2 AND user_id=$3",
                str(group_id),
                week_start,
                str(user_id),
            )
        if row is None:
            return None
        return {"text": str(row["text"]), "stats": _parse_stats(row["stats"])}


async def open_store(db_url: str) -> StatsStore:
    """按 DATABASE_URL 选实现并 init。db_url 为空 = 内存降级。"""
    store: StatsStore = PgStatsStore(db_url) if db_url else InMemoryStatsStore()
    await store.init()
    return store
