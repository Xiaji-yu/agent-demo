"""数据库备份/恢复。

策略（`strategy=auto`）：
1. **pg_dump**（优先）：完美保真（schema + 数据 + vector 类型）。宿主机没有 pg 客户端时，
   自动改用运行 PG 的容器 `docker exec <container> pg_dump ...`——容器内进程不继承
   宿主机环境变量，密码必须经 `-e PGPASSWORD=...` 显式传入（否则静默降级、必然认证失败）。
2. **JSONL 导出**（兜底）：纯 asyncpg 读全表写 gzip JSONL，无外部依赖；
   恢复时按表插回（`ON CONFLICT <主键> DO NOTHING` + 重置序列）。

两者产物都放在 `data/backups/`，按天命名，按保留天数轮转。备份范围是**全部业务表**
（含 `schedules`——用户提醒的持久化完全依赖它，漏掉则恢复后提醒全丢）。

完整性保障（进程中断/磁盘满不应留下「看起来最新」的半截备份）：
- 原子写：先写 `<name>.part`，成功后 `os.replace` 到最终名；
- 校验和：写成功后生成 `<name>.sha256` sidecar；`verify_backup` 做全量校验
  （旧备份无 sidecar 时退化为全量可读性检查）；prune 先删校验失败的文件，
  保留期只对完好的备份计数。

按天 + 轮转意味着：**任何一次事故最多丢一天**，而不是全部。
恢复入口只经由 `scripts/backup_db.py`（需要显式 --yes；.sql.gz 恢复前会自动做一次
pre-restore 快照），避免误触。
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover — Windows 无 fcntl，见 _backup_lock 的降级
    _fcntl = None

DEFAULT_KEEP = 7
DEFAULT_DIR = "data/backups"
# L15：schedules 是用户提醒的持久化层，必须随库备份/恢复，否则恢复后提醒全丢。
# 各表主键见 store.py DDL：除 user_state 主键为 user_id 外，其余均为自增 id。
_PGDATA_TABLES = (
    "sessions",
    "messages",
    "facts",
    "kb_sources",
    "kb_chunks",
    "schedules",
    "user_state",
)
# 恢复顺序：先父表后子表（外键依赖）
_RESTORE_ORDER = (
    "sessions",
    "user_state",
    "messages",
    "facts",
    "kb_sources",
    "kb_chunks",
    "schedules",
)
_SEQ_TABLES = ("sessions", "messages", "facts", "kb_sources", "kb_chunks", "schedules")
# ON CONFLICT 的冲突目标 = 各表主键列（M4：user_state 没有 id 列，主键是 user_id）
_CONFLICT_TARGET: dict[str, str] = {"user_state": "user_id"}
# M3：恢复时表名/列名要拼进 SQL（值已参数化，标识符没有）——被篡改的备份文件
# 不得借此注入任意 SQL。表名必须在白名单内，列名必须是安全标识符。
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BACKUP_LOCK_NAME = ".backup.lock"
_PG_DUMP_TIMEOUT = 300  # 秒；pg_dump 整体超时（stdout 流读完 + 进程退出）


# ---------- 工具探测 ----------
def find_pg_dump() -> tuple[list[str], str] | None:
    """返回 (执行前缀, 说明)。宿主机有 pg_dump 直接用；否则尝试 docker exec。"""
    if shutil.which("pg_dump"):
        return (["pg_dump"], "local pg_dump")
    if shutil.which("docker"):
        container = os.getenv("PG_CONTAINER", "agent-demo-db-1")
        try:
            probe = subprocess.run(
                ["docker", "exec", container, "which", "pg_dump"],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (subprocess.TimeoutExpired, OSError):
            # M（REVIEW-a604023..679c9b3）：docker 守护无响应时原实现让异常逃出
            # backup_database 的 RuntimeError 捕获范围 → strategy="auto" 直接失败、
            # 不回退 JSONL。这里退化为"容器方式不可用"。
            logger.warning(
                "docker exec 探测 pg_dump 失败/超时，判定为不可用", exc_info=True
            )
            return None
        if probe.returncode == 0:
            return (
                ["docker", "exec", "-i", container, "pg_dump"],
                f"pg_dump in container {container}",
            )
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


def _with_container_env(prefix: list[str], password: str) -> list[str]:
    """L15：容器内进程不继承宿主机环境变量，PGPASSWORD 必须经 `docker exec -e`
    传进容器。注意 -e 必须放在**容器名之前**（docker exec 的参数），否则会被当成
    容器内命令自己的参数（pg_dump 的 -e 恰好是 --extension，报错极具迷惑性）。
    本机 pg_dump/psql 路径走进程环境变量（见各调用点的 env），原样返回。
    """
    if not password or not prefix or prefix[0] != "docker":
        return list(prefix)
    idx = 2  # 跳过 "docker exec"
    while idx < len(prefix) and prefix[idx].startswith("-"):
        idx += 1
    return [*prefix[:idx], "-e", f"PGPASSWORD={password}", *prefix[idx:]]


# ---------- 备份 ----------
def _backup_path(out_dir: Path, tag: str, suffix: str) -> Path:
    return out_dir / f"{tag}-{time.strftime('%Y-%m-%d')}{suffix}"


def _harden(path: Path) -> None:
    """备份含全部聊天记录/人格数据，落盘即收紧为仅属主可读写（0600，M5）。

    Windows 无 POSIX 权限语义：chmod 可能无效或抛错，静默跳过。
    """
    try:
        os.chmod(path, 0o600)
    except OSError:
        logger.debug("backup: chmod 0600 skipped for %s", path, exc_info=True)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_checksum(path: Path) -> None:
    """写 `<name>.sha256` sidecar（hex 摘要），供 verify / 异地核对（M6）。"""
    sidecar = Path(f"{path}.sha256")
    tmp = Path(f"{sidecar}.part")
    tmp.write_text(_sha256_file(path) + "\n", encoding="utf-8")
    os.replace(tmp, sidecar)


@contextlib.contextmanager
def _backup_lock(out_dir: Path):
    """备份全程互斥（L13）：cron 与手动同时跑会交叉写坏同一当日文件。

    LOCK_NB：拿不到锁直接报「已有备份在运行」，绝不排队——排队的备份做完时，
    前一个的 prune 可能已把它刚写的当日文件轮转掉。Windows 无 fcntl：降级为无锁。
    """
    if _fcntl is None:
        logger.debug("backup lock: fcntl unavailable (Windows?), running without lock")
        yield
        return
    lock_path = out_dir / _BACKUP_LOCK_NAME
    fh = open(lock_path, "w")
    try:
        try:
            _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError(f"已有备份在运行（拿不到 {lock_path}）") from None
        try:
            yield
        finally:
            _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)
    finally:
        fh.close()


async def backup_database(
    db_url: str,
    out_dir: str | Path = DEFAULT_DIR,
    *,
    keep: int = DEFAULT_KEEP,
    strategy: str = "auto",
    tag: str = "agent-demo",
    mirror_dir: str | Path | None = None,
) -> dict:
    """做一次备份，返回 {path, strategy, bytes, pruned, mirrored, mirror_path}。

    mirror_dir 给定时，会在本地备份成功后再复制一份到该目录（异地/另一块盘），
    并按同样的 keep 轮转。镜像失败**不算本地备份失败**，但会在结果里标
    `mirrored=False` 并 error 级告警——否则用户会以为有异地副本而实际没有。
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    with _backup_lock(out):
        result = None
        if strategy in ("auto", "pg_dump"):
            try:
                result = await _backup_pg_dump(db_url, out, tag)
            except RuntimeError:
                if strategy == "pg_dump":
                    raise
                # auto：pg_dump 失败回退 JSONL（原因已带 stderr 尾部记录在案）
                logger.warning(
                    "pg_dump failed, falling back to JSONL export", exc_info=True
                )
            if result is None and strategy == "pg_dump":
                raise RuntimeError("pg_dump 不可用（宿主机与容器都没有）")
        if result is None:
            result = await _backup_jsonl(db_url, out, tag)

        # M（REVIEW-a604023..679c9b3）：prune 内部对每份备份全量 gunzip + 逐行
        # json.loads + 整份 SHA256，同步跑会停摆事件循环（实测 2.16s/41MB）→ 移入线程
        pruned = await asyncio.to_thread(prune_backups, out, keep, tag)
        result["pruned"] = pruned
        logger.info(
            "backup: %s (%s, %.1f KB), pruned=%s",
            result["path"],
            result["strategy"],
            result["bytes"] / 1024,
            pruned,
        )

        if mirror_dir:
            mirrored, mirror_path = await asyncio.to_thread(
                _mirror_backup, Path(result["path"]), Path(mirror_dir), keep, tag
            )
            result["mirrored"] = mirrored
            result["mirror_path"] = mirror_path
    return result


def _mirror_backup(
    src: Path, mirror_dir: Path, keep: int, tag: str = "agent-demo"
) -> tuple[bool, str | None]:
    """把备份复制到镜像目录并轮转。返回 (是否成功, 目标路径)。"""
    try:
        mirror_dir.mkdir(parents=True, exist_ok=True)
        dst = mirror_dir / src.name
        shutil.copy2(src, dst)
        # M（REVIEW-a604023..679c9b3）：sidecar 校验和必须同镜像——
        # 否则异地副本永久 checksum="missing"，无法识别"gzip 合法但内容被改"
        sidecar = src.with_name(src.name + ".sha256")
        if sidecar.is_file():
            shutil.copy2(sidecar, mirror_dir / sidecar.name)
        else:
            logger.warning("源备份缺少 .sha256 sidecar，镜像副本将无法校验：%s", src)
        prune_backups(mirror_dir, keep, tag=tag)
        logger.info("backup mirrored: %s", dst)
        return True, str(dst)
    except Exception:
        logger.error(
            "备份镜像失败（%s → %s）：本地备份成功，但异地副本未更新，请检查该路径是否挂载/可写",
            src,
            mirror_dir,
            exc_info=True,
        )
        return False, None


def _pump_pg_dump_stdout(proc: subprocess.Popen, part: Path) -> None:
    # 注意：不能把 GzipFile 直接交给 subprocess（它只用底层 fd，会绕过压缩层）。
    # 这里流式读取子进程输出并压缩写入，避免整份 dump 进内存。
    with gzip.open(part, "wb") as fh:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(65536)
            if not chunk:
                break
            fh.write(chunk)


def _stderr_tail(err_fh, limit: int = 800) -> str:
    try:
        err_fh.seek(0)
        return err_fh.read()[-limit:].decode("utf-8", "replace").strip()
    except OSError:
        return ""


async def _backup_pg_dump(db_url: str, out: Path, tag: str) -> dict | None:
    spec = find_pg_dump()
    if spec is None:
        return None
    prefix, desc = spec
    p = _dsn_parts(db_url)
    path = _backup_path(out, tag, ".sql.gz")
    part = Path(f"{path}.part")
    env = dict(os.environ)
    if p["password"]:
        env["PGPASSWORD"] = p["password"]
    cmd = [
        *_with_container_env(prefix, p["password"]),
        "-h",
        p["host"],
        "-p",
        str(p["port"]),
        "-U",
        p["user"],
        "--no-owner",
        "--no-privileges",
        "--clean",
        "--if-exists",
        p["database"],
    ]
    # L14：stderr 落临时文件而不是 PIPE——无人读的 PIPE 塞满（>64KB）会永久挂起；
    # stdout 抽水整体包 asyncio.wait_for，pg_dump 卡死时能超时杀掉并把 stderr
    # 尾部带进异常消息。
    loop = asyncio.get_running_loop()
    with tempfile.TemporaryFile() as err_fh:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err_fh, env=env)
        try:
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(None, _pump_pg_dump_stdout, proc, part),
                    timeout=_PG_DUMP_TIMEOUT,
                )
            except TimeoutError:
                proc.kill()
                proc.wait()
                raise RuntimeError(
                    f"pg_dump 超时（>{_PG_DUMP_TIMEOUT}s），已终止：{_stderr_tail(err_fh)}"
                ) from None
            try:
                returncode = proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()
                raise RuntimeError(
                    f"pg_dump 退出超时，已终止：{_stderr_tail(err_fh)}"
                ) from None
        except RuntimeError:
            part.unlink(missing_ok=True)
            raise
        except Exception:
            part.unlink(missing_ok=True)
            logger.exception("pg_dump 执行异常，回退 JSONL 导出")
            return None
        if returncode != 0:
            part.unlink(missing_ok=True)
            raise RuntimeError(f"pg_dump 退出码 {returncode}：{_stderr_tail(err_fh)}")
    os.replace(part, path)  # M6：原子改名，绝不留半截文件顶着最终名
    _harden(path)
    _write_checksum(path)
    return {"path": str(path), "strategy": desc, "bytes": path.stat().st_size}


def _serialize_rows(table: str, records) -> str:
    """把一批行序列化成 JSONL 文本（CPU 密集，供 to_thread 调用）。"""
    parts = []
    for r in records:
        parts.append(
            json.dumps(
                {"table": table, "row": _jsonable(dict(r))},
                ensure_ascii=False,
            )
            + "\n"
        )
    return "".join(parts)


async def _backup_jsonl(db_url: str, out: Path, tag: str) -> dict:
    import asyncpg

    path = _backup_path(out, tag, ".jsonl.gz")
    part = Path(f"{path}.part")
    conn = await asyncpg.connect(db_url)
    rows_total = 0
    try:
        try:
            with gzip.open(part, "wt", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "__meta__": {
                                "created": time.time(),
                                "tables": list(_PGDATA_TABLES),
                            }
                        }
                    )
                    + "\n"
                )
                for table in _PGDATA_TABLES:
                    records = await conn.fetch(f"SELECT * FROM {table}")  # noqa: S608 — 表名为本模块常量
                    # M（REVIEW-a604023..679c9b3）：json.dumps + gzip 写原在事件循环里同步
                    # 执行（实测 10 万行停摆 4.27s）→ 序列化移入线程，仅保留 await 写回
                    payload_text = await asyncio.to_thread(
                        _serialize_rows, table, records
                    )
                    fh.write(payload_text)
                    rows_total += len(records)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
    finally:
        await conn.close()
    os.replace(part, path)
    _harden(path)
    _write_checksum(path)
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


def list_backups(
    out_dir: str | Path = DEFAULT_DIR, tag: str = "agent-demo"
) -> list[dict]:
    out = Path(out_dir)
    if not out.is_dir():
        return []
    items = []
    for p in sorted(out.glob(f"{tag}-*")):
        if p.suffix not in (".gz",):
            continue  # .sha256 / .part 不算备份
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


def verify_backup(path: str | Path) -> dict:
    """全量校验一份备份（M6），返回 {"ok", "kind", "checksum", "tables"?, "error"?}。

    - .sql.gz：gzip 完整解压读完（截断/坏流判坏）；
    - .jsonl.gz：gzip 完整解压 + 全行 JSON 解析；
    - 存在 `<file>.sha256` sidecar 时校验摘要一致（mismatch 判坏）；
    - 无 sidecar 的旧备份只做全量可读性检查（checksum="missing"）。
    """
    p = Path(path)
    kind = "pg_dump" if p.name.endswith(".sql.gz") else "jsonl"
    tables: dict[str, int] = {}
    error = None
    try:
        if kind == "jsonl":
            with gzip.open(p, "rt", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    if not isinstance(rec, dict) or "__meta__" in rec:
                        continue
                    name = str(rec.get("table") or "<unknown>")
                    tables[name] = tables.get(name, 0) + 1
        else:
            with gzip.open(p, "rb") as fh:
                while fh.read(1 << 20):
                    pass
    except (OSError, EOFError, ValueError) as exc:
        # BadGzipFile ⊂ OSError；截断 ⊂ EOFError；坏 JSON/解码 ⊂ ValueError
        error = f"{type(exc).__name__}: {exc}"[:300]

    checksum = "missing"
    if error is None:
        sidecar = Path(f"{p}.sha256")
        if sidecar.is_file():
            checksum = "mismatch"
            try:
                if sidecar.read_text(encoding="utf-8").strip() == _sha256_file(p):
                    checksum = "verified"
            except OSError:
                checksum = "missing"
            if checksum == "mismatch":
                return {
                    "ok": False,
                    "kind": kind,
                    "checksum": checksum,
                    "error": "sha256 校验和不符（文件被篡改或写入中断）",
                }
    if error is not None:
        return {"ok": False, "kind": kind, "checksum": checksum, "error": error}
    result = {"ok": True, "kind": kind, "checksum": checksum}
    if kind == "jsonl":
        result["tables"] = tables
    return result


def _remove_backup(path: Path) -> bool:
    """删除备份文件及其 sidecar；数据文件删掉才算成功。"""
    try:
        path.unlink()
    except OSError:
        logger.exception("prune backup failed: %s", path)
        return False
    try:
        Path(f"{path}.sha256").unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.exception("prune sidecar failed: %s", path)
    return True


def prune_backups(
    out_dir: str | Path = DEFAULT_DIR, keep: int = DEFAULT_KEEP, tag: str = "agent-demo"
) -> list[str]:
    """先删校验失败的备份（坏 gzip/坏行/校验和不符——半截备份比没有更危险，
    还会顶着「最新」的 mtime 把好备份挤掉），保留期只对通过校验的文件计数。
    返回被删文件名。
    """
    keep = max(1, int(keep))
    items = list_backups(out_dir, tag)
    removed = []
    valid = []
    for item in items:
        p = Path(item["path"])
        check = verify_backup(p)
        if not check["ok"]:
            logger.warning(
                "prune: 删除校验失败的备份 %s（%s）", p.name, check.get("error", "")
            )
            if _remove_backup(p):
                removed.append(p.name)
        else:
            valid.append(item)
    for item in valid[keep:]:
        p = Path(item["path"])
        if _remove_backup(p):
            removed.append(p.name)
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
    cmd = [
        *_with_container_env(prefix, p["password"]),
        "-h",
        p["host"],
        "-p",
        str(p["port"]),
        "-U",
        p["user"],
        "-d",
        p["database"],
        "-v",
        "ON_ERROR_STOP=1",
    ]
    if dry_run:
        return {"strategy": desc, "dry_run": True, "bytes": path.stat().st_size}
    # 同样不能把 GzipFile 交给 subprocess（stdin 也走 fd）：先解压再喂给 psql
    with gzip.open(path, "rb") as fh:
        sql = fh.read()
    logger.info(
        "restore: feeding %.1f MB SQL into %s", len(sql) / 1024 / 1024, p["database"]
    )
    proc = subprocess.run(cmd, input=sql, capture_output=True, env=env, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"恢复失败：{proc.stderr.decode('utf-8', 'replace')[:800]}")
    return {"strategy": desc, "restored": True, "bytes": path.stat().st_size}


def _row_identifiers_safe(table, row) -> bool:
    """M3：恢复前校验标识符。表名必须在恢复顺序白名单内、列名必须是安全标识符；
    否则该行跳过——被篡改的备份文件不得把任意 SQL 拼进 INSERT。"""
    if not isinstance(table, str) or table not in _RESTORE_ORDER:
        return False
    if not isinstance(row, dict) or not row:
        return False
    return all(isinstance(c, str) and _IDENTIFIER_RE.fullmatch(c) for c in row)


async def _restore_jsonl(db_url: str, path: Path, dry_run: bool) -> dict:
    import asyncpg

    counts: dict[str, int] = {}
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or "__meta__" in rec:
                continue
            name = str(rec.get("table") or "<unknown>")
            counts[name] = counts.get(name, 0) + 1
    if dry_run:
        return {"strategy": "asyncpg jsonl", "dry_run": True, "rows": counts}

    conn = await asyncpg.connect(db_url)
    restored: dict[str, int] = {}
    failures: dict[str, int] = {}
    try:
        async with conn.transaction():
            for table in _RESTORE_ORDER:
                restored[table] = 0
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        failures["<unparseable>"] = failures.get("<unparseable>", 0) + 1
                        continue
                    if not isinstance(rec, dict) or "__meta__" in rec:
                        continue
                    table = rec.get("table")
                    if not _row_identifiers_safe(table, rec.get("row")):
                        # M3：恶意/被篡改的行直接跳过并计入失败，绝不拼进 SQL
                        key = table if isinstance(table, str) and table else "<invalid>"
                        failures[key] = failures.get(key, 0) + 1
                        logger.warning(
                            "restore: skip row with untrusted identifier (table=%r)",
                            table,
                        )
                        continue
                    row = _unjsonable(rec["row"])
                    cols = list(row.keys())
                    quoted = ", ".join(f'"{c}"' for c in cols)  # 已过白名单，双引号兜底
                    placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
                    conflict = _CONFLICT_TARGET.get(table, "id")
                    sql = (
                        f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders}) '
                        f'ON CONFLICT ("{conflict}") DO NOTHING'
                    )
                    try:
                        # M4：SAVEPOINT 隔离单行失败——一行坏数据不再把整个事务拖进
                        # aborted 状态、把后续行全部连坐成静默失败
                        async with conn.transaction():
                            await conn.execute(sql, *[row[c] for c in cols])
                        restored[table] += 1
                    except Exception as exc:
                        failures[table] = failures.get(table, 0) + 1
                        logger.warning(
                            "restore row failed table=%s: %s", table, str(exc)[:300]
                        )
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
    if failures:
        # M4：恢复必须 fail-loud。静默打印 ✅ 的「零行恢复」比失败本身更危险。
        total = sum(failures.values())
        detail = ", ".join(f"{t}×{n}" for t, n in sorted(failures.items()))
        raise RuntimeError(
            f"恢复失败：{total} 行未能恢复（{detail}），目标库可能不完整"
        )
    return {"strategy": "asyncpg jsonl", "restored": restored}
