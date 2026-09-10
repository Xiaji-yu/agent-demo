"""数据库备份/恢复。

策略（`strategy=auto`）：
1. **pg_dump**（优先）：完美保真（schema + 数据 + vector 类型）。宿主机没有 pg 客户端时，
   自动改用运行 PG 的容器 `docker exec <container> pg_dump ...`。
2. **JSONL 导出**（兜底）：纯 asyncpg 读全表写 gzip JSONL，无外部依赖；
   恢复时按表插回（`ON CONFLICT (id) DO NOTHING` + 重置序列）。

两者产物都放在 `data/backups/`，按天命名，按保留天数轮转。
按天 + 轮转意味着：**任何一次事故最多丢一天**，而不是全部。

恢复入口只经由 `scripts/backup_db.py`（需要显式 --yes），避免误触。
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_KEEP = 7
DEFAULT_DIR = "data/backups"
_PGDATA_TABLES = ("sessions", "messages", "facts", "kb_sources", "kb_chunks", "user_state")
# 恢复顺序：先父表后子表（外键依赖）
_RESTORE_ORDER = ("sessions", "user_state", "messages", "facts", "kb_sources", "kb_chunks")
_SEQ_TABLES = ("sessions", "messages", "facts", "kb_sources", "kb_chunks")


# ---------- 工具探测 ----------
def find_pg_dump() -> tuple[list[str], str] | None:
    """返回 (执行前缀, 说明)。宿主机有 pg_dump 直接用；否则尝试 docker exec。"""
    if shutil.which("pg_dump"):
        return (["pg_dump"], "local pg_dump")
    if shutil.which("docker"):
        container = os.getenv("PG_CONTAINER", "agent-demo-db-1")
        probe = subprocess.run(
            ["docker", "exec", container, "which", "pg_dump"],
            capture_output=True, text=True, timeout=20,
        )
        if probe.returncode == 0:
            return (["docker", "exec", "-i", container, "pg_dump"], f"pg_dump in container {container}")
    return None


def _dsn_parts(db_url: str) -> dict:
    import urllib.parse as up

    p = up.urlparse(db_url)
    return {
        "host": p.hostname or "127.0.0.1",
        "port": int(p.port or 5432),
        "user": up.unquote(p.username or ""),
        "password": up.unquote(p.password or ""),
        "database": (p.path or "").lstrip("/"),
    }


# ---------- 备份 ----------
def _backup_path(out_dir: Path, tag: str, suffix: str) -> Path:
    return out_dir / f"{tag}-{time.strftime('%Y-%m-%d')}{suffix}"


async def backup_database(
    db_url: str,
    out_dir: str | Path = DEFAULT_DIR,
    *,
    keep: int = DEFAULT_KEEP,
    strategy: str = "auto",
    tag: str = "agent-demo",
) -> dict:
    """做一次备份，返回 {path, strategy, bytes, kept, pruned}。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    result = None
    if strategy in ("auto", "pg_dump"):
        result = await asyncio.to_thread(_backup_pg_dump, db_url, out, tag)
        if result is None and strategy == "pg_dump":
            raise RuntimeError("pg_dump 不可用（宿主机与容器都没有）")
    if result is None:
        result = await _backup_jsonl(db_url, out, tag)

    pruned = prune_backups(out, keep, tag=tag)
    result["pruned"] = pruned
    logger.info(
        "backup: %s (%s, %.1f KB), pruned=%s",
        result["path"], result["strategy"], result["bytes"] / 1024, pruned,
    )
    return result


def _backup_pg_dump(db_url: str, out: Path, tag: str) -> dict | None:
    spec = find_pg_dump()
    if spec is None:
        return None
    prefix, desc = spec
    p = _dsn_parts(db_url)
    path = _backup_path(out, tag, ".sql.gz")
    env = dict(os.environ)
    if p["password"]:
        env["PGPASSWORD"] = p["password"]
    cmd = [
        *prefix,
        "-h", p["host"], "-p", str(p["port"]), "-U", p["user"],
        "--no-owner", "--no-privileges", "--clean", "--if-exists",
        p["database"],
    ]
    try:
        # 注意：不能把 GzipFile 直接交给 subprocess（它只用底层 fd，会绕过压缩层）。
        # 这里流式读取子进程输出并压缩写入，避免整份 dump 进内存。
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        try:
            with gzip.open(path, "wb") as fh:
                assert proc.stdout is not None
                while True:
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break
                    fh.write(chunk)
            _, stderr = proc.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise
        if proc.returncode != 0:
            err = (stderr or b"").decode("utf-8", "replace")[:500]
            logger.warning("pg_dump failed (%s), falling back to JSONL export", err)
            path.unlink(missing_ok=True)
            return None
    except Exception:
        logger.exception("pg_dump 执行异常，回退 JSONL 导出")
        path.unlink(missing_ok=True)
        return None
    return {"path": str(path), "strategy": desc, "bytes": path.stat().st_size}


async def _backup_jsonl(db_url: str, out: Path, tag: str) -> dict:
    import asyncpg

    path = _backup_path(out, tag, ".jsonl.gz")
    conn = await asyncpg.connect(db_url)
    rows_total = 0
    try:
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            fh.write(json.dumps({"__meta__": {"created": time.time(), "tables": list(_PGDATA_TABLES)}}) + "\n")
            for table in _PGDATA_TABLES:
                records = await conn.fetch(f"SELECT * FROM {table}")
                for r in records:
                    fh.write(json.dumps({"table": table, "row": _jsonable(dict(r))}, ensure_ascii=False) + "\n")
                    rows_total += 1
    finally:
        await conn.close()
    return {
        "path": str(path),
        "strategy": "asyncpg jsonl",
        "bytes": path.stat().st_size,
        "rows": rows_total,
    }


def _jsonable(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        if hasattr(v, "isoformat"):
            out[k] = {"__type__": "datetime", "value": v.isoformat()}
        else:
            out[k] = v
    return out


def _unjsonable(row: dict) -> dict:
    """JSONL 里的标记字段还原成 Python 类型（asyncpg 需要 datetime 而不是 str）。"""
    out = {}
    for k, v in row.items():
        if isinstance(v, dict) and v.get("__type__") == "datetime":
            try:
                out[k] = datetime.fromisoformat(v["value"])
            except Exception:
                logger.warning("restore: unparseable datetime for column %s", k)
                out[k] = None
        else:
            out[k] = v
    return out


def list_backups(out_dir: str | Path = DEFAULT_DIR, tag: str = "agent-demo") -> list[dict]:
    out = Path(out_dir)
    if not out.is_dir():
        return []
    items = []
    for p in sorted(out.glob(f"{tag}-*")):
        if p.suffix not in (".gz",):
            continue
        stat = p.stat()
        items.append(
            {
                "path": str(p),
                "name": p.name,
                "bytes": stat.st_size,
                "mtime": stat.st_mtime,
                "kind": "pg_dump" if p.name.endswith(".sql.gz") else "jsonl",
            }
        )
    return sorted(items, key=lambda i: i["mtime"], reverse=True)


def prune_backups(out_dir: str | Path = DEFAULT_DIR, keep: int = DEFAULT_KEEP, tag: str = "agent-demo") -> list[str]:
    """只保留最近 keep 份（按修改时间），返回被删文件名。"""
    keep = max(1, int(keep))
    items = list_backups(out_dir, tag)
    removed = []
    for item in items[keep:]:
        try:
            Path(item["path"]).unlink()
            removed.append(item["name"])
        except OSError:
            logger.exception("prune backup failed: %s", item["path"])
    return removed


# ---------- 恢复 ----------
async def restore_database(
    db_url: str,
    dump_path: str | Path,
    *,
    dry_run: bool = False,
) -> dict:
    """从备份恢复。.sql.gz 走 psql/pg_restore；.jsonl.gz 走逐表回灌。"""
    path = Path(dump_path)
    if not path.is_file():
        raise FileNotFoundError(f"备份文件不存在：{path}")
    if path.name.endswith(".sql.gz"):
        return await asyncio.to_thread(_restore_sql, db_url, path, dry_run)
    return await _restore_jsonl(db_url, path, dry_run)


def _restore_sql(db_url: str, path: Path, dry_run: bool) -> dict:
    spec = None
    if shutil.which("psql"):
        spec = (["psql"], "local psql")
    elif shutil.which("docker"):
        container = os.getenv("PG_CONTAINER", "agent-demo-db-1")
        spec = (["docker", "exec", "-i", container, "psql"], f"psql in {container}")
    if spec is None:
        raise RuntimeError("没有 psql 可用，无法恢复 .sql.gz（可改用 JSONL 备份）")
    prefix, desc = spec
    p = _dsn_parts(db_url)
    env = dict(os.environ)
    if p["password"]:
        env["PGPASSWORD"] = p["password"]
    cmd = [*prefix, "-h", p["host"], "-p", str(p["port"]), "-U", p["user"], "-d", p["database"], "-v", "ON_ERROR_STOP=1"]
    if dry_run:
        return {"strategy": desc, "dry_run": True, "bytes": path.stat().st_size}
    # 同样不能把 GzipFile 交给 subprocess（stdin 也走 fd）：先解压再喂给 psql
    with gzip.open(path, "rb") as fh:
        sql = fh.read()
    logger.info("restore: feeding %.1f MB SQL into %s", len(sql) / 1024 / 1024, p["database"])
    proc = subprocess.run(cmd, input=sql, capture_output=True, env=env, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"恢复失败：{proc.stderr.decode('utf-8', 'replace')[:800]}")
    return {"strategy": desc, "restored": True, "bytes": path.stat().st_size}


async def _restore_jsonl(db_url: str, path: Path, dry_run: bool) -> dict:
    import asyncpg

    counts: dict[str, int] = {}
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if "__meta__" in rec:
                continue
            counts[rec["table"]] = counts.get(rec["table"], 0) + 1
    if dry_run:
        return {"strategy": "asyncpg jsonl", "dry_run": True, "rows": counts}

    conn = await asyncpg.connect(db_url)
    restored: dict[str, int] = {}
    try:
        async with conn.transaction():
            for table in _RESTORE_ORDER:
                restored[table] = 0
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    if "__meta__" in rec:
                        continue
                    table = rec["table"]
                    row = _unjsonable(rec["row"])
                    cols = list(row.keys())
                    placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
                    sql = (
                        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
                        f"ON CONFLICT (id) DO NOTHING"
                    )
                    try:
                        await conn.execute(sql, *[row[c] for c in cols])
                        restored[table] = restored.get(table, 0) + 1
                    except Exception:
                        logger.exception("restore row failed table=%s", table)
            for table in _SEQ_TABLES:
                try:
                    await conn.execute(
                        f"SELECT setval(pg_get_serial_sequence('{table}','id'), "
                        f"COALESCE((SELECT MAX(id) FROM {table}), 1))"
                    )
                except Exception:
                    logger.exception("restore: reset sequence failed for %s", table)
    finally:
        await conn.close()
    return {"strategy": "asyncpg jsonl", "restored": restored}
