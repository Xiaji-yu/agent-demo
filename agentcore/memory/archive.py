"""聊天记录归档：把每条消息同时追加写到 DB 之外的 JSONL 文件，滚动保留 N 天。

为什么要有它（而不是只依赖数据库备份）：

- 数据库备份是「定期快照」，两次快照之间写入的数据会丢；归档是**逐条追加**的
- 归档落在 DB 之外（普通文件），任何针对数据库的误操作（TRUNCATE/DROP/写坏）
  都碰不到它——这正是上次删库事件里唯一能救命的东西
- 蒸馏可以把它当输入源，库被清空后知识仍能继续从归档里沉淀
- 万一要恢复，它是可直接 grep 的明文记录

文件按天切分：`<root>/messages-YYYY-MM-DD.jsonl`，每天一行一条 JSON；超过保留
天数的文件在每日任务里删除（滚动覆盖）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterator
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_KEEP_DAYS = 7
_MAX_SCAN_LINES = 200_000  # read_since 单次最多扫描行数（防超大归档拖慢）


def _day_str(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


class MessageArchive:
    """按天切分的 JSONL 消息归档。写失败绝不影响对话（只记日志）。"""

    def __init__(self, root: str | Path, keep_days: int = DEFAULT_KEEP_DAYS):
        self.root = Path(root)
        self.keep_days = max(1, int(keep_days))

    # ---------- 写入 ----------
    def path_for_day(self, day: str) -> Path:
        return self.root / f"messages-{day}.jsonl"

    async def append(self, record: dict) -> None:
        rec = dict(record)
        rec.setdefault("ts", time.time())
        path = self.path_for_day(_day_str(float(rec["ts"])))
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        try:
            await asyncio.to_thread(self._append_sync, path, line)
        except Exception:
            logger.exception("archive append failed (message kept in DB): %s", path.name)

    @staticmethod
    def _append_sync(path: Path, line: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)

    # ---------- 滚动清理 ----------
    def prune(self, now: float | None = None) -> list[str]:
        """删除超过保留天数的归档文件，返回被删文件名。"""
        now = now or time.time()
        cutoff = _day_str(now - self.keep_days * 86400)
        removed: list[str] = []
        if not self.root.is_dir():
            return removed
        for p in sorted(self.root.glob("messages-*.jsonl")):
            day = p.stem.replace("messages-", "")
            if day < cutoff:  # YYYY-MM-DD 字典序即时序
                try:
                    p.unlink()
                    removed.append(p.name)
                except OSError:
                    logger.exception("archive prune failed: %s", p)
        if removed:
            logger.info("archive: pruned %d file(s) older than %s: %s", len(removed), cutoff, removed)
        return removed

    async def prune_async(self, now: float | None = None) -> list[str]:
        return await asyncio.to_thread(self.prune, now)

    # ---------- 读取 ----------
    def _files(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        return sorted(self.root.glob("messages-*.jsonl"))

    def iter_records(
        self, after_id: int = 0, since_day: str | None = None, until_day: str | None = None
    ) -> Iterator[dict]:
        """按时间顺序产出完整记录（含身份字段，仅供恢复使用）。

        since_day/until_day 为 YYYY-MM-DD，可按天范围恢复（例如只恢复最后 3 天）。
        """
        for path in self._files():
            day = path.stem.replace("messages-", "")
            if since_day and day < since_day:
                continue
            if until_day and day > until_day:
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            logger.warning("archive: skipping corrupt line in %s", path.name)
                            continue
                        if int(rec.get("id") or 0) > after_id:
                            yield rec
            except OSError:
                logger.exception("archive read failed: %s", path)

    def read_since(self, after_id: int, limit: int = 200) -> list[dict]:
        """返回与 store.messages_after 同形状的记录（**不含身份字段**，供蒸馏使用）。"""
        out: list[dict] = []
        scanned = 0
        for rec in self.iter_records(after_id):
            scanned += 1
            if scanned > _MAX_SCAN_LINES:
                logger.warning("archive: read_since hit scan cap (%d lines)", _MAX_SCAN_LINES)
                break
            out.append(
                {
                    "id": int(rec.get("id") or 0),
                    "session_id": str(rec.get("session_id") or ""),
                    "role": rec.get("role") or "",
                    "content": rec.get("content") or "",
                }
            )
            if len(out) >= limit:
                break
        out.sort(key=lambda r: r["id"])
        return out

    def latest_message_id(self) -> int:
        """归档中出现过的最大消息 id（与数据库 MAX(id) 语义一致）。

        逐行扫描，有上限保护：归档是 7 天滚动的小文件，日常代价可忽略。
        """
        newest = 0
        scanned = 0
        for path in self._files():
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        scanned += 1
                        if scanned > _MAX_SCAN_LINES:
                            logger.warning("archive: latest_message_id hit scan cap")
                            return newest
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            newest = max(newest, int(json.loads(line).get("id") or 0))
                        except Exception:
                            continue
            except OSError:
                logger.exception("archive read failed: %s", path)
        return newest

    def days(self, since_day: str | None = None, until_day: str | None = None) -> list[str]:
        """归档里存在的日期列表（可限定范围），供恢复时展示。"""
        out = []
        for path in self._files():
            day = path.stem.replace("messages-", "")
            if since_day and day < since_day:
                continue
            if until_day and day > until_day:
                continue
            out.append(day)
        return out

    def stats(self) -> dict:
        files = self._files()
        total = 0
        lines = 0
        for p in files:
            try:
                total += p.stat().st_size
                with open(p, encoding="utf-8", errors="replace") as f:
                    lines += sum(1 for _ in f)
            except OSError:
                continue
        return {
            "files": len(files),
            "bytes": total,
            "records": lines,
            "oldest": files[0].stem.replace("messages-", "") if files else None,
            "newest": files[-1].stem.replace("messages-", "") if files else None,
            "keep_days": self.keep_days,
        }


class ArchivingStore:
    """给任意 MemoryStore 套一层归档：写入消息时顺手落一份到 JSONL。

    其余接口全部透传（`__getattr__`），因此对上层完全透明；
    归档失败只记日志，绝不影响对话。
    """

    def __init__(self, inner, archive: MessageArchive):
        self._inner = inner
        self._archive = archive
        self._identity_cache: dict[str, tuple[str, str | None]] = {}

    def __getattr__(self, name):
        return getattr(self._inner, name)

    @property
    def inner(self):
        return self._inner

    @property
    def archive(self) -> MessageArchive:
        """供蒸馏读取归档（DB ∪ 归档）。"""
        return self._archive

    async def _identity(self, session_id: str) -> tuple[str, str | None]:
        cached = self._identity_cache.get(str(session_id))
        if cached is not None:
            return cached
        ident: tuple[str, str | None] = ("", None)
        try:
            get_ident = getattr(self._inner, "get_session_identity", None)
            if get_ident is not None:
                ident = await get_ident(str(session_id))
        except Exception:
            logger.exception("archive: session identity lookup failed: %s", session_id)
        self._identity_cache[str(session_id)] = ident
        return ident

    async def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_calls=None,
        tool_call_id: str | None = None,
    ):
        msg_id = await self._inner.append_message(
            session_id, role, content, tool_calls=tool_calls, tool_call_id=tool_call_id
        )
        try:
            user_id, group_id = await self._identity(session_id)
            await self._archive.append(
                {
                    "id": msg_id,
                    "session_id": str(session_id),
                    "user_id": user_id,
                    "group_id": group_id,
                    "role": role,
                    "content": content,
                    "tool_calls": tool_calls,
                    "tool_call_id": tool_call_id,
                    "ts": time.time(),
                }
            )
        except Exception:
            logger.exception("archive: append wrapper failed for session %s", session_id)
        return msg_id
