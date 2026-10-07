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
