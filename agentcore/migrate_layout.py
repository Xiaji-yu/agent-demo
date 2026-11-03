"""旧布局一次性迁移：``data/workspace/{cron,personas,SOUL.md}`` → ``data/{cron,personas}``。

**为什么搬**：``data/workspace`` 是 LLM 沙箱的可写区（``fs_write`` 能落盘），
定时任务与人格放在里面等于把「配置」交给一条提示注入就能改的面。2026-10-29 起
两者搬到沙箱之外的 ``data/cron`` / ``data/personas``，``WorkspaceFS`` 同步把
``cron``/``personas``/``SOUL.md`` 列为保留名拒写（见 ``workspace/fs.py``）。

**为什么是复制不是搬**：迁移失败不能影响启动，用户回滚版本时也不能发现自己的
人格与定时任务原地消失。旧文件原样保留，读的时候旧位作为回落（见
``personas/manager.py`` 的 ``_persona_bases`` / ``jobs.load`` 的内嵌 state 回落）。

**幂等**：每次启动都跑；目标已存在就跳过，所以重复调用无副作用，也不会覆盖用户
在新位置手改过的内容。
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

# 旧布局里需要迁移的三类东西（相对 workspace）
_LEGACY_CRON_DIR = "cron"
_LEGACY_PERSONAS_DIR = "personas"
_LEGACY_SOUL = "SOUL.md"
# jobs.json 同目录的状态 sidecar（旧版本内嵌在 jobs.json 里，没有这个文件）
_STATE_FILENAME = "state.json"


def _chmod_600(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Windows 上 chmod 只改只读位；权限位语义不成立，别让迁移因此失败
        pass


def _copy_file(src: Path, dst: Path, moved: list[str]) -> None:
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        # C5（REVIEW 2026-11-02）：copy2 继承源文件模式，源是 0644 则落盘就是
        # 0644——人格 SOUL.md / jobs.json 对同机其它本地用户可读。改成先按 0600
        # 创建目标、再把内容拷贝进去，模式在创建时写死而不是事后补 chmod。
        fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            # Windows 无 POSIX 权限语义（只读位除外）
            pass
        with os.fdopen(fd, "wb") as out:
            with open(src, "rb") as inp:
                shutil.copyfileobj(inp, out)
        shutil.copystat(src, dst)
        moved.append(str(dst))
    except OSError as e:
        logger.warning("旧布局迁移失败（文件 %s → %s）：%s", src, dst, e)


def _copy_dir(src: Path, dst: Path, moved: list[str]) -> None:
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst)
        for f in dst.rglob("*"):
            if f.is_file():
                _chmod_600(f)
        moved.append(str(dst))
    except OSError as e:
        logger.warning("旧布局迁移失败（目录 %s → %s）：%s", src, dst, e)


def migrate_legacy_layout(
    *,
    workspace: Path | None = None,
    cron_dir: Path | None = None,
    personas_dir: Path | None = None,
) -> list[str]:
    """把旧位内容复制到新位；返回本次实际写入的路径列表（测试/日志用）。

    参数只为测试注入；生产路径从 :func:`_cron_dir` / :func:`_personas_dir` /
    ``WORKSPACE_DIR`` 解析，与运行期读取同源，不引入第二套配置。
    """
    ws = workspace or Path(os.getenv("WORKSPACE_DIR") or "data/workspace")
    cron_target = cron_dir or _cron_dir()
    personas_target = personas_dir or _personas_dir()
    moved: list[str] = []

    # 1) 定时任务：jobs.json +（新版才有的）state.json
    legacy_cron = ws / _LEGACY_CRON_DIR
    if legacy_cron.resolve() != cron_target.resolve():
        for name in ("jobs.json", _STATE_FILENAME):
            src = legacy_cron / name
            dst = cron_target / name
            if src.is_file() and not dst.exists():
                _copy_file(src, dst, moved)

    # 2) 默认人格：根级 SOUL.md，3) 可切换人格：每个 personas/<name>/ 目录。
    # 若 ``PERSONAS_DIR`` 仍指着老的 workspace 布局，写面就是旧位本身——此时
    # 「迁移」源与目标同一处，跳过整段（否则会把 ws/SOUL.md 拷进 ws/personas/ 造出
    # 一个没人读的副本）。
    legacy_personas = ws / _LEGACY_PERSONAS_DIR
    if personas_target.resolve() != legacy_personas.resolve():
        legacy_soul = ws / _LEGACY_SOUL
        soul_target = personas_target / _LEGACY_SOUL
        if legacy_soul.is_file() and not soul_target.exists():
            _copy_file(legacy_soul, soul_target, moved)
        if legacy_personas.is_dir():
            for child in sorted(legacy_personas.iterdir()):
                if not child.is_dir():
                    continue
                dst = personas_target / child.name
                if dst.exists():
                    continue
                _copy_dir(child, dst, moved)

    if moved:
        logger.info("旧布局已复制到新位置：%d 项（旧文件保留）", len(moved))
    return moved


def _cron_dir() -> Path:
    from agentcore.scheduler.jobs import cron_dir

    return cron_dir()


def _personas_dir() -> Path:
    from agentcore.personas.manager import PersonaManager

    return PersonaManager().dir
