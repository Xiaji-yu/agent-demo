"""数据库迁移注册表。

约定（``validate_registry`` 在 import 与测试期强制）：

- 版本号是四位零填充字符串（``0001``/``0002``），字典序 = 应用顺序；
- 版本连续无空洞——删掉历史迁移会让老库与代码对不上，必须用新迁移修正；
- 目录里每个 ``*.sql`` 都必须在 ``MIGRATIONS`` 注册：新增文件忘登记时
  fail-closed，而不是让 schema 静默漂移；
- **已发布的迁移文件不可修改**：runner 记录 SHA-256，改了会在启动时拒绝运行。

``destructive`` 只在注册表里声明（不靠文件名推断）。破坏性迁移默认不会在
启动时自动执行，必须运维用 ``scripts/migrate.py up --allow-destructive --yes``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent
_FILE_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    path: Path
    destructive: bool = False


def _migration(version: str, name: str, *, destructive: bool = False) -> Migration:
    return Migration(
        version=version,
        name=name,
        path=MIGRATIONS_DIR / f"{version}_{name}.sql",
        destructive=destructive,
    )


MIGRATIONS: tuple[Migration, ...] = (_migration("0001", "baseline"),)


def latest_version(migrations: tuple[Migration, ...] = MIGRATIONS) -> str | None:
    return migrations[-1].version if migrations else None


def validate_registry(
    migrations: tuple[Migration, ...] = MIGRATIONS,
    directory: Path | None = MIGRATIONS_DIR,
) -> None:
    """校验注册表与目录。抛 ``ValueError``（调用方包成 MigrationError）。

    ``directory=None`` 跳过"目录里是否有未注册 .sql"的扫描（测试传入临时
    迁移集时使用）；生产注册表在 import 时已按默认目录校验过。
    """
    versions = [m.version for m in migrations]
    if not versions:
        raise ValueError("migration registry is empty")
    if len(set(versions)) != len(versions):
        raise ValueError(f"duplicate migration versions: {versions}")
    if versions != sorted(versions):
        raise ValueError(f"migrations must be declared in ascending order: {versions}")
    expected = [f"{i:04d}" for i in range(1, len(versions) + 1)]
    if versions != expected:
        raise ValueError(f"migration versions must be contiguous 0001..: {versions}")

    registered: set[Path] = set()
    for m in migrations:
        if not m.version.isdigit() or len(m.version) != 4:
            raise ValueError(f"bad migration version {m.version!r} (want 4 digits)")
        match = _FILE_RE.match(m.path.name)
        if not match or match.group(1) != m.version:
            raise ValueError(
                f"migration file name does not match its version: {m.path.name}"
            )
        if not m.path.is_file():
            raise ValueError(f"migration file missing: {m.path}")
        if m.path.stat().st_size == 0:
            raise ValueError(f"migration file is empty: {m.path}")
        registered.add(m.path.resolve())

    if directory is not None and directory.is_dir():
        stray = sorted(
            p.name for p in directory.glob("*.sql") if p.resolve() not in registered
        )
        if stray:
            raise ValueError(
                "unregistered migration file(s): "
                + ", ".join(stray)
                + " — add them to MIGRATIONS (and never edit applied ones)"
            )


validate_registry()
