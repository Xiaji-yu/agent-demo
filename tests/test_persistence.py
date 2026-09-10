"""记录保全：聊天记录归档（7 天滚动）+ 数据库备份/恢复。

这一层是「防止再次删库导致数据不可恢复」，所以测试重点不是覆盖率，而是
**真的能不能把数据找回来**：导出 → 清空 → 回灌 → 逐表核对。
"""
import asyncio
import gzip
import hashlib
import io
import json
import logging
import os
import stat
import time
import types
from pathlib import Path

import pytest

from agentcore.memory.archive import ArchivingStore, MessageArchive
from agentcore.memory.store import InMemoryMemoryStore

# ---------- 归档：写入 / 读取 / 滚动 ----------


class TestMessageArchive:
    @pytest.fixture
    def archive(self, tmp_path):
        return MessageArchive(tmp_path / "archive", keep_days=7)

    @pytest.mark.asyncio
    async def test_append_creates_daily_file(self, archive):
        await archive.append({"id": 1, "session_id": "s1", "role": "user", "content": "你好"})
        files = list(archive.root.glob("messages-*.jsonl"))
        assert len(files) == 1
        rec = json.loads(files[0].read_text(encoding="utf-8").strip())
        assert rec["content"] == "你好" and rec["id"] == 1

    @pytest.mark.asyncio
    async def test_read_since_filters_and_sorts(self, archive):
        for i in range(1, 6):
            await archive.append({"id": i, "session_id": "s1", "role": "user", "content": f"m{i}"})
        rows = archive.read_since(2, limit=10)
        assert [r["content"] for r in rows] == ["m3", "m4", "m5"]
        assert rows[0]["id"] == 3

    @pytest.mark.asyncio
    async def test_read_since_omits_identity_fields(self, archive):
        await archive.append(
            {"id": 1, "session_id": "s1", "user_id": "12345", "group_id": "999",
             "role": "user", "content": "内容"}
        )
        row = archive.read_since(0)[0]
        # user_id 绝不外带；group_id 保留供蒸馏侧做私聊过滤（M1）
        assert "user_id" not in row and row["group_id"] == "999"
        assert set(row) == {"id", "session_id", "group_id", "role", "content"}

    @pytest.mark.asyncio
    async def test_latest_message_id(self, archive):
        for i in (3, 7, 5):
            await archive.append({"id": i, "session_id": "s", "role": "user", "content": "x"})
        assert archive.latest_message_id() == 7

    def test_prune_keeps_recent_days(self, archive):
        now = time.time()
        old_day = time.strftime("%Y-%m-%d", time.localtime(now - 10 * 86400))
        edge_day = time.strftime("%Y-%m-%d", time.localtime(now - 6 * 86400))
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        for day in (old_day, edge_day, today):
            p = archive.path_for_day(day)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('{"id":1}\n', encoding="utf-8")

        removed = archive.prune(now=now)
        assert removed == [f"messages-{old_day}.jsonl"]
        assert archive.path_for_day(edge_day).exists()
        assert archive.path_for_day(today).exists()

    def test_stats(self, archive):
        for day in ("2026-01-01", "2026-01-02"):
            p = archive.path_for_day(day)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('{"id":1}\n{"id":2}\n', encoding="utf-8")
        st = archive.stats()
        assert st["files"] == 2 and st["records"] == 4 and st["oldest"] == "2026-01-01"

    @pytest.mark.asyncio
    async def test_corrupt_line_is_skipped(self, archive):
        p = archive.path_for_day(time.strftime("%Y-%m-%d"))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"id": 1, "role": "user", "content": "ok"}\nNOT JSON\n', encoding="utf-8")
        assert [r["content"] for r in archive.read_since(0)] == ["ok"]

    def test_iter_records_cap_counts_read_lines(self, archive):
        """L6：上限按**读取**的行数计——即使水位线落后把大部分行过滤掉，
        解析工作也被封顶，不会全量 json.loads。"""
        p = archive.path_for_day("2026-01-01")
        p.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps({"id": 1000 + i, "session_id": "s", "role": "user", "content": f"c{i}"})
            for i in range(10)
        ]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")

        # 读 3 行（id 1000-1002），全被 after_id=1005 过滤 → 产出为空，但确实只读了 3 行
        assert list(archive.iter_records(1005, limit=3)) == []
        # 同样的 after_id，放开上限就能读到后面的行
        assert [r["id"] for r in archive.iter_records(1005, limit=10)] == list(range(1006, 1010))
        assert len(list(archive.iter_records(0, limit=4))) == 4

    def test_latest_message_id_respects_read_cap(self, archive, monkeypatch):
        """L6：latest_message_id 也走 iter_records 的读取上限。"""
        from agentcore.memory import archive as archive_mod

        p = archive.path_for_day("2026-01-01")
        p.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps({"id": i, "session_id": "s", "role": "user", "content": "x"})
            for i in range(1, 11)
        ]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")

        monkeypatch.setattr(archive_mod, "_MAX_SCAN_LINES", 4)
        assert archive.latest_message_id() == 4  # 只读到前 4 行，最大 id=4

    @pytest.mark.skipif(os.name != "posix", reason="POSIX 权限位")
    @pytest.mark.asyncio
    async def test_archive_file_is_owner_only(self, tmp_path):
        """M5：归档是逐条明文聊天记录，落盘即 0600。"""
        archive = MessageArchive(tmp_path / "archive")
        await archive.append({"id": 1, "session_id": "s", "role": "user", "content": "x"})
        p = next(archive.root.glob("messages-*.jsonl"))
        assert stat.S_IMODE(p.stat().st_mode) == 0o600


class TestArchivingStore:
    @pytest.mark.asyncio
    async def test_writes_land_in_both_store_and_archive(self, tmp_path):
        inner = InMemoryMemoryStore()
        archive = MessageArchive(tmp_path / "archive", keep_days=7)
        store = ArchivingStore(inner, archive)

        sid = await store.resolve_session("u1", "g1")
        msg_id = await store.append_message(sid, "user", "记一下")
        assert msg_id == 1

        # 库里有
        assert (await store.get_history(sid))[0]["content"] == "记一下"
        # 归档里也有，且带身份字段（供恢复用）
        rec = next(archive.iter_records())
        assert rec["content"] == "记一下"
        assert rec["user_id"] == "u1" and rec["group_id"] == "g1"

    @pytest.mark.asyncio
    async def test_delegates_other_methods(self, tmp_path):
        inner = InMemoryMemoryStore()
        store = ArchivingStore(inner, MessageArchive(tmp_path / "a"))
        # 透传：facts / kb / 身份查询都照常工作
        await store.save_fact("u1", "住在北京", [1.0, 0.0])
        assert await store.list_facts("u1") == ["住在北京"]
        assert (await store.kb_stats())["sources"] == 0
        assert store.archive is not None

    @pytest.mark.asyncio
    async def test_archive_failure_does_not_break_conversation(self, tmp_path, monkeypatch):
        inner = InMemoryMemoryStore()
        archive = MessageArchive(tmp_path / "archive")

        async def boom(*a, **k):
            raise OSError("disk full")

        monkeypatch.setattr(archive, "append", boom)
        store = ArchivingStore(inner, archive)
        sid = await store.resolve_session("u1", None)
        await store.append_message(sid, "user", "照样要能写进库")
        assert (await store.get_history(sid))[0]["content"] == "照样要能写进库"

    @pytest.mark.asyncio
    async def test_identity_lookup_failure_is_not_cached(self, tmp_path):
        """M11：空身份/查询失败不入缓存——否则一次 DB 抖动会把该 session 之后
        所有归档记录的归属永久打成 unknown（恢复时混进同一会话）。"""
        class FlakyInner:
            def __init__(self):
                self.calls = 0
                self.fail = True

            async def append_message(self, session_id, role, content, **kwargs):
                return 1

            async def get_session_identity(self, session_id):
                self.calls += 1
                if self.fail:
                    raise RuntimeError("db jitter")
                return ("u-recovered", None)

        inner = FlakyInner()
        archive = MessageArchive(tmp_path / "archive")
        store = ArchivingStore(inner, archive)

        await store.append_message("s1", "user", "x")
        # 归档容忍失败：身份为空也照样落归档
        assert next(archive.iter_records())["user_id"] == ""
        # 但失败不入缓存
        assert "s1" not in store._identity_cache
        assert inner.calls == 1

        # DB 恢复后，同一 session 下次重试即可拿到正确身份
        inner.fail = False
        await store.append_message("s1", "user", "y")
        recs = list(archive.iter_records())
        assert recs[-1]["user_id"] == "u-recovered"
        assert "s1" in store._identity_cache

    @pytest.mark.asyncio
    async def test_identity_cache_is_bounded(self, tmp_path):
        """M11：缓存有上限，超限后不再新增（查询本身不受影响）。"""
        from agentcore.memory.archive import _IDENTITY_CACHE_LIMIT

        class Inner:
            async def append_message(self, session_id, role, content, **kwargs):
                return 1

            async def get_session_identity(self, session_id):
                return (f"u-{session_id}", None)

        store = ArchivingStore(Inner(), MessageArchive(tmp_path / "a"))
        for i in range(_IDENTITY_CACHE_LIMIT + 10):
            await store._identity(f"s{i}")
        assert len(store._identity_cache) == _IDENTITY_CACHE_LIMIT
        # 超限后的新会话：查得到身份但不入缓存
        assert await store._identity("brand-new") == ("u-brand-new", None)
        assert "brand-new" not in store._identity_cache


# ---------- 蒸馏把归档并进来 ----------


class TestDistillUsesArchive:
    @pytest.mark.asyncio
    async def test_archive_fills_in_when_db_is_empty(self, tmp_path):
        """库被清空 / 落后时，蒸馏仍能从归档拿到内容（关键防事故属性）。

        M1/H4 后首跑水位线初始化为最新 id（存量历史不回灌），所以这里先给 KB
        留下「已蒸馏到 id=1」的水位线，再模拟库丢失：归档里 id=2 的消息仍能
        继续沉淀，知识链不断流。
        """
        from agentcore.rag.distill import distill_from_memory
        from tests.test_rag import FakeEmbedding, FakeLLM, _tc

        inner = InMemoryMemoryStore()
        archive = MessageArchive(tmp_path / "archive")
        store = ArchivingStore(inner, archive)

        sid = await store.resolve_session("u1", "g1")
        await store.append_message(sid, "user", "沙箱白名单怎么防绕过？" * 20)
        await store.append_message(sid, "assistant", "必须逐参数校验并禁用 find -exec。" * 20)
        # KB 已蒸馏到 id=1（distill 水位线留痕，非首跑）
        await store.kb_add_source(
            "记忆蒸馏", "distill", location="memory", meta={"last_message_id": 1}
        )

        # 模拟数据库内容丢失（归档还在）
        inner.messages.clear()

        llm = FakeLLM(_tc("沙箱安全", ["白名单必须逐参数校验并禁用 find -exec"]))
        result = await distill_from_memory(llm, store, FakeEmbedding(), min_chars=10)
        assert result["status"] == "ok" and result["chunks"] == 1
        assert "archive" in result["source"]

    @pytest.mark.asyncio
    async def test_db_and_archive_are_deduped(self, tmp_path):
        """库与归档同一条消息只蒸馏一次。"""
        from agentcore.rag.distill import _collect_messages

        inner = InMemoryMemoryStore()
        store = ArchivingStore(inner, MessageArchive(tmp_path / "archive"))
        # M1 后私聊消息默认不进蒸馏输入，这里用群聊会话验证去重
        sid = await store.resolve_session("u1", "g1")
        await store.append_message(sid, "user", "内容。" * 100)

        rows, note = await _collect_messages(store, 0, 200)
        assert len(rows) == 1, "库里与归档里的同一条消息不能重复"
        assert note.startswith("db+archive")


# ---------- 数据库备份 / 恢复（需要 PG） ----------

PG = os.getenv("TEST_DATABASE_URL")
pg_only = pytest.mark.skipif(not PG, reason="TEST_DATABASE_URL not set")


def _write_valid_gzip(path: Path, content: bytes = b"") -> None:
    """写一份完好的 gzip 文件（M6 后 prune/verify 会把垃圾字节判坏）。"""
    with gzip.open(path, "wb") as fh:
        fh.write(content)


def _write_valid_jsonl_gz(path: Path, rows=()) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({"__meta__": {}}) + "\n")
        for table, rid in rows:
            fh.write(json.dumps({"table": table, "row": {"id": rid}}) + "\n")


@pg_only
class TestBackupRestore:
    @pytest.mark.asyncio
    async def test_jsonl_export_then_restore_roundtrip(self, tmp_path):
        """核心保证：导出 → 清空 → 回灌 → 数据一条不少。

        M4：必须包含 user_state 行（人格数据）——它没有 id 列，曾是恢复必炸点。
        """
        from agentcore.backup import backup_database, list_backups, restore_database
        from agentcore.memory.store import PgMemoryStore

        store = PgMemoryStore(PG, dim=8)
        await store.init()
        try:
            async with store.pool.acquire() as c:
                await c.execute("TRUNCATE messages, sessions, facts, kb_sources, kb_chunks CASCADE")
                await c.execute("DELETE FROM user_state")
            sid = await store.resolve_session("u-backup", "g-backup")
            for i in range(5):
                await store.append_message(sid, "user", f"备份消息 {i}")
            await store.save_fact("u-backup", "事实一", [0.5] * 8, session_id=sid)
            await store.kb_add_source("来源一", "manual")
            await store.set_user_persona("u-backup", "严谨的技术评审")
            before = await store.kb_stats()

            result = await backup_database(PG, tmp_path, keep=7, strategy="jsonl")
            assert result["bytes"] > 0
            counts = json.loads(json.dumps(result))
            assert counts["rows"] >= 7

            # M6：备份文件 0600、有 .sha256 sidecar，且 sidecar/part 不影响 list 语义
            dump = Path(result["path"])
            if os.name == "posix":
                assert stat.S_IMODE(dump.stat().st_mode) == 0o600
            sidecar = Path(f"{dump}.sha256")
            assert sidecar.is_file()
            assert sidecar.read_text(encoding="utf-8").strip() == hashlib.sha256(
                dump.read_bytes()
            ).hexdigest()
            assert [i["name"] for i in list_backups(tmp_path)] == [dump.name]

            # 模拟事故：清空（user_state 无外键，CASCADE 不波及，显式清掉）
            async with store.pool.acquire() as c:
                await c.execute("TRUNCATE messages, sessions, facts, kb_sources, kb_chunks CASCADE")
                await c.execute("DELETE FROM user_state")
            assert await store.latest_message_id() == 0
            assert (await store.kb_stats())["sources"] == 0
            assert await store.get_user_persona("u-backup") is None

            # 回灌
            out = await restore_database(PG, result["path"])
            assert out["restored"]["messages"] == 5
            assert out["restored"]["sessions"] >= 1
            assert out["restored"]["user_state"] == 1

            # 核对
            async with store.pool.acquire() as c:
                msgs = await c.fetchval("SELECT count(*) FROM messages")
                facts = await c.fetchval("SELECT count(*) FROM facts")
            assert msgs == 5 and facts == 1
            assert (await store.kb_stats())["sources"] == before["sources"]
            assert await store.list_facts("u-backup") == ["事实一"]
            assert await store.get_user_persona("u-backup") == "严谨的技术评审"
        finally:
            await store.aclose()

    @pytest.mark.asyncio
    async def test_restore_skips_rows_with_injected_identifiers(self, tmp_path):
        """M3：被篡改的备份把表名/列名拼进 SQL 的注入企图必须被拦下——
        恶意行跳过并计失败，库结构原封不动。"""
        from agentcore.backup import restore_database
        from agentcore.memory.store import PgMemoryStore

        store = PgMemoryStore(PG, dim=8)
        await store.init()
        try:
            async with store.pool.acquire() as c:
                await c.execute("TRUNCATE messages, sessions CASCADE")
            bomb = tmp_path / "agent-demo-evil.jsonl.gz"
            with gzip.open(bomb, "wt", encoding="utf-8") as fh:
                fh.write(json.dumps({"__meta__": {}}) + "\n")
                # 表名注入
                fh.write(json.dumps({
                    "table": 'messages"; DROP TABLE messages; --',
                    "row": {"id": 1, "session_id": None, "role": "user", "content": "pwn"},
                }, ensure_ascii=False) + "\n")
                # 列名注入
                fh.write(json.dumps({
                    "table": "messages",
                    "row": {
                        "id": 2, "role": "user", "content": "pwn",
                        "content) VALUES (1); DROP TABLE messages; --": "x",
                    },
                }, ensure_ascii=False) + "\n")

            with pytest.raises(RuntimeError, match="恢复失败"):
                await restore_database(PG, bomb)

            async with store.pool.acquire() as c:
                assert await c.fetchval("SELECT to_regclass('messages')") is not None
                assert await c.fetchval("SELECT count(*) FROM messages") == 0
        finally:
            await store.aclose()

    @pytest.mark.asyncio
    async def test_restore_fails_loud_on_bad_row_and_keeps_good_rows(self, tmp_path):
        """M4：单行坏数据（不存在的列）→ SAVEPOINT 隔离：好行照常恢复，
        恢复结束时整体报错，绝不静默打印 ✅。"""
        from agentcore.backup import restore_database
        from agentcore.memory.store import PgMemoryStore

        store = PgMemoryStore(PG, dim=8)
        await store.init()
        try:
            async with store.pool.acquire() as c:
                await c.execute("TRUNCATE messages, sessions CASCADE")
            dump = tmp_path / "agent-demo-badrow.jsonl.gz"
            with gzip.open(dump, "wt", encoding="utf-8") as fh:
                fh.write(json.dumps({"__meta__": {}}) + "\n")
                # 合法行（session_id=None 可空）
                fh.write(json.dumps({
                    "table": "messages",
                    "row": {"id": 501, "session_id": None, "role": "user", "content": "好行"},
                }, ensure_ascii=False) + "\n")
                # 坏行：列名合法但列不存在
                fh.write(json.dumps({
                    "table": "messages",
                    "row": {"id": 502, "session_id": None, "role": "user",
                            "content": "坏行", "no_such_column": 1},
                }, ensure_ascii=False) + "\n")

            with pytest.raises(RuntimeError, match=r"1 行未能恢复.*messages"):
                await restore_database(PG, dump)

            # SAVEPOINT 隔离：坏行没有连坐好行
            async with store.pool.acquire() as c:
                assert await c.fetchval("SELECT count(*) FROM messages") == 1
                assert await c.fetchval("SELECT content FROM messages") == "好行"
        finally:
            await store.aclose()

    @pytest.mark.asyncio
    async def test_restore_is_idempotent(self, tmp_path):
        from agentcore.backup import backup_database, restore_database
        from agentcore.memory.store import PgMemoryStore

        store = PgMemoryStore(PG, dim=8)
        await store.init()
        try:
            async with store.pool.acquire() as c:
                await c.execute("TRUNCATE messages, sessions, facts CASCADE")
            sid = await store.resolve_session("u-idem", None)
            await store.append_message(sid, "user", "只此一条")
            dump = await backup_database(PG, tmp_path, strategy="jsonl")
            await restore_database(PG, dump["path"])  # 已存在 → 不重复插入
            async with store.pool.acquire() as c:
                assert await c.fetchval("SELECT count(*) FROM messages") == 1
                assert await c.fetchval("SELECT count(*) FROM sessions WHERE user_id='u-idem'") == 1
        finally:
            await store.aclose()

    @pytest.mark.asyncio
    async def test_restore_after_sequence_drift(self, tmp_path):
        """恢复后序列要跟着最大 id 走，否则新消息会撞主键。"""
        from agentcore.backup import backup_database, restore_database
        from agentcore.memory.store import PgMemoryStore

        store = PgMemoryStore(PG, dim=8)
        await store.init()
        try:
            async with store.pool.acquire() as c:
                await c.execute("TRUNCATE messages, sessions, facts CASCADE")
            sid = await store.resolve_session("u-seq", None)
            for i in range(3):
                await store.append_message(sid, "user", f"m{i}")
            dump = await backup_database(PG, tmp_path, strategy="jsonl")
            async with store.pool.acquire() as c:
                await c.execute("TRUNCATE messages CASCADE")
                await c.execute("ALTER SEQUENCE messages_id_seq RESTART WITH 1")
            await restore_database(PG, dump["path"])
            # 恢复后还能继续写而不冲突
            new_id = await store.append_message(sid, "user", "恢复之后的新消息")
            assert new_id > 3
        finally:
            await store.aclose()

    @pytest.mark.asyncio
    async def test_pg_dump_backup_and_restore(self, tmp_path):
        """pg_dump 路径（宿主机或容器内）。不可用则跳过。"""
        from agentcore.backup import backup_database, restore_database
        from agentcore.backup.db_backup import find_pg_dump

        if find_pg_dump() is None:
            pytest.skip("本机无 pg_dump / docker 可用")
        from agentcore.memory.store import PgMemoryStore

        store = PgMemoryStore(PG, dim=8)
        await store.init()
        try:
            async with store.pool.acquire() as c:
                await c.execute("TRUNCATE messages, sessions, facts CASCADE")
            sid = await store.resolve_session("u-dump", "g-dump")
            await store.append_message(sid, "user", "pg_dump 备份的消息")
            result = await backup_database(PG, tmp_path, strategy="pg_dump")
            assert result["path"].endswith(".sql.gz")
            with gzip.open(result["path"], "rt", encoding="utf-8", errors="replace") as fh:
                head = fh.read(2000)
            assert "PostgreSQL database dump" in head

            async with store.pool.acquire() as c:
                await c.execute("TRUNCATE messages, sessions, facts CASCADE")
            out = await restore_database(PG, result["path"])
            assert out["restored"] is True
            async with store.pool.acquire() as c:
                assert await c.fetchval("SELECT count(*) FROM messages") == 1
        finally:
            await store.aclose()


class TestBackupHousekeeping:
    def test_prune_backups_keeps_newest(self, tmp_path):
        from agentcore.backup import list_backups, prune_backups

        for i in range(5):
            p = tmp_path / f"agent-demo-2026-01-0{i + 1}.jsonl.gz"
            _write_valid_gzip(p, b'{"__meta__": {}}\n')
            os.utime(p, (1000 + i, 1000 + i))
        removed = prune_backups(tmp_path, keep=2)
        assert len(removed) == 3
        assert len(list_backups(tmp_path)) == 2

    def test_verify_rejects_truncated_gzip(self, tmp_path):
        """M6：半截 .sql.gz（进程中断的典型产物）必须判坏，全量解压读完才算数。"""
        from agentcore.backup import verify_backup

        p = tmp_path / "agent-demo-2026-01-01.sql.gz"
        _write_valid_gzip(p, b"-- " + b"x" * 100000)
        raw = p.read_bytes()
        p.write_bytes(raw[: len(raw) // 2])
        result = verify_backup(p)
        assert result["ok"] is False

    def test_verify_rejects_bad_jsonl_line(self, tmp_path):
        from agentcore.backup import verify_backup

        p = tmp_path / "agent-demo-2026-01-01.jsonl.gz"
        with gzip.open(p, "wt", encoding="utf-8") as fh:
            fh.write('{"__meta__": {}}\n{"table": "messages", "row": {"id": 1}}\nNOT JSON\n')
        assert verify_backup(p)["ok"] is False

    def test_verify_checks_sidecar_checksum(self, tmp_path):
        """M6：有 .sha256 sidecar 时校验一致；被篡改/截断但 sidecar 未更新 → 判坏。"""
        from agentcore.backup import verify_backup

        p = tmp_path / "agent-demo-2026-01-01.jsonl.gz"
        _write_valid_jsonl_gz(p, [("messages", 1), ("messages", 2), ("facts", 3)])
        sidecar = Path(f"{p}.sha256")
        sidecar.write_text(hashlib.sha256(p.read_bytes()).hexdigest() + "\n", encoding="utf-8")

        result = verify_backup(p)
        assert result["ok"] is True
        assert result["checksum"] == "verified"
        assert result["tables"] == {"messages": 2, "facts": 1}

        # 篡改内容后 sidecar 不再匹配
        with gzip.open(p, "at", encoding="utf-8") as fh:
            fh.write('{"table": "messages", "row": {"id": 99}}\n')
        tampered = verify_backup(p)
        assert tampered["ok"] is False and tampered["checksum"] == "mismatch"

    def test_verify_without_sidecar_checks_readability_only(self, tmp_path):
        """旧备份没有 sidecar：只做全量可读性检查，仍算完好。"""
        from agentcore.backup import verify_backup

        p = tmp_path / "agent-demo-2026-01-01.jsonl.gz"
        _write_valid_jsonl_gz(p, [("messages", 1)])
        result = verify_backup(p)
        assert result["ok"] is True and result["checksum"] == "missing"

    def test_list_backups_ignores_sidecars_and_part_files(self, tmp_path):
        """M6：.sha256 / .part 不是备份——glob 语义保持，轮转与展示不受影响。"""
        from agentcore.backup import list_backups

        p = tmp_path / "agent-demo-2026-01-01.sql.gz"
        _write_valid_gzip(p, b"-- dump")
        Path(f"{p}.sha256").write_text("ab" * 32, encoding="utf-8")
        Path(f"{p}.part").write_bytes(b"junk")
        assert [i["name"] for i in list_backups(tmp_path)] == ["agent-demo-2026-01-01.sql.gz"]

    def test_prune_deletes_corrupt_before_retention(self, tmp_path):
        """M6：坏备份先删（哪怕它 mtime 最新），保留期只对完好备份计数。"""
        from agentcore.backup import list_backups, prune_backups, verify_backup

        for i, day in enumerate(("01", "02", "03")):
            p = tmp_path / f"agent-demo-2026-01-{day}.jsonl.gz"
            _write_valid_jsonl_gz(p, [("messages", i)])
            os.utime(p, (1000 + i, 1000 + i))
        # 半截备份顶着最新 mtime——正是「事故时才发现不能用」的形态
        corrupt = tmp_path / "agent-demo-2026-01-04.jsonl.gz"
        _write_valid_gzip(corrupt, b"x" * 100000)
        raw = corrupt.read_bytes()
        corrupt.write_bytes(raw[: len(raw) // 2])
        os.utime(corrupt, (1000 + 3, 1000 + 3))
        assert verify_backup(corrupt)["ok"] is False

        removed = prune_backups(tmp_path, keep=2)
        assert "agent-demo-2026-01-04.jsonl.gz" in removed  # 坏的先删
        assert "agent-demo-2026-01-01.jsonl.gz" in removed  # 剩余按保留期删最旧
        remaining = {i["name"] for i in list_backups(tmp_path)}
        assert remaining == {
            "agent-demo-2026-01-02.jsonl.gz",
            "agent-demo-2026-01-03.jsonl.gz",
        }

    def test_prune_removes_sidecar_with_backup(self, tmp_path):
        from agentcore.backup import prune_backups

        old = tmp_path / "agent-demo-2026-01-01.jsonl.gz"
        _write_valid_jsonl_gz(old, [("messages", 1)])
        Path(f"{old}.sha256").write_text(
            hashlib.sha256(old.read_bytes()).hexdigest(), encoding="utf-8"
        )
        os.utime(old, (1000, 1000))
        new = tmp_path / "agent-demo-2026-01-02.jsonl.gz"
        _write_valid_jsonl_gz(new, [("messages", 2)])

        removed = prune_backups(tmp_path, keep=1)
        assert removed == ["agent-demo-2026-01-01.jsonl.gz"]
        assert not Path(f"{old}.sha256").exists(), "sidecar 必须随备份一起删掉"

    @pytest.mark.skipif(os.name != "posix", reason="POSIX 权限位")
    def test_harden_restricts_backup_permissions(self, tmp_path):
        """M5：备份含全部聊天记录/人格数据，落盘即 0600。"""
        from agentcore.backup.db_backup import _harden

        p = tmp_path / "x.sql.gz"
        _write_valid_gzip(p)
        os.chmod(p, 0o644)
        _harden(p)
        assert stat.S_IMODE(p.stat().st_mode) == 0o600

    @pytest.mark.skipif(os.name != "posix", reason="fcntl 文件锁")
    @pytest.mark.asyncio
    async def test_concurrent_backup_is_rejected_by_lock(self, tmp_path):
        """L13：已有备份持锁时，第二次备份直接报「已有备份在运行」，
        绝不并行交叉写坏同一当日文件（锁在连库之前拿，本用例离线可跑）。"""
        import fcntl

        from agentcore.backup import backup_database

        lock_path = tmp_path / ".backup.lock"
        with open(lock_path, "w") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(RuntimeError, match="已有备份在运行"):
                await backup_database(
                    "postgresql://nobody@127.0.0.1:1/none", tmp_path, strategy="jsonl"
                )

    @pytest.mark.asyncio
    async def test_unreachable_db_backup_raises(self, tmp_path):
        """连不上库时备份必须报错（不能静默产出空备份，那比没有备份更危险）。"""
        import asyncpg

        from agentcore.backup import backup_database

        with pytest.raises((OSError, asyncpg.PostgresError, ConnectionError, TimeoutError)):
            await backup_database(
                "postgresql://nobody@127.0.0.1:1/none", tmp_path, strategy="jsonl"
            )


class TestDockerPgDumpPassword:
    @pytest.mark.asyncio
    async def test_docker_fallback_passes_password_via_dash_e(self, tmp_path, monkeypatch, caplog):
        """L15：容器内进程读不到宿主机环境变量，PGPASSWORD 必须经 -e 传进容器，
        且密码不得出现在日志里。"""
        from agentcore.backup import db_backup

        captured = {}

        class FakeProc:
            returncode = 0
            stdout = io.BytesIO(b"-- PostgreSQL database dump\n")

            def wait(self, timeout=None):
                return 0

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = list(cmd)
            return FakeProc()

        monkeypatch.setattr(
            db_backup.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None
        )
        monkeypatch.setattr(
            db_backup.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0)
        )
        monkeypatch.setattr(db_backup.subprocess, "Popen", fake_popen)

        with caplog.at_level(logging.DEBUG, logger="agentcore.backup.db_backup"):
            result = await db_backup._backup_pg_dump(
                "postgresql://admin:s3cret-pw@db-host:5432/agent_demo", tmp_path, "agent-demo"
            )

        assert result is not None
        cmd = captured["cmd"]
        assert cmd[0] == "docker" and "pg_dump" in cmd
        assert "-e" in cmd and "PGPASSWORD=s3cret-pw" in cmd
        # -e 是 docker exec 的参数：必须在容器名之前（pg_dump 自己的 -e 是 --extension）
        container = os.getenv("PG_CONTAINER", "agent-demo-db-1")
        assert cmd.index("PGPASSWORD=s3cret-pw") < cmd.index(container)
        assert "s3cret-pw" not in caplog.text

    @pytest.mark.asyncio
    async def test_pg_dump_failure_includes_stderr_tail(self, tmp_path, monkeypatch):
        """L14：pg_dump 失败时，stderr（落临时文件而非 PIPE）尾部要进异常消息。"""
        from agentcore.backup import db_backup

        class FakeProc:
            returncode = 3
            stdout = io.BytesIO(b"")

            def wait(self, timeout=None):
                return 3

        def fake_popen(cmd, **kwargs):
            # 真实代码把 stderr 重定向到临时文件：模拟 pg_dump 往里写诊断输出
            kwargs["stderr"].write(b"pg_dump: error: FATAL password authentication failed")
            return FakeProc()

        monkeypatch.setattr(
            db_backup.shutil, "which", lambda name: "/usr/bin/pg_dump" if name == "pg_dump" else None
        )
        monkeypatch.setattr(db_backup.subprocess, "Popen", fake_popen)

        with pytest.raises(RuntimeError, match="FATAL password authentication failed"):
            await db_backup._backup_pg_dump(
                "postgresql://u:p@127.0.0.1:5432/db", tmp_path, "agent-demo"
            )


def test_archive_module_import_paths():
    """归档与备份都在 agentcore.backup 下可导入（文档/脚本依赖这个路径）。"""
    from agentcore.backup import ArchivingStore as A
    from agentcore.backup import MessageArchive as M

    assert A is ArchivingStore and M is MessageArchive


@pytest.mark.asyncio
async def test_archive_concurrent_appends_are_intact(tmp_path):
    """并发追加不能写出半行（每条记录一行，整行可解析）。"""
    archive = MessageArchive(tmp_path / "archive")
    await asyncio.gather(
        # 真实消息 id 从 1 开始（read_since 语义是 id > after_id）
        *[archive.append({"id": i, "session_id": "s", "role": "user", "content": f"c{i}"}) for i in range(1, 51)]
    )
    recs = list(archive.iter_records())
    assert sorted(r["id"] for r in recs) == list(range(1, 51))


class TestConfigAndDocs:
    """配置/文档漂移守卫：保全相关的开关必须同时出现在 config.yaml 与 .env.example。"""

    def test_config_has_archive_and_backup_sections(self):
        import yaml

        cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
        assert cfg["archive"]["keep_days"] == 7
        assert cfg["archive"]["dir"]
        assert cfg["backup"]["keep"] >= 1
        assert cfg["backup"]["cron"].count(" ") == 4, "backup.cron 应为 5 段 cron"

    def test_env_example_documents_switches(self):
        text = open(".env.example", encoding="utf-8").read()
        for key in (
            "AGENT_ARCHIVE_ENABLED", "AGENT_ARCHIVE_KEEP_DAYS",
            "AGENT_BACKUP_ENABLED", "AGENT_BACKUP_KEEP", "AGENT_BACKUP_CRON",
            "PG_CONTAINER",
        ):
            assert key in text, f".env.example 缺少 {key}"

    def test_gitignore_excludes_private_data(self):
        """归档与备份含隐私内容，必须被 git 忽略（否则会推到远端）。"""
        text = open(".gitignore", encoding="utf-8").read()
        assert "data/archive/" in text
        assert "data/backups/" in text

    def test_readme_documents_restore_steps(self):
        text = open("README.md", encoding="utf-8").read()
        assert "backup_db.py" in text, "README 必须给出备份/恢复命令"
        assert "restore" in text


class TestBackupMirror:
    """异地镜像：单机备份挡得住误删，挡不住盘坏——镜像到第二块盘/NAS 才算副本。"""

    def test_mirror_copies_and_rotates(self, tmp_path):
        from agentcore.backup import list_backups, prune_backups
        from agentcore.backup.db_backup import _mirror_backup

        src_dir = tmp_path / "backups"
        mirror = tmp_path / "nas"
        src_dir.mkdir()
        src = src_dir / "agent-demo-2026-01-01.sql.gz"
        _write_valid_gzip(src, b"dump")

        ok, dst = _mirror_backup(src, mirror, keep=2, tag="agent-demo")
        assert ok and Path(dst).is_file()
        with gzip.open(Path(dst), "rb") as fh:
            assert fh.read() == b"dump"

        # 老备份在镜像侧也要轮转掉（镜像侧文件同样要能通过校验才参与保留计数）
        for day in ("02", "03"):
            p = mirror / f"agent-demo-2026-01-{day}.sql.gz"
            _write_valid_gzip(p, b"x")
            os.utime(p, (1000 + int(day), 1000 + int(day)))
        prune_backups(mirror, keep=2)
        assert len(list_backups(mirror)) == 2

    def test_mirror_failure_does_not_fail_local_backup(self, tmp_path, monkeypatch):
        """镜像目录不可写时：本地备份仍成功，但结果里明确标出没镜像上。"""
        from agentcore.backup.db_backup import _mirror_backup

        src = tmp_path / "agent-demo-2026-01-01.sql.gz"
        src.write_bytes(b"dump")

        def boom(*a, **k):
            raise OSError("NAS 掉线")

        monkeypatch.setattr("agentcore.backup.db_backup.shutil.copy2", boom)
        ok, dst = _mirror_backup(src, tmp_path / "nas", keep=2, tag="agent-demo")
        assert ok is False and dst is None

    @pytest.mark.skipif(not PG, reason="TEST_DATABASE_URL not set")
    @pytest.mark.asyncio
    async def test_backup_reports_mirror_result(self, tmp_path):
        from agentcore.backup import backup_database

        result = await backup_database(
            PG, tmp_path / "local", strategy="jsonl", mirror_dir=tmp_path / "nas"
        )
        assert result["mirrored"] is True
        assert Path(result["mirror_path"]).is_file()
        assert Path(result["mirror_path"]).stat().st_size == result["bytes"]


# ---------- 从归档回灌（删库后的最后手段） ----------

PG2 = os.getenv("TEST_DATABASE_URL")
pg_only2 = pytest.mark.skipif(not PG2, reason="TEST_DATABASE_URL not set")


@pg_only2
class TestArchiveRestore:
    @pytest.mark.asyncio
    async def test_replays_archive_into_empty_database(self, tmp_path):
        """核心场景：库被清空 → 仅凭归档把消息与会话找回来（含群聊）。"""
        from agentcore.backup import restore_from_archive
        from agentcore.memory.store import PgMemoryStore

        archive = MessageArchive(tmp_path / "archive")
        store = ArchivingStore(PgMemoryStore(PG2, dim=8), archive)
        await store.init()
        try:
            async with store.pool.acquire() as c:
                await c.execute("TRUNCATE messages, sessions, facts CASCADE")
            priv = await store.resolve_session("u-arch", None)
            grp = await store.resolve_session("u-arch", "g-arch")
            await store.append_message(priv, "user", "私聊消息一")
            await store.append_message(priv, "assistant", "私聊回复一")
            await store.append_message(grp, "user", "群里的消息")
            # TRUNCATE 不重置 SERIAL：id 不保证从 1 开始，只断言数量与顺序
            ids = sorted(int(r["id"]) for r in archive.iter_records())
            assert len(ids) == 3 and ids == sorted(ids)

            # 事故：全清
            async with store.pool.acquire() as c:
                await c.execute("TRUNCATE messages, sessions CASCADE")
            assert await store.pool.fetchval("SELECT count(*) FROM messages") == 0

            # 演练
            dry = await restore_from_archive(PG2, tmp_path / "archive", dry_run=True)
            assert dry["status"] == "dry-run" and dry["records"] == 3

            # 真回灌
            out = await restore_from_archive(PG2, tmp_path / "archive", dry_run=False)
            assert out["messages_inserted"] == 3
            assert out["sessions_created"] == 2  # 私聊 + 群

            # 消息按原 id 回来，且归属到正确的会话
            async with store.pool.acquire() as c:
                rows = await c.fetch(
                    "SELECT m.id, m.content, s.user_id, s.group_id FROM messages m "
                    "JOIN sessions s ON s.id = m.session_id ORDER BY m.id"
                )
            assert [int(r["id"]) for r in rows] == ids  # 按原 id 回来
            assert rows[0]["user_id"] == "u-arch" and rows[0]["group_id"] is None
            assert rows[2]["group_id"] == "g-arch"
        finally:
            await store.aclose()

    @pytest.mark.asyncio
    async def test_replay_is_idempotent_and_fixes_sequence(self, tmp_path):
        from agentcore.backup import restore_from_archive
        from agentcore.memory.store import PgMemoryStore

        archive = MessageArchive(tmp_path / "archive")
        store = ArchivingStore(PgMemoryStore(PG2, dim=8), archive)
        await store.init()
        try:
            async with store.pool.acquire() as c:
                await c.execute("TRUNCATE messages, sessions, facts CASCADE")
            sid = await store.resolve_session("u-idem2", None)
            for i in range(3):
                await store.append_message(sid, "user", f"m{i}")

            async with store.pool.acquire() as c:
                await c.execute("TRUNCATE messages CASCADE")
                await c.execute("ALTER SEQUENCE messages_id_seq RESTART WITH 1")

            first = await restore_from_archive(PG2, tmp_path / "archive", dry_run=False)
            assert first["messages_inserted"] == 3
            second = await restore_from_archive(PG2, tmp_path / "archive", dry_run=False)
            assert second["messages_inserted"] == 0 and second["messages_skipped"] == 3

            # 序列跟着最大 id 走：后续写入不撞主键
            new_id = await store.append_message(sid, "user", "回灌之后的新消息")
            assert new_id > first["records"]  # 序列已跟到最大 id 之后
        finally:
            await store.aclose()

    @pytest.mark.asyncio
    async def test_day_range_filter(self, tmp_path):
        from agentcore.backup import restore_from_archive
        from agentcore.memory.store import PgMemoryStore

        archive = MessageArchive(tmp_path / "archive")
        # 直接造两个不同日期的归档文件
        for day, mid in (("2026-01-01", 1), ("2026-01-02", 2)):
            p = archive.path_for_day(day)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                json.dumps({"id": mid, "session_id": "1", "user_id": "u-day",
                            "group_id": None, "role": "user", "content": f"{day} 的消息"}) + "\n",
                encoding="utf-8",
            )

        dry = await restore_from_archive(
            PG2, tmp_path / "archive", since_day="2026-01-02", dry_run=True
        )
        assert dry["records"] == 1 and dry["days"] == ["2026-01-02"]

        store = PgMemoryStore(PG2, dim=8)
        await store.init()
        try:
            async with store.pool.acquire() as c:
                await c.execute("TRUNCATE messages, sessions CASCADE")
            out = await restore_from_archive(
                PG2, tmp_path / "archive", since_day="2026-01-02", dry_run=False
            )
            assert out["messages_inserted"] == 1
            async with store.pool.acquire() as c:
                content = await c.fetchval("SELECT content FROM messages")
            assert "2026-01-02" in content
        finally:
            await store.aclose()

    @pytest.mark.asyncio
    async def test_empty_archive_reports_empty(self, tmp_path):
        from agentcore.backup import restore_from_archive

        out = await restore_from_archive(PG2, tmp_path / "no-such-dir", dry_run=True)
        assert out["status"] == "empty"


class TestArchiveRestoreGuards:
    def test_cli_requires_confirmation(self, tmp_path, monkeypatch):
        """回灌会写库，必须显式 --yes（或先 --dry-run）。"""
        import subprocess
        import sys

        proc = subprocess.run(
            [sys.executable, "scripts/backup_db.py", "restore-archive", "--archive-dir", str(tmp_path)],
            capture_output=True, text=True,
            env={**os.environ, "DATABASE_URL": "postgresql://x@127.0.0.1:1/x"},
        )
        assert proc.returncode == 2
        assert "拒绝执行" in (proc.stderr + proc.stdout)

    def test_cli_dry_run_wiring(self, tmp_path):
        """回归：CLI 子命令必须真的能跑起来（曾因漏导入 restore_from_archive 而 NameError，
        而守卫在调用前就退出，导致旧测试抓不到）。空归档目录 → 不需要连库。"""
        import subprocess
        import sys

        proc = subprocess.run(
            [sys.executable, "scripts/backup_db.py", "restore-archive",
             "--archive-dir", str(tmp_path / "empty"), "--dry-run"],
            capture_output=True, text=True,
            env={**os.environ, "DATABASE_URL": "postgresql://x@127.0.0.1:1/x"},
        )
        assert proc.returncode == 0, proc.stderr[-500:]
        assert "没有记录" in proc.stdout
        assert "NameError" not in proc.stderr


class TestRestoreCliGuards:
    """恢复 CLI 的安全护栏（M4 fail-loud / M7 恢复前快照）。"""

    @staticmethod
    def _load_cli():
        import importlib.util

        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location("backup_db_cli", root / "scripts" / "backup_db.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    @staticmethod
    def _args(file: str, backup_dir: Path) -> types.SimpleNamespace:
        return types.SimpleNamespace(file=file, yes=True, dry_run=False, dir=str(backup_dir), keep=7)

    def test_sql_restore_snapshots_before_restore(self, tmp_path, monkeypatch, capsys):
        """M7：.sql.gz 恢复 = DROP+CREATE 覆盖现有库，执行前必须自动做 pre-restore 快照。"""
        mod = self._load_cli()
        order = []
        backup_dir = tmp_path / "backups"

        async def fake_backup(db_url, out_dir, **kwargs):
            assert kwargs.get("tag") == "pre-restore", "恢复前快照必须用 pre-restore 标签"
            assert Path(out_dir) == backup_dir, "快照必须落到同一备份目录"
            backup_dir.mkdir(parents=True, exist_ok=True)
            (backup_dir / "pre-restore-2026-09-10.sql.gz").write_bytes(b"x")
            order.append("backup")
            return {"path": str(backup_dir / "pre-restore-2026-09-10.sql.gz")}

        async def fake_restore(db_url, file, dry_run=False):
            order.append("restore")
            return {"strategy": "fake", "restored": True}

        monkeypatch.setenv("DATABASE_URL", "postgresql://x@127.0.0.1:1/x")
        monkeypatch.setattr(mod, "backup_database", fake_backup)
        monkeypatch.setattr(mod, "restore_database", fake_restore)

        mod.cmd_restore(self._args("agent-demo-2026-09-09.sql.gz", backup_dir))
        assert order == ["backup", "restore"], "必须先快照、后恢复"
        assert (backup_dir / "pre-restore-2026-09-10.sql.gz").is_file()
        assert "pre-restore-2026-09-10.sql.gz" in capsys.readouterr().out

    def test_jsonl_restore_needs_no_snapshot(self, tmp_path, monkeypatch):
        """M7：JSONL 恢复是幂等 DO NOTHING 追加，不覆盖数据 → 无需快照。"""
        mod = self._load_cli()
        order = []

        async def fake_backup(*a, **k):
            order.append("backup")
            return {"path": "x"}

        async def fake_restore(db_url, file, dry_run=False):
            order.append("restore")
            return {"strategy": "fake", "restored": True}

        monkeypatch.setenv("DATABASE_URL", "postgresql://x@127.0.0.1:1/x")
        monkeypatch.setattr(mod, "backup_database", fake_backup)
        monkeypatch.setattr(mod, "restore_database", fake_restore)

        mod.cmd_restore(self._args("agent-demo-2026-09-09.jsonl.gz", tmp_path))
        assert order == ["restore"]

    def test_restore_aborts_when_pre_snapshot_fails(self, tmp_path, monkeypatch, capsys):
        """M7：快照失败（如连不上库）必须中止恢复——没有退路的覆盖不能执行。"""
        mod = self._load_cli()
        order = []

        async def failing_backup(*a, **k):
            order.append("backup")
            raise RuntimeError("连不上库")

        async def fake_restore(*a, **k):
            order.append("restore")
            return {}

        monkeypatch.setenv("DATABASE_URL", "postgresql://x@127.0.0.1:1/x")
        monkeypatch.setattr(mod, "backup_database", failing_backup)
        monkeypatch.setattr(mod, "restore_database", fake_restore)

        with pytest.raises(SystemExit) as exc_info:
            mod.cmd_restore(self._args("agent-demo-2026-09-09.sql.gz", tmp_path))
        assert exc_info.value.code == 1
        assert order == ["backup"], "快照失败后绝不能继续恢复"
        assert "❌" in capsys.readouterr().err

    def test_restore_failure_fails_loud(self, tmp_path, monkeypatch, capsys):
        """M4：恢复有失败行时 CLI 打 ❌ 并以非零码退出，绝不打印 ✅。"""
        mod = self._load_cli()

        async def fake_restore(db_url, file, dry_run=False):
            raise RuntimeError("恢复失败：2 行未能恢复（messages×2）")

        monkeypatch.setenv("DATABASE_URL", "postgresql://x@127.0.0.1:1/x")
        monkeypatch.setattr(mod, "restore_database", fake_restore)

        with pytest.raises(SystemExit) as exc_info:
            mod.cmd_restore(self._args("agent-demo-2026-09-09.jsonl.gz", tmp_path))
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "❌" in captured.err and "✅" not in captured.out


class TestScratchDbGuards:
    """L17：scratch_db 的库名守卫与标识符转义。"""

    @staticmethod
    def _load_module():
        import importlib.util

        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location("scratch_db_cli", root / "scripts" / "scratch_db.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_name_guard_is_token_based(self, monkeypatch):
        """整词匹配：latest/contest 这类含 `test` 子串的名字不得再被误放行；
        scratch/test 词素照常放行（含默认名 agent_demo_scratch）。"""
        mod = self._load_module()
        monkeypatch.setenv("DATABASE_URL", "postgresql://x@127.0.0.1:1/agent_demo_prod")
        for name in ("agent_demo_scratch", "scratch-test-db", "test_run_db"):
            mod._check_name(name)  # 不抛即放行
        for name in ("latest", "contest", "production"):
            with pytest.raises(SystemExit):
                mod._check_name(name)

    def test_name_equal_to_prod_db_rejected(self, monkeypatch):
        mod = self._load_module()
        monkeypatch.setenv("DATABASE_URL", "postgresql://x@127.0.0.1:1/agent_demo_scratch")
        with pytest.raises(SystemExit):
            mod._check_name("agent_demo_scratch")

    def test_identifier_quotes_are_escaped(self):
        """L17：DROP 的库名标识符内 `"` 翻倍转义，内嵌引号/分号不再是 SQL。"""
        mod = self._load_module()
        assert mod._quote_ident('a"b; DROP DATABASE x') == '"a""b; DROP DATABASE x"'
        assert mod._quote_ident("plain") == '"plain"'
