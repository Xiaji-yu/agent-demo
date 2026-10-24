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
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import IO, BinaryIO

logger = logging.getLogger(__name__)

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover — Windows 无 fcntl，见 _backup_lock 的降级
    _fcntl = None

DEFAULT_KEEP = 7
DEFAULT_DIR = "data/backups"
# 表清单唯一来源见 agentcore/memory/schema.py：新增表漏登记会让备份/恢复
# 静默丢数据（group_persona 就是这样丢过）。这里保留旧名以兼容测试 monkeypatch。
from agentcore.memory.schema import (  # noqa: E402
    CONFLICT_TARGET,
    DATA_TABLES,
    RESTORE_ORDER,
    SEQUENCE_TABLES,
)

_PGDATA_TABLES = DATA_TABLES
# 恢复顺序：先父表后子表（外键依赖）
_RESTORE_ORDER = RESTORE_ORDER
_SEQ_TABLES = SEQUENCE_TABLES
# ON CONFLICT 的冲突目标 = 各表主键列（user_state/group_persona 主键不是 id）
_CONFLICT_TARGET: dict[str, str] = CONFLICT_TARGET
# M3：恢复时表名/列名要拼进 SQL（值已参数化，标识符没有）——被篡改的备份文件
# 不得借此注入任意 SQL。表名必须在白名单内，列名必须是安全标识符。
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BACKUP_LOCK_NAME = ".backup.lock"
_PG_DUMP_TIMEOUT = 300  # 秒；pg_dump 整体超时（stdout 流读完 + 进程退出）
_AGE_MAGIC = b"age-encryption.org/v1"


def _backup_encryption() -> tuple[str, str] | None:
    """读取可选备份加密配置，返回 ``(age 可执行文件, recipient)``。

    ``AGENT_BACKUP_ENCRYPT=age`` 才启用；其余空值/off 视为不加密。配置错误一律
    fail-loud——备份是隐私兜底，绝不能因为写错 key 就悄悄落一份明文全库。
    """
    mode = (os.getenv("AGENT_BACKUP_ENCRYPT") or "").strip().lower()
    if mode in {"", "none", "off", "0"}:
        return None
    if mode != "age":
        raise RuntimeError(
            f"AGENT_BACKUP_ENCRYPT={mode!r} 不支持（可选值：空/off/age）"
        )
    recipient = (os.getenv("AGENT_BACKUP_AGE_RECIPIENT") or "").strip()
    if not recipient:
        raise RuntimeError(
            "AGENT_BACKUP_ENCRYPT=age 但未配置 AGENT_BACKUP_AGE_RECIPIENT"
        )
    if any(ch in recipient for ch in "\r\n"):
        raise RuntimeError("AGENT_BACKUP_AGE_RECIPIENT 含换行，拒绝使用")
    age_bin = shutil.which("age")
    if age_bin is None:
        raise RuntimeError(
            "AGENT_BACKUP_ENCRYPT=age 但宿主机没有 age 命令（请先安装 age）"
        )
    return age_bin, recipient


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


def _sh_quote(value: str) -> str:
    """POSIX 单引号安全包裹（密码可含任意字符）。"""
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _container_script_wrap(
    prefix: list[str], password: str, args: list[str]
) -> tuple[list[str], str]:
    """容器备份路径：密码经 stdin 注入 ``sh -s`` 脚本，不进 argv。

    M7（REVIEW-1e0f5ae..a0a6bb4）：旧实现 `docker exec -e PGPASSWORD=…` 把密码
    放进 docker CLI 的 argv——宿主机 `/proc/<pid>/cmdline` 对同机其他本地用户
    可读。备份路径的 stdin 空闲，改为 stdin 喂脚本（``export PGPASSWORD=…;
    exec pg_dump <逐项转义的参数>``）：argv 只剩无敏感参数，pg_dump 的 stdout
    仍是备份流。**pg_dump 参数必须整段放脚本内**（逐项 `_sh_quote` 转义）——
    不要改回 `sh -s pg_dump "$@"` 位置参数转发：dash 不把首个 operand 当 $0
    （偏离 POSIX），命令名会混进 "$@" 实测炸 pg_dump（too many arguments）。
    返回 ``(argv, stdin_script)``；非容器路径返回 ``(prefix+args, "")``
    （密码走进程环境变量，本就不经 argv）。
    """
    if not password or not prefix or prefix[0] != "docker":
        return [*prefix, *args], ""
    idx = 2  # 跳过 "docker exec"（可选 flag 归下一个循环处理）
    while idx < len(prefix) and prefix[idx].startswith("-"):
        idx += 1
    # prefix[idx] = 容器名，prefix[idx+1] = 容器内命令（pg_dump）
    inner = prefix[idx + 1] if idx + 1 < len(prefix) else "pg_dump"
    quoted = " ".join(_sh_quote(a) for a in args)
    script = f"export PGPASSWORD={_sh_quote(password)}\nexec {inner} {quoted}\n"
    return [*prefix[:idx], prefix[idx], "sh", "-s"], script


def _with_container_env(prefix: list[str], password: str) -> list[str]:
    """L15：容器内进程不继承宿主机环境变量，PGPASSWORD 必须经 `docker exec -e`
    传进容器。注意 -e 必须放在**容器名之前**（docker exec 的参数），否则会被当成
    容器内命令自己的参数（pg_dump 的 -e 恰好是 --extension，报错极具迷惑性）。
    本机 pg_dump/psql 路径走进程环境变量（见各调用点的 env），原样返回。

    **仅 restore（psql）路径使用**（M7，REVIEW-1e0f5ae..a0a6bb4）：密码确实
    驻留 argv 数秒，这里是有意豁免——psql 的 stdin 被恢复 SQL 流占用，
    「sh -s 由 stdin 注入」不可行；换成容器内 PGPASSFILE 落盘则是把「argv
    秒级驻留」换成「容器文件系统驻留 + 三步失败分支」，不成比例。缓解：
    单用户部署、进程秒级结束。备份路径已改 stdin 注入（见
    _container_script_wrap），此处不要再「顺手统一」。
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


async def _current_schema_version(db_url: str) -> str | None:
    """备份时记录 schema 版本，供恢复前做"备份比代码新"的守卫。"""
    import asyncpg

    conn = await asyncpg.connect(db_url)
    try:
        exists = await conn.fetchval(
            "SELECT to_regclass('schema_migrations') IS NOT NULL"
        )
        if not exists:
            return None
        return await conn.fetchval("SELECT max(version) FROM schema_migrations")
    finally:
        await conn.close()


def _schema_sidecar_path(path: Path) -> Path:
    """sidecar 的规范路径：加密备份按**解密后的名字**命名。

    ``a.sql.gz.age`` 的 sidecar 是 ``a.sql.gz.schema.json``——restore 只接受
    解密出的 ``a.sql.gz``，按 ``f"{path}.schema.json"`` 命名会得到
    ``a.sql.gz.age.schema.json``，解密后永远读不到，版本守卫静默失效
    （REVIEW-ee39c8e..e55cab5 M2）。
    """
    name = path.name[: -len(".age")] if path.name.endswith(".age") else path.name
    return path.with_name(name + ".schema.json")


def _write_schema_sidecar(path: Path, version: str) -> None:
    sidecar = _schema_sidecar_path(path)
    tmp = Path(f"{sidecar}.part")
    tmp.write_text(
        json.dumps({"schema_version": version, "created": time.time()}) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, sidecar)
    _harden(sidecar)


def _schema_sidecar_candidates(path: Path) -> list[Path]:
    """读取顺序：规范名 → 旧加密名（<file>.age.schema.json）→ 旧通用名。

    旧加密备份的 sidecar 落在 ``a.sql.gz.age.schema.json``；用户解密出
    ``a.sql.gz`` 后只有这条历史命名能找到版本信息。
    """
    candidates = [_schema_sidecar_path(path), Path(f"{path}.age.schema.json")]
    legacy = Path(f"{path}.schema.json")
    if legacy not in candidates:
        candidates.append(legacy)
    return candidates


def _read_schema_sidecar(path: Path) -> str | None:
    """读 backup 的 schema 版本；没有 sidecar 返回 None（旧备份）。

    存在但损坏/字段不合法时 **raise**：restore 是覆盖性操作，读不到版本就
    无法判断"备份比代码新"，静默跳过等于把版本守卫关掉（L4）。
    """
    sidecar = next((p for p in _schema_sidecar_candidates(path) if p.is_file()), None)
    if sidecar is None:
        return None
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"备份的 schema sidecar 无法解析：{sidecar}") from exc
    version = data.get("schema_version") if isinstance(data, dict) else None
    if isinstance(version, str) and version.isdigit():
        return version
    raise RuntimeError(f"备份的 schema sidecar 缺少合法的 schema_version：{sidecar}")


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
    fd = os.open(lock_path, os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError(f"已有备份在运行（拿不到 {lock_path}）") from None
        try:
            yield
        finally:
            _fcntl.flock(fd, _fcntl.LOCK_UN)
    finally:
        os.close(fd)


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
    encryption = _backup_encryption()
    if encryption and strategy == "jsonl":
        raise RuntimeError(
            "AGENT_BACKUP_ENCRYPT=age 仅支持 pg_dump 备份（JSONL 不回退、不加密）"
        )

    with _backup_lock(out):
        result = None
        if strategy in ("auto", "pg_dump"):
            try:
                result = await _backup_pg_dump(db_url, out, tag, encryption=encryption)
            except RuntimeError:
                if strategy == "pg_dump" or encryption:
                    # 加密请求下绝不回退 JSONL：那会悄悄落一份明文全库
                    raise
                # auto：pg_dump 失败回退 JSONL（原因已带 stderr 尾部记录在案）
                logger.warning(
                    "pg_dump failed, falling back to JSONL export", exc_info=True
                )
            if result is None:
                if encryption:
                    raise RuntimeError(
                        "AGENT_BACKUP_ENCRYPT=age 需要 pg_dump（宿主机与容器都没有）"
                    )
                if strategy == "pg_dump":
                    raise RuntimeError("pg_dump 不可用（宿主机与容器都没有）")
        if result is None:
            result = await _backup_jsonl(db_url, out, tag)

        # M（REVIEW-a604023..679c9b3）：prune 内部对每份备份全量 gunzip + 逐行
        # json.loads + 整份 SHA256，同步跑会停摆事件循环（实测 2.16s/41MB）→ 移入线程
        pruned = await asyncio.to_thread(prune_backups, out, keep, tag)
        result["pruned"] = pruned

        # schema 版本 sidecar 必须在镜像之前写：异地副本要带同一份守卫信息
        try:
            schema_version = await _current_schema_version(db_url)
        except Exception:
            logger.warning(
                "backup: schema version probe failed; restore guard unavailable",
                exc_info=True,
            )
        else:
            if schema_version:
                _write_schema_sidecar(Path(result["path"]), schema_version)
                result["schema_version"] = schema_version
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
        schema_sidecar = _schema_sidecar_path(src)
        if schema_sidecar.is_file():
            shutil.copy2(schema_sidecar, mirror_dir / schema_sidecar.name)
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


def _pump_pg_dump_stdout(
    proc: subprocess.Popen, part: Path, age_stdin: IO[bytes] | None = None
) -> None:
    # 注意：不能把 GzipFile 直接交给 subprocess（它只用底层 fd，会绕过压缩层）。
    # 这里流式读取子进程输出并压缩写入，避免整份 dump 进内存。
    # 加密时压缩流直接喂进 age 的 stdin：明文 SQL 不落盘。
    assert proc.stdout is not None
    if age_stdin is not None:
        with gzip.GzipFile(fileobj=age_stdin, mode="wb") as fh:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                fh.write(chunk)
        return
    with gzip.open(part, "wb") as fh:
        # .part 阶段含全量聊天记录：创建即 0600（080_harden 只在改名后跑）
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


def _pg_dump_rest_args(p: dict) -> list[str]:
    """pg_dump 的连接/过滤参数（不含工具前缀）。

    ``schema_migrations`` 是部署元数据而非业务数据：它随 dump 恢复会让老备份
    把账本"倒回"过去，启动时误判迁移已应用。排除后由启动期 runner 按当前代码
    重建账本（基线幂等），见 agentcore/memory/migrate.py。
    """
    return [
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
        "--exclude-table=schema_migrations",
        p["database"],
    ]


async def _backup_pg_dump(
    db_url: str,
    out: Path,
    tag: str,
    *,
    encryption: tuple[str, str] | None = None,
) -> dict | None:
    spec = find_pg_dump()
    if spec is None:
        return None
    prefix, desc = spec
    p = _dsn_parts(db_url)
    path = _backup_path(out, tag, ".sql.gz.age" if encryption else ".sql.gz")
    part = Path(f"{path}.part")
    part.touch(mode=0o600)  # 写入窗口内不落 0644；已存在时保持原 mode
    env = dict(os.environ)
    if p["password"]:
        env["PGPASSWORD"] = p["password"]
    rest = _pg_dump_rest_args(p)
    cmd, stdin_script = _container_script_wrap(prefix, p["password"], rest)
    # L14：stderr 落临时文件而不是 PIPE——无人读的 PIPE 塞满（>64KB）会永久挂起；
    # stdout 抽水整体包 asyncio.wait_for，pg_dump 卡死时能超时杀掉并把 stderr
    # 尾部带进异常消息。
    loop = asyncio.get_running_loop()
    with tempfile.TemporaryFile() as err_fh, tempfile.TemporaryFile() as age_err_fh:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=err_fh,
            env=env,
            stdin=subprocess.PIPE if stdin_script else None,
        )
        age_out: BinaryIO | None = None
        age_proc: subprocess.Popen | None = None
        if encryption is not None:
            age_bin, recipient = encryption
            age_out = open(part, "wb")
            try:
                # --recipient= 形式：recipient 由管理员配置，不能变成额外 argv
                age_proc = subprocess.Popen(
                    [age_bin, f"--recipient={recipient}"],
                    stdin=subprocess.PIPE,
                    stdout=age_out,
                    stderr=age_err_fh,
                )
            except Exception:
                proc.kill()
                proc.wait()
                age_out.close()
                part.unlink(missing_ok=True)
                raise

        def _abort() -> None:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            if age_proc is not None and age_proc.poll() is None:
                age_proc.kill()
                age_proc.wait()
            if age_out is not None and not age_out.closed:
                age_out.close()

        if stdin_script:
            # M7：注入脚本很小（<PIPE 缓冲），写完即关、不依赖 pg_dump 消费
            stdin = proc.stdin
            assert stdin is not None  # 由上面的 stdin= 参数保证
            try:
                stdin.write(stdin_script.encode("utf-8"))
                stdin.close()
            except (BrokenPipeError, OSError):
                logger.warning("backup: 容器 pg_dump stdin 注入失败（进程提前退出）")
                try:
                    stdin.close()
                except Exception:
                    pass
        try:
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        _pump_pg_dump_stdout,
                        proc,
                        part,
                        age_proc.stdin if age_proc is not None else None,
                    ),
                    timeout=_PG_DUMP_TIMEOUT,
                )
            except TimeoutError:
                _abort()
                raise RuntimeError(
                    f"pg_dump 超时（>{_PG_DUMP_TIMEOUT}s），已终止：{_stderr_tail(err_fh)}"
                ) from None
            if age_proc is not None:
                assert age_proc.stdin is not None
                with contextlib.suppress(OSError, ValueError):
                    age_proc.stdin.close()
                try:
                    age_code = age_proc.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    _abort()
                    raise RuntimeError("age 退出超时，已终止") from None
                if age_code != 0:
                    _abort()
                    raise RuntimeError(
                        f"age 加密失败（退出码 {age_code}）：{_stderr_tail(age_err_fh)}"
                    )
            try:
                returncode = proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                _abort()
                raise RuntimeError(
                    f"pg_dump 退出超时，已终止：{_stderr_tail(err_fh)}"
                ) from None
        except RuntimeError:
            _abort()
            part.unlink(missing_ok=True)
            raise
        except Exception:
            _abort()
            part.unlink(missing_ok=True)
            if encryption is not None:
                # 已承诺加密就绝不回退明文 JSONL；把 age/pg_dump 的 stderr 带出去
                raise RuntimeError(
                    "加密备份流写入失败："
                    f"{_stderr_tail(age_err_fh) or _stderr_tail(err_fh) or 'stream error'}"
                ) from None
            logger.exception("pg_dump 执行异常，回退 JSONL 导出")
            return None
        finally:
            if age_out is not None and not age_out.closed:
                age_out.close()
        if returncode != 0:
            part.unlink(missing_ok=True)
            raise RuntimeError(f"pg_dump 退出码 {returncode}：{_stderr_tail(err_fh)}")
    os.replace(part, path)  # M6：原子改名，绝不留半截文件顶着最终名
    _harden(path)
    _write_checksum(path)
    return {
        "path": str(path),
        "strategy": f"{desc} + age" if encryption else desc,
        "bytes": path.stat().st_size,
    }


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


_JSONL_BATCH_ROWS = 500  # 每批序列化行数：控制内存峰值，同时避免线程调度过碎


async def _iter_table_jsonl(conn, table: str, batch_rows: int = _JSONL_BATCH_ROWS):
    """按批产出该表的 JSONL 文本与行数：游标读取（不整表进内存）+ 线程内序列化。

    M（REVIEW-a604023..679c9b3）：原实现 `await conn.fetch("SELECT * FROM t")` 把整表
    读进内存再逐行 dumps——大表下既是内存峰值也是事件循环停摆来源。改为游标流式，
    每 `batch_rows` 行交给 to_thread 序列化一次。
    """
    batch: list = []
    async with conn.transaction():
        async for record in conn.cursor(f"SELECT * FROM {table}"):  # noqa: S608 — 表名为本模块常量
            batch.append(record)
            if len(batch) >= batch_rows:
                yield await asyncio.to_thread(_serialize_rows, table, batch), len(batch)
                batch = []
    if batch:
        yield await asyncio.to_thread(_serialize_rows, table, batch), len(batch)


async def _backup_jsonl(db_url: str, out: Path, tag: str) -> dict:
    import asyncpg

    path = _backup_path(out, tag, ".jsonl.gz")
    part = Path(f"{path}.part")
    part.touch(mode=0o600)
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
                    async for chunk, n in _iter_table_jsonl(conn, table):
                        fh.write(chunk)
                        rows_total += n
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
        if not (p.name.endswith(".gz") or p.name.endswith(".age")):
            continue  # .sha256 / .schema.json / .part 不算备份
        stat = p.stat()
        if p.name.endswith(".sql.gz.age"):
            kind = "pg_dump_encrypted"
        elif p.name.endswith(".sql.gz"):
            kind = "pg_dump"
        else:
            kind = "jsonl"
        items.append(
            {
                "path": str(p),
                "name": p.name,
                "bytes": stat.st_size,
                "mtime": stat.st_mtime,
                "kind": kind,
            }
        )
    return sorted(items, key=lambda i: i["mtime"], reverse=True)


def verify_backup(path: str | Path) -> dict:
    """全量校验一份备份（M6），返回 {"ok", "kind", "checksum", "tables"?, "error"?}。

    - .sql.gz：gzip 完整解压读完（截断/坏流判坏）；
    - .sql.gz.age：只能校验 age 魔数与 sha256（没有身份私钥无法验内部数据）；
    - .jsonl.gz：gzip 完整解压 + 全行 JSON 解析；
    - 存在 `<file>.sha256` sidecar 时校验摘要一致（mismatch 判坏）；
    - 无 sidecar 的旧备份只做全量可读性检查（checksum="missing"）。
    """
    p = Path(path)
    if p.name.endswith(".sql.gz.age"):
        kind = "pg_dump_encrypted"
    elif p.name.endswith(".sql.gz"):
        kind = "pg_dump"
    else:
        kind = "jsonl"
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
        elif kind == "pg_dump_encrypted":
            with open(p, "rb") as fh:
                head = fh.read(len(_AGE_MAGIC))
            if head != _AGE_MAGIC:
                error = "不是有效的 age 密文（缺少 age-encryption.org/v1 头）"
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
    """删除备份文件及其全部 sidecar；数据文件删掉才算成功。"""
    try:
        path.unlink()
    except OSError:
        logger.exception("prune backup failed: %s", path)
        return False
    for sidecar in (Path(f"{path}.sha256"), *_schema_sidecar_candidates(path)):
        try:
            sidecar.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.exception("prune sidecar failed: %s", sidecar)
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
    # 硬 kill 的 .part 残留不在 list_backups 结果里（只收 .gz），会永久滞留
    try:
        stale_parts = sorted(Path(out_dir).glob(f"{tag}-*.part"))
    except OSError:
        stale_parts = []
    for part in stale_parts:
        try:
            age_h = (time.time() - part.stat().st_mtime) / 3600
        except OSError:
            continue
        if age_h >= 6:
            try:
                part.unlink()
                removed.append(part.name)
                logger.warning("prune: 清理中断残留的 .part %s", part.name)
            except OSError:
                pass
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
    allow_newer_schema: bool = False,
) -> dict:
    """从备份恢复。.sql.gz 走 psql/pg_restore；.jsonl.gz 走逐表回灌。

    加密备份（.sql.gz.age）**不在这里解密**：私钥不应常驻 bot 进程。请先用
    ``age -d -i <私钥> <file> > <file.sql.gz>`` 解出明文，再恢复该 .sql.gz。

    ``schema_migrations`` 不在备份内；若备份 sidecar 记录的 schema 版本高于当前
    代码（用更新版本做的备份恢复进旧代码），默认拒绝——旧代码无法正确处理新表结构。
    """
    path = Path(dump_path)
    if not path.is_file():
        raise FileNotFoundError(f"备份文件不存在：{path}")
    if path.name.endswith(".sql.gz.age"):
        raise RuntimeError(
            "加密备份不能直接恢复：请先解密——"
            f"age -d -i <私钥文件> {path} > {path.name[:-4]}，"
            "再执行 restore <解出的 .sql.gz> --yes"
        )
    check = verify_backup(path)
    if not check.get("ok"):
        # 坏备份直接灌库 = 用投毒/截断数据覆盖生产（JSONL 幂等合并会让
        # 篡改行静默并入），fail-loud 而不是恢复出半个库
        raise RuntimeError(f"备份校验失败，拒绝恢复：{check.get('error', 'unknown')}")
    if check.get("checksum") == "missing":
        logger.warning(
            "restore: %s 无 sha256 sidecar，仅通过可读性校验（旧备份）", path.name
        )
    backup_version = _read_schema_sidecar(path)
    if backup_version:
        from agentcore.memory.migrations import latest_version

        code_version = latest_version()
        if code_version and backup_version > code_version and not allow_newer_schema:
            raise RuntimeError(
                f"备份的 schema 版本 {backup_version} 高于当前代码 {code_version}："
                "旧代码无法正确处理新结构。请升级代码后恢复；确认风险要强行恢复请加 "
                "--allow-newer-schema"
            )
    if path.name.endswith(".sql.gz"):
        return await asyncio.to_thread(_restore_sql, db_url, path, dry_run)
    return await _restore_jsonl(db_url, path, dry_run)


_CLEAR_MIGRATION_LEDGER_SQL = (
    "DO $$ BEGIN "
    "IF to_regclass('schema_migrations') IS NOT NULL THEN "
    "DELETE FROM schema_migrations; "
    "END IF; END $$;\n"
)


def _restore_sql(db_url: str, path: Path, dry_run: bool) -> dict:
    spec = None
    if shutil.which("psql"):
        spec = (["psql"], "local psql")
    elif shutil.which("docker"):
        # 与 find_pg_dump 同款探测：容器名错/容器里没有 psql 时立刻给出
        # 可行动的错误，而不是等到 SQL 喂到一半才以非零退出暴露
        container = os.getenv("PG_CONTAINER", "agent-demo-db-1")
        try:
            probe = subprocess.run(
                ["docker", "exec", container, "which", "psql"],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (subprocess.TimeoutExpired, OSError):
            probe = None
        if probe is not None and probe.returncode == 0:
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
    # 同样不能把 GzipFile 交给 subprocess（stdin 也走 fd）：流式解压边读边喂，
    # 整份 dump 不再全量进 RAM（GB 级库 = 直接 OOM）。
    # L19（REVIEW-de09478..workdir）：stdin/stdout/stderr 三PIPE 且没有并发读 →
    # 双管道死锁：psql 往 stdout 打「INSERT 0 1」（--inserts 格式 dump 每行都有），
    # 塞满 64KB 后阻塞写、于是不再消费 stdin，主线程的 stdin.write 跟着阻塞——
    # 直到 600s 超时（pre-restore 快照兜底，但整次恢复白等 10 分钟）。
    # 修法与 _backup_pg_dump 同源：stderr 落临时文件（无人读的 PIPE 必挂），
    # stdout 由主线程读完，stdin 交给 feeder 线程喂——两侧各有一个消费者。
    logger.info("restore: streaming SQL into %s", p["database"])
    with gzip.open(path, "rb") as src, tempfile.TemporaryFile() as err_fh:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=err_fh,
            env=env,
        )
        assert proc.stdin is not None
        feeder = threading.Thread(
            target=_feed_restore_stdin,
            args=(src, proc, _CLEAR_MIGRATION_LEDGER_SQL.encode()),
            daemon=True,
        )
        feeder.start()
        try:
            stdout = proc.stdout.read() if proc.stdout else b""
            feeder.join(timeout=30)
            code = proc.wait(timeout=600)
            err_tail = _stderr_tail(err_fh)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            proc.wait()
            err_tail = _stderr_tail(err_fh)
            raise RuntimeError(
                f"恢复超时（600s）：{exc}；stderr 尾部：{err_tail[:200]}"
            ) from exc
        finally:
            with contextlib.suppress(OSError, ValueError):
                proc.stdin.close()
    if code != 0:
        raise RuntimeError(f"恢复失败：{err_tail[:800]}")
    return {
        "strategy": desc,
        "restored": True,
        "bytes": path.stat().st_size,
        "stdout_tail": stdout[-200:].decode("utf-8", "replace"),
    }


def _feed_restore_stdin(src, proc, prefix: bytes = b"") -> None:
    r"""feeder 线程：把解压后的 SQL 流喂进 psql 的 stdin，喂完即关。

    ``prefix`` 在 dump 之前写入（恢复前清空迁移账本，见 ``_CLEAR_MIGRATION_LEDGER_SQL``）。

    过滤 pg17 pg_dump 生成、但旧版 psql 不认识的 ``SET transaction_timeout``
    （PG 17 新增会话参数；pg16 psql 恢复 pg17 dump 报
    ``unrecognized configuration parameter``——CI runner 的 pg_dump 与 service
    的 psql 版本漂移即触发，2026-10-21 CI 红）。该 SET 只影响会话超时、不
    影响数据，滤掉即可，避免整份恢复因一行会话设置失败。

    **只滤 COPY 块外的顶格 SET 行**：plain COPY 格式的数据行是原文（tab 分隔），
    字段内容任意位置含该子串——按子串滤会静默删数据行且 COPY 终止符完好、
    psql 不报错（REVIEW-1b16fa7..9c2d3c1 H1，已复现数据行丢失）。故用块状态机：
    ``COPY … FROM stdin;`` 到 ``\.`` 之间一行不滤，块外仅删 ``^SET transaction_timeout``。

    BrokenPipe/OSError 是 psql 早退（SQL 报错 ON_ERROR_STOP）的正常伴随——
    静默收场，错误信息由 stderr 临时文件里的尾部给出。
    """
    try:
        if prefix:
            proc.stdin.write(prefix)
        buf = b""
        in_copy = False
        while True:
            chunk = src.read(1 << 20)
            if not chunk:
                break
            buf += chunk
            if b"\n" not in buf:
                continue
            parts = buf.split(b"\n")
            buf = parts.pop()  # 末段可能不完整，留给下次
            kept = []
            for ln in parts:
                if in_copy:
                    kept.append(ln)  # COPY 块内是数据原文，一行不滤
                    if ln == b"\\.":
                        in_copy = False
                elif ln.startswith(b"SET transaction_timeout"):
                    continue  # 块外的顶格会话 SET 才滤
                else:
                    kept.append(ln)
                    if ln.startswith(b"COPY ") and ln.rstrip().endswith(b"FROM stdin;"):
                        in_copy = True
            if kept:
                proc.stdin.write(b"\n".join(kept) + b"\n")
        if buf and not in_copy and not buf.startswith(b"SET transaction_timeout"):
            proc.stdin.write(buf)
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        with contextlib.suppress(OSError, ValueError):
            proc.stdin.close()


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
    skipped: dict[str, int] = {}
    failures: dict[str, int] = {}
    try:
        async with conn.transaction():
            # M3：备份不含 schema_migrations；恢复到非空库时旧账本会保留且通常
            # 已是当前版本 → 启动期 migrate 认为"已到最新"而跳过，业务表却回到
            # 备份时的旧结构。清空账本让下次启动按迁移链幂等重建。
            await conn.execute(_CLEAR_MIGRATION_LEDGER_SQL)
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
                    if (
                        not isinstance(table, str)
                        or not table
                        or not _row_identifiers_safe(table, rec.get("row"))
                    ):
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
                            status = await conn.execute(sql, *[row[c] for c in cols])
                        # INSERT 0 0 = ON CONFLICT 跳过：计入 restored 会虚报
                        # 「恢复了 N 行」（重复恢复同一备份时数字全部失真）
                        if str(status).rsplit(" ", 1)[-1] == "1":
                            restored[table] += 1
                        else:
                            skipped[table] = skipped.get(table, 0) + 1
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
    return {
        "strategy": "asyncpg jsonl",
        "restored": restored,
        "skipped": skipped,
        "failures": failures,
    }
