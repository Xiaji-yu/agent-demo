"""`_backup_jsonl` 流式读取的回归测试（REVIEW-a604023..679c9b3 M）。

关键回归点：旧实现对每张表执行 ``await conn.fetch("SELECT * FROM t")``——整表进内存，
且序列化在事件循环线程上跑。现在必须：

1. 走 ``conn.cursor(...)`` 游标流式读取，按 ``_JSONL_BATCH_ROWS`` 分批；
2. 每批交给 ``asyncio.to_thread`` 序列化（不阻塞事件循环）；
3. 中途失败时删掉 ``.part`` 临时文件，绝不留下半份备份。

这些用例不依赖 PG（用假连接覆盖协议）；PG 可用时另有一个 1200 行的真库用例，
用「serialize 调用次数 / 单批行数」把「整表 fetch + 一次性序列化」钉死。
"""

import gzip
import hashlib
import json
import os
import stat
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
