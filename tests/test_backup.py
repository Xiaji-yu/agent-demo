"""备份 / 恢复链路回归：JSONL 游标流式（REVIEW-a604023..679c9b3）与
镜像 sidecar、docker 探测超时、归档 limit（test_review_m_fixes 并入）。

关键回归点：旧实现 `await conn.fetch("SELECT * FROM t")` 把整表读进内存；
现在必须 `conn.cursor(...)` 分批 + 每批 to_thread 序列化，失败清理 `.part`。
"""

import gzip
import hashlib
import json
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from agentcore.backup import db_backup


class _AsyncCM:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeConn:
    """只实现 `_iter_table_jsonl` 用到的协议：transaction() / cursor() / close()。"""

    def __init__(self, tables: dict[str, list[dict]], fail_on: str | None = None):
        self.tables = tables
        self.fail_on = fail_on
        self.sql_calls: list[str] = []
        self.fetch_calls: list[str] = []
        self.closed = False

    def transaction(self):
        return _AsyncCM()

    async def cursor(self, sql):
        self.sql_calls.append(sql)
        table = sql.rsplit(" ", 1)[-1]
        if table == self.fail_on:
            raise RuntimeError(f"boom on {table}")
        for row in self.tables.get(table, []):
            yield row

    async def fetch(self, sql, *args):  # 旧实现走的路径：调用即失败
        self.fetch_calls.append(sql)
        raise AssertionError("不得整表 fetch：必须用游标流式读取")

    async def close(self):
        self.closed = True


def _patch_connect(monkeypatch, conn: _FakeConn) -> None:
    import asyncpg

    async def fake_connect(_url, *a, **kw):
        return conn

    monkeypatch.setattr(asyncpg, "connect", fake_connect)


@pytest.mark.asyncio
async def test_iter_table_jsonl_streams_in_batches(monkeypatch):
    rows = [{"id": i, "v": f"v{i}"} for i in range(7)]
    conn = _FakeConn({"facts": rows})

    calls: list[tuple[int, str]] = []
    real = db_backup._serialize_rows

    def spy(table, batch):
        calls.append((len(batch), threading.current_thread().name))
        return real(table, batch)

    monkeypatch.setattr(db_backup, "_serialize_rows", spy)

    batches = [item async for item in db_backup._iter_table_jsonl(conn, "facts", 3)]

    assert [n for _, n in batches] == [3, 3, 1], "必须按 batch_rows 分批，尾批为余数"
    assert conn.fetch_calls == [], "不得回退到整表 fetch"
    assert conn.sql_calls == ["SELECT * FROM facts"]
    assert [size for size, _ in calls] == [3, 3, 1]
    main = threading.current_thread().name
    assert all(name != main for _, name in calls), (
        f"序列化必须在线程池里跑，实际线程：{calls}"
    )

    lines = [json.loads(ln) for ln in "".join(c for c, _ in batches).splitlines()]
    assert [ln["row"]["id"] for ln in lines] == list(range(7)), "顺序与内容不得改变"
    assert {ln["table"] for ln in lines} == {"facts"}


@pytest.mark.asyncio
async def test_iter_table_jsonl_empty_table_yields_nothing():
    conn = _FakeConn({"facts": []})
    assert [item async for item in db_backup._iter_table_jsonl(conn, "facts", 3)] == []


@pytest.mark.asyncio
async def test_iter_table_jsonl_exact_multiple_has_no_empty_tail():
    """整批结束时不得多产出一个空批次（否则 rows 统计与 JSONL 会出现空块）。"""
    conn = _FakeConn({"facts": [{"id": i} for i in range(4)]})
    batches = [item async for item in db_backup._iter_table_jsonl(conn, "facts", 2)]
    assert [n for _, n in batches] == [2, 2]
    assert all(chunk for chunk, _ in batches)


@pytest.mark.asyncio
async def test_backup_jsonl_writes_meta_and_streams_all_tables(monkeypatch, tmp_path):
    conn = _FakeConn({"t1": [{"id": 1}], "t2": [{"id": 2}, {"id": 3}]})
    _patch_connect(monkeypatch, conn)
    monkeypatch.setattr(db_backup, "_PGDATA_TABLES", ("t1", "t2"))

    info = await db_backup._backup_jsonl("postgresql://x/y", tmp_path, "tag")

    assert info["rows"] == 3
    assert info["strategy"] == "asyncpg jsonl"
    assert conn.closed, "无论成功失败都必须关闭连接"
    path = Path(info["path"])
    assert path.is_file()
    assert not Path(f"{path}.part").exists()
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        lines = [json.loads(ln) for ln in fh.read().splitlines()]
    assert lines[0]["__meta__"]["tables"] == ["t1", "t2"]
    assert [(ln["table"], ln["row"]["id"]) for ln in lines[1:]] == [
        ("t1", 1),
        ("t2", 2),
        ("t2", 3),
    ]
    # 校验和 sidecar 与文件内容一致（异地核对依赖它）
    sidecar = Path(f"{path}.sha256")
    assert (
        sidecar.read_text(encoding="utf-8").strip()
        == hashlib.sha256(path.read_bytes()).hexdigest()
    )
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, "备份含聊天记录，必须 0600"


@pytest.mark.asyncio
async def test_backup_jsonl_failure_leaves_no_partial_file(monkeypatch, tmp_path):
    """第二张表读挂：`.part` 与最终文件都必须不存在，只留下完整备份或什么都没有。"""
    conn = _FakeConn({"t1": [{"id": 1}], "t2": [{"id": 2}]}, fail_on="t2")
    _patch_connect(monkeypatch, conn)
    monkeypatch.setattr(db_backup, "_PGDATA_TABLES", ("t1", "t2"))

    with pytest.raises(RuntimeError, match="boom on t2"):
        await db_backup._backup_jsonl("postgresql://x/y", tmp_path, "tag")

    assert list(tmp_path.glob("*")) == [], f"残留文件：{list(tmp_path.glob('*'))}"
    assert conn.closed


@pytest.mark.asyncio
async def test_backup_jsonl_failure_during_iteration_unlinks_part(
    monkeypatch, tmp_path
):
    """游标迭代中途抛错（而非首行前）同样必须清理 `.part`。"""
    conn = _FakeConn({"t1": [{"id": 1}]})

    def exploding(table, batch):
        raise RuntimeError("serialize exploded")

    monkeypatch.setattr(db_backup, "_serialize_rows", exploding)
    _patch_connect(monkeypatch, conn)
    monkeypatch.setattr(db_backup, "_PGDATA_TABLES", ("t1",))

    with pytest.raises(RuntimeError, match="serialize exploded"):
        await db_backup._backup_jsonl("postgresql://x/y", tmp_path, "tag")
    assert list(tmp_path.glob("*")) == []


# --------------------------------------------------------------------------- #
# 真库用例：证明「大表也不是一次性读进内存」
# --------------------------------------------------------------------------- #

pytest_pg = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="TEST_DATABASE_URL not set; PG integration tests skipped",
)


@pytest_pg
@pytest.mark.asyncio
async def test_backup_jsonl_pg_streams_large_table(monkeypatch, tmp_path):
    import asyncpg

    url = os.environ["TEST_DATABASE_URL"]
    conn = await asyncpg.connect(url)
    try:
        await conn.execute("DROP TABLE IF EXISTS backup_probe")
        await conn.execute("CREATE TABLE backup_probe (id int primary key, v text)")
        await conn.execute(
            "INSERT INTO backup_probe (id, v) "
            "SELECT g, 'v' || g FROM generate_series(0, 1199) AS g"
        )
    finally:
        await conn.close()

    seen_batches: list[int] = []
    real = db_backup._serialize_rows

    def spy(table, batch):
        seen_batches.append(len(batch))
        return real(table, batch)

    monkeypatch.setattr(db_backup, "_serialize_rows", spy)
    monkeypatch.setattr(db_backup, "_PGDATA_TABLES", ("backup_probe",))

    info = await db_backup._backup_jsonl(url, tmp_path, "probe")

    assert info["rows"] == 1200
    batch_rows = db_backup._JSONL_BATCH_ROWS
    assert len(seen_batches) >= 2, f"1200 行必须分批序列化，实际只调用了 {seen_batches}"
    assert sum(seen_batches) == 1200
    assert max(seen_batches) <= batch_rows, (
        f"单批 {max(seen_batches)} 行超过 batch_rows={batch_rows}：又整表进内存了"
    )

    path = Path(info["path"])
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        lines = [json.loads(ln) for ln in fh.read().splitlines()]
    assert lines[0]["__meta__"]["tables"] == ["backup_probe"]
    assert [ln["row"]["id"] for ln in lines[1:]] == list(range(1200))


# ==========================================================================
# REVIEW-a604023..679c9b3 M（镜像 sidecar / docker 探测超时 / 归档 limit）
# ==========================================================================


# 来源: test_review_m_fixes TestMirrorSidecar
class TestMirrorSidecar:
    def test_sidecar_is_copied(self, tmp_path):
        from agentcore.backup.db_backup import _mirror_backup

        src_dir = tmp_path / "src"
        src_dir.mkdir()
        src = src_dir / "messages-2026-01-01.jsonl.gz"
        src.write_bytes(b"fake")
        src.with_name(src.name + ".sha256").write_text("deadbeef", encoding="utf-8")

        mirror = tmp_path / "mirror"
        ok, dst = _mirror_backup(src, mirror, keep=5)
        assert ok and dst
        assert (mirror / (src.name + ".sha256")).is_file(), "镜像必须带 sidecar 校验和"

    def test_docker_probe_timeout_degrades(self, monkeypatch):
        import subprocess

        import agentcore.backup.db_backup as b

        # pg_dump 不存在、docker 存在 → 走到 docker 探测（该探测抛超时）
        monkeypatch.setattr(
            b.shutil,
            "which",
            lambda name: "/usr/bin/docker" if name == "docker" else None,
        )
        monkeypatch.setattr(
            b.subprocess,
            "run",
            lambda *a, **k: (_ for _ in ()).throw(
                subprocess.TimeoutExpired("docker exec", 20)
            ),
        )
        assert b.find_pg_dump() is None  # 不再让异常逃出 → auto 可回退 JSONL


# ==========================================================================
# REVIEW-ee39c8e..e55cab5 M2/L1/L4：schema sidecar 命名与清理
# ==========================================================================


class TestSchemaSidecarNaming:
    """加密备份的 sidecar 必须按**解密后**的名字命名，否则版本守卫静默失效。"""

    def test_encrypted_sidecar_uses_decrypted_name(self, tmp_path):
        assert (
            db_backup._schema_sidecar_path(
                tmp_path / "agent-demo-2026-10-24.sql.gz.age"
            ).name
            == "agent-demo-2026-10-24.sql.gz.schema.json"
        )
        assert (
            db_backup._schema_sidecar_path(
                tmp_path / "agent-demo-2026-10-24.sql.gz"
            ).name
            == "agent-demo-2026-10-24.sql.gz.schema.json"
        )

    def test_sidecar_written_for_encrypted_read_by_decrypted_path(self, tmp_path):
        encrypted = tmp_path / "a.sql.gz.age"
        encrypted.write_bytes(b"age")
        db_backup._write_schema_sidecar(encrypted, "0003")
        assert (tmp_path / "a.sql.gz.schema.json").is_file()
        assert not (tmp_path / "a.sql.gz.age.schema.json").exists()
        decrypted = tmp_path / "a.sql.gz"
        assert db_backup._read_schema_sidecar(decrypted) == "0003"

    def test_legacy_encrypted_sidecar_still_readable(self, tmp_path):
        """存量备份的旧命名 <file>.age.schema.json 不能被静默丢弃。"""
        decrypted = tmp_path / "a.sql.gz"
        (tmp_path / "a.sql.gz.age.schema.json").write_text(
            json.dumps({"schema_version": "0002"}), encoding="utf-8"
        )
        assert db_backup._read_schema_sidecar(decrypted) == "0002"

    def test_missing_sidecar_is_legacy_none(self, tmp_path):
        assert db_backup._read_schema_sidecar(tmp_path / "a.sql.gz") is None

    def test_corrupt_sidecar_fails_loud(self, tmp_path, monkeypatch):
        """L4：sidecar 存在但损坏 → restore 必须拒绝，而不是跳过版本守卫。"""
        p = tmp_path / "a.sql.gz"
        with gzip.open(p, "wb") as fh:
            fh.write(b"SELECT 1;\n")
        (tmp_path / "a.sql.gz.schema.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(RuntimeError, match="无法解析"):
            db_backup._read_schema_sidecar(p)

    def test_malformed_version_fails_loud(self, tmp_path):
        p = tmp_path / "a.sql.gz"
        (tmp_path / "a.sql.gz.schema.json").write_text(
            json.dumps({"schema_version": "v3"}), encoding="utf-8"
        )
        with pytest.raises(RuntimeError, match="schema_version"):
            db_backup._read_schema_sidecar(p)

    @pytest.mark.asyncio
    async def test_restore_guard_fires_on_decrypted_encrypted_backup(self, tmp_path):
        """加密备份解密后，版本守卫必须真的生效（M2 的端到端后果）。"""
        p = tmp_path / "a.sql.gz"
        with gzip.open(p, "wb") as fh:
            fh.write(b"SELECT 1;\n")
        db_backup._write_schema_sidecar(p, "99999999")
        with pytest.raises(RuntimeError, match="高于当前代码"):
            await db_backup.restore_database("postgresql://u:p@127.0.0.1:5432/db", p)

    def test_remove_backup_deletes_schema_sidecars(self, tmp_path):
        """L1：prune 不得留下 .schema.json 孤儿。"""
        p = tmp_path / "a.sql.gz"
        p.write_bytes(b"x")
        (tmp_path / "a.sql.gz.sha256").write_text("deadbeef", encoding="utf-8")
        (tmp_path / "a.sql.gz.schema.json").write_text("{}", encoding="utf-8")
        (tmp_path / "a.sql.gz.age.schema.json").write_text("{}", encoding="utf-8")

        assert db_backup._remove_backup(p) is True
        assert list(tmp_path.iterdir()) == []

    def test_mirror_copies_canonical_schema_sidecar(self, tmp_path):
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        src = src_dir / "a.sql.gz.age"
        src.write_bytes(b"age")
        db_backup._write_schema_sidecar(src, "0003")

        mirror = tmp_path / "mirror"
        ok, _ = db_backup._mirror_backup(src, mirror, keep=5)
        assert ok
        assert (mirror / "a.sql.gz.schema.json").is_file()


class TestRestoreClearsMigrationLedger:
    """M3：备份不含 schema_migrations；恢复到非空库前必须清空旧账本。"""

    @pytest.mark.asyncio
    async def test_jsonl_restore_clears_ledger_first(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        class _Tx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

        class _Conn:
            def __init__(self):
                self.executed: list[str] = []

            def transaction(self):
                return _Tx()

            async def execute(self, sql, *_args):
                self.executed.append(sql)
                return "INSERT 0 1"

            async def close(self):
                pass

        conn = _Conn()

        async def fake_connect(_url):
            return conn

        monkeypatch.setitem(
            sys.modules, "asyncpg", SimpleNamespace(connect=fake_connect)
        )
        path = tmp_path / "a.jsonl.gz"
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            fh.write(json.dumps({"__meta__": {"tables": []}}) + "\n")

        await db_backup._restore_jsonl(
            "postgresql://u:p@127.0.0.1:5432/db", path, False
        )

        assert conn.executed, "恢复必须执行 SQL"
        assert "DELETE FROM schema_migrations" in conn.executed[0]
        assert conn.executed[0].startswith("DO $$")


class TestContainerPasswordNotInArgv:
    """M7（REVIEW-1e0f5ae..a0a6bb4）：容器备份路径密码不进 argv。

    旧实现 `docker exec -e PGPASSWORD=…` 把密码放进 docker CLI 的 argv——
    宿主机 /proc/<pid>/cmdline 对同机其他本地用户可读。备份路径（stdin 空闲）
    改为 `sh -s` + stdin 注入脚本；restore（psql）路径 stdin 被恢复 SQL 流
    占用，stdin 注入不可行，**有意豁免**保留 -e 形态（见 _with_container_env
    docstring，勿「顺手统一」）。
    """

    def test_backup_wrap_keeps_password_out_of_argv(self):
        prefix = ["docker", "exec", "-i", "db-1", "pg_dump"]
        args = ["-h", "127.0.0.1", "--if-exists", "agent_demo"]
        cmd, script = db_backup._container_script_wrap(prefix, "s3cret", args)
        joined = " ".join(cmd)
        assert "s3cret" not in joined, "密码不得出现在 argv"
        assert "agent_demo" not in joined, "参数整段进脚本（dash 的 $@ 语义不可靠）"
        assert cmd[:5] == ["docker", "exec", "-i", "db-1", "sh", "-s"][:5]
        assert "exec pg_dump '-h' '127.0.0.1' '--if-exists' 'agent_demo'" in script
        assert "'s3cret'" in script

    def test_backup_wrap_quotes_single_quotes_in_password(self):
        _prefix = ["docker", "exec", "-i", "db-1", "pg_dump"]
        cmd, script = db_backup._container_script_wrap(_prefix, "pa'ss", [])
        assert "pa'ss" not in " ".join(cmd)
        assert "'pa'\"'\"'ss'" in script  # POSIX 单引号转义

    def test_backup_wrap_passthrough_without_docker_or_password(self):
        prefix = ["/usr/bin/pg_dump"]
        assert db_backup._container_script_wrap(prefix, "", ["-h", "x"]) == (
            ["/usr/bin/pg_dump", "-h", "x"],
            "",
        )
        assert db_backup._container_script_wrap(prefix, "pw", ["-h", "x"]) == (
            ["/usr/bin/pg_dump", "-h", "x"],
            "",
        )
        d = ["docker", "exec", "-i", "db-1", "pg_dump"]
        assert db_backup._container_script_wrap(d, "", ["-h", "x"]) == (
            [*d, "-h", "x"],
            "",
        )

    def test_restore_path_intentionally_keeps_env_flag(self):
        """restore 路径豁免在位：仍走 -e 形态（stdin 被恢复 SQL 占用）。"""
        prefix = ["docker", "exec", "-i", "db-1", "psql"]
        cmd = db_backup._with_container_env(prefix, "s3cret")
        assert "-e" in cmd and "PGPASSWORD=s3cret" in cmd


# ------------------------------------------------ 归档恢复不限读行


# 来源: test_review_m_fixes TestArchiveRestoreLimit
class TestArchiveRestoreLimit:
    def test_iter_records_limit_none_is_unlimited(self, tmp_path):
        import json

        from agentcore.memory.archive import MessageArchive

        arch = MessageArchive(str(tmp_path))
        day_file = tmp_path / "messages-2026-01-01.jsonl"
        with day_file.open("w", encoding="utf-8") as fh:
            for i in range(50):
                fh.write(
                    json.dumps(
                        {"id": i + 1, "session_id": 1, "role": "user", "content": "x"}
                    )
                    + "\n"
                )

        assert len(list(arch.iter_records(0, limit=10))) == 10
        assert len(list(arch.iter_records(0, limit=None))) == 50


class TestPartFileHardening:
    """.part 窗口 0600 + 中断残留清理（审查 P2 安全/可维护性）。"""

    def test_part_mode_0600_on_touch(self, tmp_path):
        import os

        from agentcore.backup.db_backup import _backup_path

        path = _backup_path(tmp_path, "agent-demo", ".jsonl.gz")
        part = Path(f"{path}.part")
        part.touch(mode=0o600)
        assert os.stat(part).st_mode & 0o777 == 0o600

    def test_prune_removes_stale_part(self, tmp_path, monkeypatch):
        import os
        import time as _t

        from agentcore.backup import db_backup as b

        stale = tmp_path / "agent-demo-20990101-000000.jsonl.gz.part"
        stale.write_bytes(b"partial")
        old = _t.time() - 7 * 3600
        os.utime(stale, (old, old))
        fresh_marker = tmp_path / "agent-demo-20990102-000000.jsonl.gz.part"
        fresh_marker.write_bytes(b"just-started")

        monkeypatch.setattr(b, "list_backups", lambda *a, **k: [])
        removed = b.prune_backups(tmp_path, keep=7)
        assert stale.name in removed
        assert not stale.exists()
        assert fresh_marker.exists(), "新鲜 .part 不得误删"


class TestAgeEncryption:
    """AGENT_BACKUP_ENCRYPT=age：recipient-only 加密，明文不落盘且不回退 JSONL。"""

    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("AGENT_BACKUP_ENCRYPT", raising=False)
        assert db_backup._backup_encryption() is None

    def test_rejects_unknown_mode(self, monkeypatch):
        monkeypatch.setenv("AGENT_BACKUP_ENCRYPT", "pgp")
        with pytest.raises(RuntimeError, match="不支持"):
            db_backup._backup_encryption()

    def test_requires_recipient(self, monkeypatch):
        monkeypatch.setenv("AGENT_BACKUP_ENCRYPT", "age")
        monkeypatch.delenv("AGENT_BACKUP_AGE_RECIPIENT", raising=False)
        with pytest.raises(RuntimeError, match="RECIPIENT"):
            db_backup._backup_encryption()

    def test_requires_age_binary(self, monkeypatch):
        monkeypatch.setenv("AGENT_BACKUP_ENCRYPT", "age")
        monkeypatch.setenv("AGENT_BACKUP_AGE_RECIPIENT", "age1test")
        monkeypatch.setattr(db_backup.shutil, "which", lambda _name: None)
        with pytest.raises(RuntimeError, match="age 命令"):
            db_backup._backup_encryption()

    def test_verify_rejects_non_age_file(self, tmp_path):
        p = tmp_path / "x.sql.gz.age"
        p.write_bytes(b"this is not age")
        result = db_backup.verify_backup(p)
        assert result["ok"] is False
        assert result["kind"] == "pg_dump_encrypted"
        assert "age" in result["error"]

    def test_pump_writes_compressed_stream_to_age_stdin(self, tmp_path):
        """加密路径：gzip 直接进 age stdin，磁盘上不产生 .part 明文。"""
        import io
        from types import SimpleNamespace

        raw = b"CREATE TABLE t (id int);\n" * 50
        sink = io.BytesIO()
        db_backup._pump_pg_dump_stdout(
            SimpleNamespace(stdout=io.BytesIO(raw)), tmp_path / "x.part", sink
        )
        assert not (tmp_path / "x.part").exists()
        assert gzip.decompress(sink.getvalue()) == raw

    @pytest.mark.asyncio
    async def test_encrypt_refuses_jsonl(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENT_BACKUP_ENCRYPT", "age")
        monkeypatch.setenv("AGENT_BACKUP_AGE_RECIPIENT", "age1test")
        monkeypatch.setattr(db_backup.shutil, "which", lambda _name: "/bin/true")
        with pytest.raises(RuntimeError, match="仅支持 pg_dump"):
            await db_backup.backup_database(
                "postgresql://u:p@127.0.0.1:5432/db", tmp_path, strategy="jsonl"
            )

    @pytest.mark.asyncio
    async def test_encrypt_never_falls_back_to_plaintext(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENT_BACKUP_ENCRYPT", "age")
        monkeypatch.setenv("AGENT_BACKUP_AGE_RECIPIENT", "age1test")
        monkeypatch.setattr(db_backup.shutil, "which", lambda _name: "/bin/true")
        monkeypatch.setattr(db_backup, "find_pg_dump", lambda: None)
        with pytest.raises(RuntimeError, match="需要 pg_dump"):
            await db_backup.backup_database(
                "postgresql://u:p@127.0.0.1:5432/db", tmp_path, strategy="auto"
            )
        assert not list(tmp_path.glob("*.gz"))
        assert not list(tmp_path.glob("*.age"))

    @pytest.mark.asyncio
    async def test_pg_dump_failure_does_not_fall_back(self, tmp_path, monkeypatch):
        """pg_dump 失败时 auto 会回退 JSONL；承诺加密后必须直接失败。"""
        monkeypatch.setenv("AGENT_BACKUP_ENCRYPT", "age")
        monkeypatch.setenv("AGENT_BACKUP_AGE_RECIPIENT", "age1test")
        monkeypatch.setattr(db_backup.shutil, "which", lambda _name: "/bin/true")

        async def fake_pg_dump(*_a, **_kw):
            raise RuntimeError("pg_dump 退出码 1：boom")

        async def forbidden_jsonl(*_a, **_kw):
            raise AssertionError("加密模式不得回退 JSONL（会落明文全库）")

        monkeypatch.setattr(db_backup, "_backup_pg_dump", fake_pg_dump)
        monkeypatch.setattr(db_backup, "_backup_jsonl", forbidden_jsonl)
        with pytest.raises(RuntimeError, match="pg_dump 退出码 1"):
            await db_backup.backup_database(
                "postgresql://u:p@127.0.0.1:5432/db", tmp_path, strategy="auto"
            )

    @pytest.mark.asyncio
    async def test_restore_refuses_encrypted_with_instructions(self, tmp_path):
        p = tmp_path / "agent-demo-2026-10-24.sql.gz.age"
        p.write_bytes(b"age-encryption.org/v1\n")
        with pytest.raises(RuntimeError, match="解密"):
            await db_backup.restore_database("postgresql://u:p@127.0.0.1:5432/db", p)

    @pytest.mark.skipif(os.name != "posix", reason="需要可执行脚本伪造 age")
    @pytest.mark.asyncio
    async def test_encrypted_backup_streams_and_keeps_plaintext_out(
        self, tmp_path, monkeypatch
    ):
        fake_age = tmp_path / "age"
        fake_age.write_text("#!/bin/sh\nprintf 'age-encryption.org/v1\\n'\ncat\n")
        fake_age.chmod(0o755)
        dump_py = (
            "import sys;sys.stdout.buffer.write(b'CREATE TABLE t (id int);\\n' * 100)"
        )
        monkeypatch.setattr(
            db_backup,
            "find_pg_dump",
            lambda: ([sys.executable, "-c", dump_py], "fake pg_dump"),
        )
        monkeypatch.setenv("AGENT_BACKUP_ENCRYPT", "age")
        monkeypatch.setenv("AGENT_BACKUP_AGE_RECIPIENT", "age1test")
        monkeypatch.setattr(
            db_backup.shutil,
            "which",
            lambda name: str(fake_age) if name == "age" else None,
        )

        async def _no_version(_url):
            return None

        monkeypatch.setattr(db_backup, "_current_schema_version", _no_version)
        result = await db_backup.backup_database(
            "postgresql://u:p@127.0.0.1:5432/db",
            tmp_path,
            keep=3,
            strategy="pg_dump",
        )

        p = Path(result["path"])
        assert p.name.endswith(".sql.gz.age")
        assert result["strategy"].endswith("+ age")
        data = p.read_bytes()
        assert data.startswith(db_backup._AGE_MAGIC)
        assert b"CREATE TABLE t" not in data, "明文 SQL 不得出现在备份文件里"
        inner = gzip.decompress(data[len(db_backup._AGE_MAGIC) + 1 :])
        assert b"CREATE TABLE t" in inner, "gzip 内容应完整（伪造 age 仅加头）"

        items = db_backup.list_backups(tmp_path)
        assert items and items[0]["kind"] == "pg_dump_encrypted"
        check = db_backup.verify_backup(p)
        assert check["ok"] is True
        assert check["kind"] == "pg_dump_encrypted"
        assert check["checksum"] == "verified"


class TestRestoreSqlNoDeadlock:
    """L19（REVIEW-de09478..workdir）：_restore_sql 三PIPE 无并发读的双管道死锁。

    psql 处理 --inserts 格式 dump 时每行往 stdout 打 "INSERT 0 1"，塞满 64KB
    后阻塞写 → 不再消费 stdin → 主线程 stdin.write 阻塞，直到 600s 超时
    （pre-restore 快照兜底，但整次恢复白等 10 分钟）。

    回归策略：用 `cat` 复刻该场景（stdin 原样回写 stdout，天然是「边读边写
    超过管道容量」）；喂 1MB 数据。旧实现（先写完全部 stdin 再读 stdout）
    必然在 64KB 处死锁 → fuzz 级铁证；新实现 feeder 线程 + 主线程并发读，
    200KB 数据必须秒级往返。
    """

    @pytest.mark.asyncio
    async def test_streaming_restore_does_not_deadlock_on_large_dump(
        self, tmp_path, monkeypatch
    ):
        import asyncio
        import gzip

        import agentcore.backup.db_backup as B

        payload = (b"INSERT INTO t VALUES (1);\n" * 60) * 300  # ~1MB
        dump = tmp_path / "big.sql.gz"
        with gzip.open(dump, "wb") as f:
            f.write(payload)
        # 备份校验（sha256 sidecar）
        import hashlib

        (tmp_path / "big.sql.gz.sha256").write_text(
            hashlib.sha256(dump.read_bytes()).hexdigest(), encoding="utf-8"
        )

        # psql 探测：PATH 里放一个 exec cat 的影子 psql（真实回写场景）；
        # docker 分支关闭。Popen 按 PATH 解析 "psql"，影子命令必须可执行。
        bindir = tmp_path / "bin"
        bindir.mkdir()
        shadow = bindir / "psql"
        shadow.write_text("#!/bin/sh\nexec cat\n", encoding="utf-8")
        shadow.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.delenv("PG_CONTAINER", raising=False)

        async def run():
            return await B.restore_database(
                "postgresql://u:p@127.0.0.1:5432/db", dump, dry_run=False
            )

        # 新实现下 cat 立刻回写完；死锁时被 wait(timeout=600) 拖住——用
        # wait_for(30) 作为用例级防线：30s 内不结束即判死锁（CI 上旧实现
        # 会挂到 600s 超时，从而稳定失败）
        try:
            await asyncio.wait_for(run(), timeout=30)
        except TimeoutError:
            pytest.fail("_restore_sql 在 1MB 回写场景下死锁（双管道）")

    def test_feeder_closes_stdin_on_broken_pipe(self, tmp_path):
        """守卫：psql 早退（SQL 报错）时 feeder 不得把 BrokenPipe 漏出线程。"""
        import gzip

        import agentcore.backup.db_backup as B

        proc = subprocess.Popen(
            ["cat"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.stdin.close()  # 提前关闭 → 任何 write 都触发 BrokenPipe/EPIPE
        src = gzip.open(tmp_path / "x.sql.gz", "wb")
        src.write(b"SELECT 1;\n" * 100000)
        src.close()
        src = gzip.open(tmp_path / "x.sql.gz", "rb")
        # 不得抛异常（BrokenPipeError/ValueError 都必须在函数内消化）
        B._feed_restore_stdin(src, proc)
        proc.wait(timeout=10)


# ==========================================================================
# REVIEW-1b16fa7..9c2d3c1：scripts/backup_mirror.sh + backup_usb.sh 回归
#
# 脚本是 root/xiaji 常驻的定时任务，fail-open 形态（静默 exit 0 / 假成功）
# 直接等价于备份丢失。这里用 PATH mock（ssh/rsync/getent/mountpoint）在
# tmp 里跑真实脚本，锁校验/闸门/拒绝分支。
# ==========================================================================


def _mock_bin(
    tmp_path,
    monkeypatch,
    *,
    getent_ip="10.10.0.1",
    mount_ok=True,
    tools=("ssh", "rsync", "getent", "mountpoint"),
):
    """在 tmp_path/bin 造 ssh/rsync/getent/mountpoint 替身并前置 PATH。

    - ssh：跳过 `--` 与 host，把「远端命令」当本地命令在 MOCK_REMOTE_DIR 里
      eval（真实测到嵌在脚本里的远端 shell 逻辑），并记录 host 参数；
    - rsync/getent/mountpoint：按参数记录/返回固定值；
    - tools：只造指定替身——USB 脚本要真 rsync 做本地复制，只 mock mountpoint。
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    scripts = {
        "ssh": (
            "#!/usr/bin/env bash\n"
            '[ "$1" = "--" ] && shift\n'
            'host="$1"; shift\n'
            'printf "ssh-host=%s\\n" "$host" >> "$MOCK_LOG"\n'
            "[ $# -gt 0 ] || exit 0\n"
            'cmd="$*"\n'
            'cmd="${cmd//$MIRROR_ROOT/$MOCK_REMOTE_DIR}"\n'
            'cd "${MOCK_REMOTE_DIR:?}" && eval "$cmd"\n'
        ),
        "rsync": (
            '#!/usr/bin/env bash\nprintf \'rsync %s\\n\' "$*" >> "$MOCK_LOG"\nexit 0\n'
        ),
        "getent": (
            f'#!/usr/bin/env bash\nprintf \'{getent_ip} %s\\n\' "$2" >> "$MOCK_LOG"\n'
        ),
        "mountpoint": (
            '#!/usr/bin/env bash\nprintf \'mountpoint %s\\n\' "$1" >> "$MOCK_LOG"\n'
            f"{'exit 0' if mount_ok else 'exit 1'}\n"
        ),
    }
    for name in tools:
        (bin_dir / name).write_text(scripts[name], encoding="utf-8")
        os.chmod(bin_dir / name, 0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("MOCK_LOG", str(tmp_path / "mock.log"))
    monkeypatch.setenv("MOCK_REMOTE_DIR", str(tmp_path / "remote"))
    (tmp_path / "remote").mkdir(exist_ok=True)
    return tmp_path / "mock.log"


def _fresh_dump(dir_path: Path, name="agent-demo-2026-10-23.sql.gz"):
    """造一份「新鲜」的 dump + sidecar（新鲜度闸门 <30h）。"""
    dir_path.mkdir(parents=True, exist_ok=True)
    dump = dir_path / name
    dump.write_bytes(b"SQLDUMP")
    (dir_path / f"{name}.sha256").write_text(
        hashlib.sha256(b"SQLDUMP").hexdigest() + "\n", encoding="utf-8"
    )
    return dump


class TestMirrorScriptGuards:
    """backup_mirror.sh：M11 注入面 / M12 私网闸门 / M14 假成功 / L9/L10 口径。"""

    SCRIPT = "scripts/backup_mirror.sh"

    def _run(self, tmp_path, monkeypatch, env, *, src=True, remote_seed=True):
        log = _mock_bin(tmp_path, monkeypatch)
        src_dir = tmp_path / "src"
        if src:
            _fresh_dump(src_dir / "backups")
        monkeypatch.setenv("SRC", str(src_dir))
        monkeypatch.setenv("MIRROR_HOST", "nas-backup")
        monkeypatch.setenv("MIRROR_ROOT", "/vol1/backup/agent-demo")
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        if remote_seed:
            # 远端目录预置同步过来的 dump + sidecar（rsync 是 mock，手动铺）
            remote = tmp_path / "remote" / "backups"
            _fresh_dump(remote)
        proc = subprocess.run(
            ["bash", self.SCRIPT],
            capture_output=True,
            text=True,
            env={**os.environ, "SRC": str(src_dir)},
            timeout=60,
        )
        return proc, log

    def test_happy_path_verifies_and_uses_double_dash(self, monkeypatch, tmp_path):
        """正常跑通：远端校验 ok，且 ssh 目标以 `--` 分隔（选项注入面收口）。"""
        proc, log = self._run(tmp_path, monkeypatch, {"MAX_GB": "0"})
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "ok: agent-demo" in proc.stdout
        hosts = [
            ln.split("=", 1)[1]
            for ln in log.read_text(encoding="utf-8").splitlines()
            if ln.startswith("ssh-host=")
        ]
        assert hosts and all(not h.startswith("-") for h in hosts)

    def test_rejects_non_numeric_keep_days(self, monkeypatch, tmp_path):
        """M11：KEEP_DAYS 拼进远端 find 谓词，非数字即拒（旧实现无校验）。"""
        proc, _ = self._run(tmp_path, monkeypatch, {"KEEP_DAYS": "1; touch /tmp/pwned"})
        assert proc.returncode == 2
        assert "KEEP_DAYS 必须是非负整数" in proc.stdout + proc.stderr

    def test_rejects_quote_in_mirror_root(self, monkeypatch, tmp_path):
        """M11：MIRROR_ROOT 含单引号 = 远端 shell 引号闭合，直接拒。"""
        proc, _ = self._run(tmp_path, monkeypatch, {"MIRROR_ROOT": "/x'y"})
        assert proc.returncode == 2
        assert "含单引号" in proc.stdout + proc.stderr

    def test_missing_backups_dir_fails_loud(self, monkeypatch, tmp_path):
        """M14：backups 目录缺失必须失败——旧实现静默 exit 0 假成功。"""
        proc, _ = self._run(tmp_path, monkeypatch, {}, src=False)
        assert proc.returncode == 1
        assert "不存在" in proc.stdout + proc.stderr

    def test_require_private_ip_rejects_public(self, monkeypatch, tmp_path):
        """M12：REQUIRE_PRIVATE_IP=1 时解析到公网地址即拒（VPS 防漂移）。"""
        _mock_bin(tmp_path, monkeypatch, getent_ip="203.0.113.9")
        src_dir = tmp_path / "src"
        _fresh_dump(src_dir / "backups")
        proc = subprocess.run(
            ["bash", self.SCRIPT],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PATH": os.environ["PATH"],
                "SRC": str(src_dir),
                "MIRROR_HOST": "vps-backup",
                "MIRROR_ROOT": "/srv/backup/agent-demo",
                "REQUIRE_PRIVATE_IP": "1",
                "MOCK_LOG": str(tmp_path / "mock.log"),
                "MOCK_REMOTE_DIR": str(tmp_path / "remote"),
            },
            timeout=60,
        )
        assert proc.returncode == 1
        assert "非私网地址" in proc.stdout + proc.stderr

    def test_missing_sidecar_is_its_own_failure(self, monkeypatch, tmp_path):
        """L9：缺 sidecar 与校验和不匹配是两种失败（旧实现每晚红、不可区分）。"""
        _mock_bin(tmp_path, monkeypatch)
        src_dir = tmp_path / "src"
        _fresh_dump(src_dir / "backups")
        remote = tmp_path / "remote" / "backups"
        remote.mkdir(parents=True)
        (remote / "agent-demo-2026-10-23.sql.gz").write_bytes(b"SQLDUMP")  # 无 sidecar
        proc = subprocess.run(
            ["bash", self.SCRIPT],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PATH": os.environ["PATH"],
                "SRC": str(src_dir),
                "MIRROR_HOST": "nas-backup",
                "MIRROR_ROOT": "/vol1/backup/agent-demo",
                "MOCK_LOG": str(tmp_path / "mock.log"),
                "MOCK_REMOTE_DIR": str(tmp_path / "remote"),
            },
            timeout=60,
        )
        assert proc.returncode != 0
        assert "NO-SIDECAR" in proc.stdout + proc.stderr

    def test_freshness_gate_accepts_jsonl_only(self, monkeypatch, tmp_path):
        """L10：纯 JSONL 部署（DB 不可达降级备份）不该每晚误报失败。"""
        _mock_bin(tmp_path, monkeypatch)
        src_dir = tmp_path / "src"
        dumps = src_dir / "backups"
        dumps.mkdir(parents=True)
        j = dumps / "agent-demo-2026-10-23.jsonl.gz"
        j.write_bytes(b"JSONLDUMP")
        remote = tmp_path / "remote" / "backups"
        _fresh_dump(remote)
        proc = subprocess.run(
            ["bash", self.SCRIPT],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PATH": os.environ["PATH"],
                "SRC": str(src_dir),
                "MIRROR_HOST": "nas-backup",
                "MIRROR_ROOT": "/vol1/backup/agent-demo",
                "MOCK_LOG": str(tmp_path / "mock.log"),
                "MOCK_REMOTE_DIR": str(tmp_path / "remote"),
            },
            timeout=60,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "没有 .sql.gz" not in proc.stdout + proc.stderr


class TestUsbScriptGuards:
    """backup_usb.sh：M10 挂载判定 / M15 存在性断言 / L12 mtime 轮转。"""

    SCRIPT = "scripts/backup_usb.sh"

    def _src(self, tmp_path, *, with_env=True, with_config=True):
        src = tmp_path / "repo"
        _fresh_dump(src / "data" / "backups")
        if with_env:
            (src / ".env").write_text("LLM_API_KEY=sk-x\n", encoding="utf-8")
        if with_config:
            (src / "config.yaml").write_text("agent: {}\n", encoding="utf-8")
        (src / "agentcore" / "personas").mkdir(parents=True, exist_ok=True)
        (src / "agentcore" / "personas" / "assistant.md").write_text(
            "# a\n", encoding="utf-8"
        )
        return src

    def _run(self, tmp_path, monkeypatch, src, usb, *, mount_ok=True):
        log = _mock_bin(tmp_path, monkeypatch, mount_ok=mount_ok, tools=("mountpoint",))
        monkeypatch.setenv("USB_ROOT", str(usb))
        proc = subprocess.run(
            ["bash", self.SCRIPT],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PATH": os.environ["PATH"],
                "USB_ROOT": str(usb),
                "SRC": str(src),
                "MOCK_LOG": str(tmp_path / "mock.log"),
                "MOCK_REMOTE_DIR": str(tmp_path / "remote"),
            },
            timeout=120,
        )
        return proc, log

    def test_unmounted_skips_without_writing(self, monkeypatch, tmp_path):
        """M10：挂载点目录存在但 mountpoint -q 为假（拔盘常态）→ 跳过且零写入。

        旧实现只查 -d/-w：目录常驻 → 全真 → 9GB 冷备写进根盘 + 假成功。
        """
        src = self._src(tmp_path)
        usb = tmp_path / "mnt" / "usb"
        usb.mkdir(parents=True)  # 挂载点目录常驻（拔盘后依然在）
        proc, log = self._run(tmp_path, monkeypatch, src, usb, mount_ok=False)
        assert proc.returncode == 0
        assert "未挂载" in proc.stdout + proc.stderr
        assert list(usb.iterdir()) == [], "未挂载不得写任何东西"

    def test_mounted_bundle_with_manifest(self, monkeypatch, tmp_path):
        """挂载正常时完整打包：MANIFEST + sidecar 双校验通过后落当天目录。"""
        src = self._src(tmp_path)
        usb = tmp_path / "mnt" / "usb"
        usb.mkdir(parents=True)
        proc, _ = self._run(tmp_path, monkeypatch, src, usb, mount_ok=True)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        dest = usb / f"agent-demo-{__import__('datetime').date.today():%Y-%m-%d}"
        assert (dest / "MANIFEST.sha256").is_file()
        assert (dest / "db.sql.gz").is_file()
        assert (dest / ".env").is_file()
        assert (dest / "personas" / "assistant.md").is_file()

    def test_missing_required_file_fails(self, monkeypatch, tmp_path):
        """M15：必含清单缺 .env → 放弃本次（MANIFEST 自洽校验发现不了缺失）。"""
        src = self._src(tmp_path, with_env=False)
        usb = tmp_path / "mnt" / "usb"
        usb.mkdir(parents=True)
        proc, _ = self._run(tmp_path, monkeypatch, src, usb, mount_ok=True)
        assert proc.returncode != 0
        out = proc.stdout + proc.stderr
        assert "必含文件缺失" in out or "rsync error" in out
        dest = usb / f"agent-demo-{__import__('datetime').date.today():%Y-%m-%d}"
        assert not dest.exists(), "缺必含文件不得落当天目录"

    def test_json_deployment_bundle_carries_agent_json(self, monkeypatch, tmp_path):
        """JSON 部署（没有 .env，配置在 config/agent.json）也必须能打冷备包。

        2026-10-28 JSON 化后 backup_usb.sh 仍把 ``.env`` 当唯一配置源：rsync 直接
        报错、必含清单必然缺一项 → 整个 NAS 部署形态的 U 盘冷备不可用，而它是
        「配置改坏了」时唯一能救回 LLM key 的那份。
        """
        src = self._src(tmp_path, with_env=False)
        (src / "config").mkdir()
        (src / "config" / "agent.json").write_text(
            '{"LLM_API_KEY": "sk-x"}\n', encoding="utf-8"
        )
        (src / "config" / "agent.env").write_text(
            "LLM_MODEL=step-3\n", encoding="utf-8"
        )
        usb = tmp_path / "mnt" / "usb"
        usb.mkdir(parents=True)
        proc, _ = self._run(tmp_path, monkeypatch, src, usb, mount_ok=True)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        dest = usb / f"agent-demo-{__import__('datetime').date.today():%Y-%m-%d}"
        assert (dest / "config" / "agent.json").is_file()
        assert (dest / "config" / "agent.env").is_file()
        # 必含清单里必须有它，否则 MANIFEST 自洽校验照样全绿（M15 的教训）
        manifest = (dest / "MANIFEST.sha256").read_text(encoding="utf-8")
        assert "config/agent.json" in manifest

    def test_rotation_uses_mtime(self, monkeypatch, tmp_path):
        """L12：轮转按 mtime 保留最近 KEEP 份（名字排序在时钟回拨时误删当天份）。"""
        src = self._src(tmp_path)
        usb = tmp_path / "mnt" / "usb"
        usb.mkdir(parents=True)
        import datetime as dt
        import time

        today = dt.date.today()
        # 9 份历史：mtime 从旧到新；最后一份是「当天」（时钟回拨场景里它的名字
        # 日期最大但 mtime 若被设旧，名字排序会优先删它）
        for i in range(9):
            d = usb / f"agent-demo-2026-10-{i + 10:02d}"
            d.mkdir()
            (d / "marker").write_text(str(i), encoding="utf-8")
            stamp = (today - dt.timedelta(days=20 - i)).strftime("%Y%m%d%H%M")
            os.utime(d, (time.mktime(time.strptime(stamp, "%Y%m%d%H%M")),) * 2)
        proc, _ = self._run(tmp_path, monkeypatch, src, usb, mount_ok=True)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        remaining = sorted(
            p.name for p in usb.iterdir() if p.name.startswith("agent-demo-20")
        )
        # KEEP=8：共 10 份（9 旧 + 当天）→ 删 mtime 最旧 2 份、留 8 份
        assert "agent-demo-2026-10-10" not in remaining
        assert "agent-demo-2026-10-11" not in remaining
        assert "agent-demo-2026-10-18" in remaining  # mtime 较新，必须留
        assert len(remaining) == 8  # 7 旧 + 当天
        assert f"agent-demo-{today:%Y-%m-%d}" in remaining
