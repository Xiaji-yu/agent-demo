"""全量群消息原文归档：PG 存储 + 内存缓冲 + reader + 对账重建。

与同包的聚合管线（collector/store）的分工：
- 聚合（``group_msg_daily``/``group_word_daily``）= 榜单/词云的**现算材料**，
  条数/字数/小时直方图/词频，随时可弃、可从原文重建
- 本模块 = **原文底账**：白名单群每条非 bot 消息一行（含 text/reply_to/
  forward_id），保留 30 天，用于对账、回溯、点评增强（路线甲：聚合管线不变）

为什么必须 PG 而不是 JSONL：核心诉求是**索引查询速度**（按群/按人/按时间窗），
且部署方自管 PG 镜像备份。代价如实记录：停机 flush 不丢；``kill -9`` 丢
≤ flush 周期（默认 30s，``AGENT_GROUP_ARCHIVE_FLUSH_SECONDS``）。

合并转发：**照常归档一行**（``kind='forward'``，存 forward_id 不取内容）——
展开要调协议端 API 且内容是他人历史发言，无实际意义；点评用「转发次数」
这个数字维度表达习惯，不碰内容。被引内容同理只记 ``reply_to``。
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import date, datetime

import asyncpg

from agentcore import tz
from agentcore.groupstats.tokenize import cut

logger = logging.getLogger(__name__)

KIND_FORWARD = "forward"
KIND_IMAGE = "image"
KIND_REPLY = "reply"
KIND_TEXT = "text"

DDL = """
CREATE TABLE IF NOT EXISTS group_messages_raw (
    group_id   TEXT NOT NULL,
    message_id TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    ts         TIMESTAMPTZ NOT NULL,
    day        DATE NOT NULL,
    nickname   TEXT NOT NULL DEFAULT '',
    kind       TEXT NOT NULL DEFAULT 'text',
    text       TEXT NOT NULL DEFAULT '',
    chars      INTEGER NOT NULL DEFAULT 0,
    reply_to   TEXT NOT NULL DEFAULT '',
    forward_id TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (group_id, message_id)
);
CREATE INDEX IF NOT EXISTS gmraw_gut_idx   ON group_messages_raw(group_id, user_id, ts DESC);
CREATE INDEX IF NOT EXISTS gmraw_gt_idx    ON group_messages_raw(group_id, ts DESC);
CREATE INDEX IF NOT EXISTS gmraw_day_idx   ON group_messages_raw(day);
CREATE INDEX IF NOT EXISTS gmraw_reply_idx ON group_messages_raw(group_id, reply_to);
"""


def classify_kind(*, has_forward: bool, has_image: bool, is_reply: bool) -> str:
    """段型 → kind。优先级 forward > image > reply > text（一条消息一个 kind，
    计数口径才 unambiguous；混合形态按最能代表它的类归）。"""
    if has_forward:
        return KIND_FORWARD
    if has_image:
        return KIND_IMAGE
    if is_reply:
        return KIND_REPLY
    return KIND_TEXT


def reply_to_of(segs) -> str:
    """取第一个 reply 段的被引 message_id（只记 id，不取被引内容）。

    自包含实现（不 import plugins.media）：agentcore 分层铁律。协议端 data
    形状不保证是 dict，逐项容错。
    """
    for seg in segs or []:
        stype = str(getattr(seg, "type", "") or "")
        if stype != "reply":
            continue
        data = getattr(seg, "data", None)
        if not isinstance(data, dict):
            continue
        for key in ("id", "message_id"):
            value = data.get(key)
            if value not in (None, ""):
                return str(value)
    return ""


class RawArchiveStore:
    """PG 原文归档存储。**无内存降级**——持久化是该功能的意义本身。"""

    def __init__(self, db_url: str):
        self.db_url = db_url
        self.pool: asyncpg.Pool | None = None

    async def init(self) -> None:
        if self.pool is not None:
            return
        self.pool = await asyncpg.create_pool(self.db_url, min_size=1, max_size=3)
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(DDL)
        except Exception:
            # 池先建、DDL 后跑：失败留非 None 池会让重试首行直接 return
            await self.pool.close()
            self.pool = None
            raise

    async def aclose(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None

    # ----------------------------------------------------------- 写入

    async def insert_rows(self, rows: list[dict]) -> None:
        """批量插入（事务 + ON CONFLICT DO NOTHING → flush 重试幂等）。"""
        if not rows or self.pool is None:
            return
        payload = [
            (
                r["group_id"],
                r["message_id"],
                r["user_id"],
                datetime.fromtimestamp(r["ts"], tz.zoneinfo()),
                r["day"],
                r["nickname"],
                r["kind"],
                r["text"],
                int(r.get("chars") or 0),
                r.get("reply_to") or "",
                r.get("forward_id") or "",
            )
            for r in rows
        ]
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.executemany(
                    "INSERT INTO group_messages_raw "
                    "(group_id, message_id, user_id, ts, day, nickname, kind, "
                    " text, chars, reply_to, forward_id) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) "
                    "ON CONFLICT (group_id, message_id) DO NOTHING",
                    payload,
                )

    async def delete_before(self, day: date) -> None:
        if self.pool is None:
            return
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM group_messages_raw WHERE day < $1", day)

    # ----------------------------------------------------------- 读取

    async def query(
        self,
        group_id: str,
        user_id: str | None,
        start: date,
        end: date,
        limit: int,
    ) -> list[dict]:
        """窗口内消息（ts 降序）。**limit<=0 返回空**（铁律：非正 limit=空结果）。

        按人/按群两条独立 SQL：各自走自己的索引（``$2 IS NULL OR`` 的写法会
        让优化器放弃索引，那是"图省事"的经典反模式）。
        """
        if limit <= 0 or self.pool is None:
            return []
        lo = datetime.combine(start, datetime.min.time(), tzinfo=tz.zoneinfo())
        hi = datetime.combine(end, datetime.max.time(), tzinfo=tz.zoneinfo())
        async with self.pool.acquire() as conn:
            if user_id:
                rows = await conn.fetch(
                    "SELECT group_id, message_id, user_id, "
                    "       to_timestamp(extract(epoch from ts)) AS ts, "
                    "       nickname, kind, text, chars, reply_to, forward_id "
                    "FROM group_messages_raw "
                    "WHERE group_id=$1 AND user_id=$2 AND ts BETWEEN $3 AND $4 "
                    "ORDER BY ts DESC LIMIT $5",
                    group_id,
                    user_id,
                    lo,
                    hi,
                    limit,
                )
            else:
                rows = await conn.fetch(
                    "SELECT group_id, message_id, user_id, "
                    "       to_timestamp(extract(epoch from ts)) AS ts, "
                    "       nickname, kind, text, chars, reply_to, forward_id "
                    "FROM group_messages_raw "
                    "WHERE group_id=$1 AND ts BETWEEN $2 AND $3 "
                    "ORDER BY ts DESC LIMIT $4",
                    group_id,
                    lo,
                    hi,
                    limit,
                )
        return [dict(r) for r in rows]

    async def user_texts(
        self, group_id: str, user_id: str, start: date, end: date, limit: int
    ) -> list[dict]:
        """某用户窗口内的消息（ts 降序）——点评切词/选原句用。"""
        return await self.query(group_id, user_id, start, end, limit)

    async def kind_count(
        self, group_id: str, user_id: str, start: date, end: date, kind: str
    ) -> int:
        """某用户窗口内指定 kind 的条数（转发/图片习惯维度）。"""
        if self.pool is None:
            return 0
        lo = datetime.combine(start, datetime.min.time(), tzinfo=tz.zoneinfo())
        hi = datetime.combine(end, datetime.max.time(), tzinfo=tz.zoneinfo())
        async with self.pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT count(*)::int FROM group_messages_raw "
                "WHERE group_id=$1 AND user_id=$2 AND ts BETWEEN $3 AND $4 AND kind=$5",
                group_id,
                user_id,
                lo,
                hi,
                kind,
            )
        return int(value or 0)

    async def reply_count(
        self, group_id: str, message_ids: list[str], start: date, end: date
    ) -> int:
        """这些 message_id 在窗口内**被引用**的次数（reply_to = ANY(ids)）。"""
        if not message_ids or self.pool is None:
            return 0
        lo = datetime.combine(start, datetime.min.time(), tzinfo=tz.zoneinfo())
        hi = datetime.combine(end, datetime.max.time(), tzinfo=tz.zoneinfo())
        async with self.pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT count(*)::int FROM group_messages_raw "
                "WHERE group_id=$1 AND reply_to = ANY($2::text[]) "
                "AND ts BETWEEN $3 AND $4",
                group_id,
                list(message_ids),
                lo,
                hi,
            )
        return int(value or 0)


class RawArchiveBuffer:
    """内存缓冲：record 同步 O(1)（不进热路径 DB），flush 批量落库。

    失败语义照 stats collector：先换出再写，失败（事务回滚）后按原顺序合并回
    队首，与下一批一起重试；ON CONFLICT DO NOTHING 保证重试不双写。
    超 ``max_rows`` 丢最旧并 ERROR——内存有界，宁可丢也不 OOM。
    """

    def __init__(
        self,
        store: RawArchiveStore,
        *,
        max_rows: int = 50000,
        retention_days: int = 30,
        allowed_groups: frozenset[str] = frozenset(),
    ):
        self.store = store
        self._max = max(1, int(max_rows))
        self._retention = max(1, int(retention_days))
        self._allowed = frozenset(allowed_groups or ())
        self._rows: deque[dict] = deque()
        self._last_prune_day: date | None = None

    def record(
        self,
        *,
        group_id: str,
        user_id: str,
        nickname: str,
        message_id: str,
        text: str,
        ts: float,
        has_image: bool = False,
        forward_id: str = "",
        reply_to: str = "",
    ) -> None:
        """累加一条群消息。任何异常都不外溢（归档是增益功能）。"""
        try:
            gid = str(group_id or "")
            uid = str(user_id or "")
            if not gid or not uid:
                return
            # 白名单群过滤（与 stats collector 同口径；挂点不过滤，这里兜底）
            if self._allowed and gid not in self._allowed:
                return
            body = (text or "").strip()
            mid = str(message_id or "") or f"noids-{int(ts * 1000)}-{uid}"
            moment = datetime.fromtimestamp(ts, tz.zoneinfo())
            if len(self._rows) >= self._max:
                self._rows.popleft()  # 丢最旧（调用方按冷却/频率兜底）
                logger.error(
                    "group archive: 缓冲超过 %d 行上限，丢弃最旧一行（flush 是否卡住？）",
                    self._max,
                )
            self._rows.append(
                {
                    "group_id": gid,
                    "message_id": mid,
                    "user_id": uid,
                    "ts": float(ts),
                    "day": moment.date(),
                    "nickname": (nickname or "")[:32],
                    "kind": classify_kind(
                        has_forward=bool(forward_id),
                        has_image=bool(has_image),
                        is_reply=bool(reply_to),
                    ),
                    "text": body[:2000],  # 单条截断：防超长刷屏文本占爆存储
                    "chars": len(body),
                    "reply_to": str(reply_to or ""),
                    "forward_id": str(forward_id or ""),
                }
            )
        except Exception:
            logger.warning("group archive record failed", exc_info=True)

    async def flush(self) -> None:
        """批量落库；失败合并回队首等下一批（事务回滚 ⇒ 重试安全）。"""
        if not self._rows:
            return
        rows = list(self._rows)
        self._rows.clear()
        try:
            await self.store.insert_rows(rows)
        except Exception:
            self._rows.extendleft(reversed(rows))
            logger.exception("group archive flush 失败，已合并回缓冲等下一批重试")
            return
        await self._maybe_prune()

    async def _maybe_prune(self) -> None:
        """retention 清理：**每天最多一次**（DELETE 走 day 索引也别每 30s 一次）。"""
        today = tz.today_date()
        if self._last_prune_day == today:
            return
        self._last_prune_day = today
        cutoff = date.fromordinal(today.toordinal() - self._retention)
        try:
            await self.store.delete_before(cutoff)
        except Exception:
            logger.exception("group archive retention 清理失败（下个自然日重试）")

    def pending(self) -> int:
        return len(self._rows)


def recompute_window(rows: list[dict]) -> dict:
    """从归档原文重算窗口聚合（对账用）：per-user 条数/字数 + 词频。

    口径与实时采集对齐：**只计有文本的消息**（stats 的 record 跳过空文本，
    纯图片行两边都不计）；词频用同一套 ``cut()``（停用词/长度规则同源）。
    停用词表在两次计算之间变过的话对账必然有差——用例固定窗口与配置。
    """
    per_user: dict[str, dict] = {}
    words: dict[str, int] = {}
    for r in rows:
        text = (r.get("text") or "").strip()
        if not text:
            continue
        uid = str(r.get("user_id") or "")
        cur = per_user.setdefault(uid, {"msgs": 0, "chars": 0})
        cur["msgs"] += 1
        cur["chars"] += len(text)
        for w in cut(text):
            words[w] = words.get(w, 0) + 1
    return {"per_user": per_user, "words": words}
