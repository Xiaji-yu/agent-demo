"""记录保全：聊天记录归档（7 天滚动）+ 数据库备份/恢复。

这一层是「防止再次删库导致数据不可恢复」，所以测试重点不是覆盖率，而是
**真的能不能把数据找回来**：导出 → 清空 → 回灌 → 逐表核对。
"""
import asyncio
import gzip
import json
import os
import time
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
        assert set(row) == {"id", "session_id", "role", "content"}, "蒸馏输入不得带身份字段"

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
    async def test_identity_lookup_failure_is_tolerated(self, tmp_path):
        inner = InMemoryMemoryStore()
        sid = await inner.resolve_session("u1", None)
        # 未知 session：身份为空也要能归档
        archive = MessageArchive(tmp_path / "archive")
        store = ArchivingStore(inner, archive)
        await store.append_message("999", "user", "x")
        assert next(archive.iter_records())["user_id"] == ""

        # 正常 session 能查到身份
        await store.append_message(sid, "user", "y")
        recs = list(archive.iter_records())
        assert recs[-1]["user_id"] == "u1"


# ---------- 蒸馏把归档并进来 ----------


class TestDistillUsesArchive:
    @pytest.mark.asyncio
    async def test_archive_fills_in_when_db_is_empty(self, tmp_path):
        """库被清空 / 落后时，蒸馏仍能从归档拿到内容（关键防事故属性）。"""
        from agentcore.rag.distill import distill_from_memory
        from tests.test_rag import FakeEmbedding, FakeLLM, _tc

        inner = InMemoryMemoryStore()
        archive = MessageArchive(tmp_path / "archive")
        store = ArchivingStore(inner, archive)

        sid = await store.resolve_session("u1", None)
        await store.append_message(sid, "user", "沙箱白名单怎么防绕过？" * 20)
        await store.append_message(sid, "assistant", "必须逐参数校验并禁用 find -exec。" * 20)

        # 模拟数据库内容丢失（归档还在）
        inner.messages.clear()

        llm = FakeLLM(_tc("沙箱安全", ["白名单必须逐参数校验并禁用 find -exec"]))
        result = await distill_from_memory(llm, store, FakeEmbedding(), min_chars=10)
        assert result["status"] == "ok" and result["chunks"] == 1
        assert "archive" in result["source"]

    @pytest.mark.asyncio
    async def test_db_and_archive_are_deduped(self, tmp_path):

        from agentcore.rag.distill import _collect_messages

        inner = InMemoryMemoryStore()
        store = ArchivingStore(inner, MessageArchive(tmp_path / "archive"))
        sid = await store.resolve_session("u1", None)
        await store.append_message(sid, "user", "内容。" * 100)

        rows, note = await _collect_messages(store, 0, 200)
        assert len(rows) == 1, "库里与归档里的同一条消息不能重复"
        assert note.startswith("db+archive")


# ---------- 数据库备份 / 恢复（需要 PG） ----------

PG = os.getenv("TEST_DATABASE_URL")
pg_only = pytest.mark.skipif(not PG, reason="TEST_DATABASE_URL not set")


@pg_only
class TestBackupRestore:
    @pytest.mark.asyncio
    async def test_jsonl_export_then_restore_roundtrip(self, tmp_path):
        """核心保证：导出 → 清空 → 回灌 → 数据一条不少。"""
        from agentcore.backup import backup_database, restore_database
        from agentcore.memory.store import PgMemoryStore

        store = PgMemoryStore(PG, dim=8)
        await store.init()
        try:
            async with store.pool.acquire() as c:
                await c.execute("TRUNCATE messages, sessions, facts, kb_sources, kb_chunks CASCADE")
            sid = await store.resolve_session("u-backup", "g-backup")
            for i in range(5):
                await store.append_message(sid, "user", f"备份消息 {i}")
            await store.save_fact("u-backup", "事实一", [0.5] * 8, session_id=sid)
            await store.kb_add_source("来源一", "manual")
            before = await store.kb_stats()

            result = await backup_database(PG, tmp_path, keep=7, strategy="jsonl")
            assert result["bytes"] > 0
            counts = json.loads(json.dumps(result))
            assert counts["rows"] >= 6

            # 模拟事故：清空
            async with store.pool.acquire() as c:
                await c.execute("TRUNCATE messages, sessions, facts, kb_sources, kb_chunks CASCADE")
            assert await store.latest_message_id() == 0
            assert (await store.kb_stats())["sources"] == 0

            # 回灌
            out = await restore_database(PG, result["path"])
            assert out["restored"]["messages"] == 5
            assert out["restored"]["sessions"] >= 1

            # 核对
            async with store.pool.acquire() as c:
                msgs = await c.fetchval("SELECT count(*) FROM messages")
                facts = await c.fetchval("SELECT count(*) FROM facts")
            assert msgs == 5 and facts == 1
            assert (await store.kb_stats())["sources"] == before["sources"]
            assert await store.list_facts("u-backup") == ["事实一"]
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
            p.write_bytes(b"x")
            os.utime(p, (1000 + i, 1000 + i))
        removed = prune_backups(tmp_path, keep=2)
        assert len(removed) == 3
        assert len(list_backups(tmp_path)) == 2

    @pytest.mark.asyncio
    async def test_unreachable_db_backup_raises(self, tmp_path):
        """连不上库时备份必须报错（不能静默产出空备份，那比没有备份更危险）。"""
        import asyncpg

        from agentcore.backup import backup_database

        with pytest.raises((OSError, asyncpg.PostgresError, ConnectionError, TimeoutError)):
            await backup_database(
                "postgresql://nobody@127.0.0.1:1/none", tmp_path, strategy="jsonl"
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
        src.write_bytes(b"dump")

        ok, dst = _mirror_backup(src, mirror, keep=2, tag="agent-demo")
        assert ok and Path(dst).is_file()
        assert Path(dst).read_bytes() == b"dump"

        # 老备份在镜像侧也要轮转掉
        for day in ("02", "03"):
            p = mirror / f"agent-demo-2026-01-{day}.sql.gz"
            p.write_bytes(b"x")
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
