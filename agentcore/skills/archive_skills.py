"""压缩包技能（extract / pack_zip）：Python 标准库实现，取代 run_command 白名单
里的外部 zip/unzip/tar（REVIEW-6745556..d730827 H1 决策，方案 B）。

为什么不用外部工具：历轮 4 个 H（zip -T/-TT、tar 扫描超限、--strip-components /
unzip -j 展平、通配符 archive 名）全部出生在「Python 侧预扫描 vs 外部工具真实
解压行为」的语义鸿沟里。标准库 zipfile/tarfile 扫描与解压**同一实现同一数据**，
不存在条目表失配，也没有 flag/glob 注入面。

安全设计（对齐「解压是 LLM 内容获得执行位的唯一转化器」的威胁模型）：
- **两遍法**：先全量校验条目（名/类型/大小累计），零落盘；全部通过后才第二遍
  写盘——坏条目/超限发生在任何字节落盘之前，无需事后清理现场。
- **执行位从根上消失**：文件一律 0600、目录一律 0700 落盘（不看包内权限位）。
  「构建脚本链」的最后一块（0777 条目存活）不可表达。
- 拒绝条目：``..`` 穿越 / 绝对路径 / 盘符与 UNC / ``.git*`` 配置段（写入面
  禁止触及 git 配置的纪律对解压同样生效）；tar 侧拒符号链接/硬链接/设备/FIFO
  条目，zip 侧拒 Unix symlink 条目。
- 上限（zip bomb 防护，env 可调防脏值）：条目数 AGENT_EXTRACT_MAX_ENTRIES、
  解压总字节 AGENT_EXTRACT_MAX_MB。zip 按集中目录声明校验并在读入时复核实际
  字节（防谎报 file_size）；tar 的 size 即流式数据块长度，累计即真值。
- 输出（含不可信的条目名回显）一律过 safety 围栏。

信任模型与 run_command 相同：仅管理员（superuser），medium+（写入面，与
fs_write 同档），注册期过滤 + handler 双层复查。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tarfile
import time
import zipfile
from pathlib import Path, PurePosixPath

from agentcore.safety import fence_untrusted
from agentcore.skills.action_skills import _env_int
from agentcore.skills.levels import at_least
from agentcore.workspace.utils import is_superuser, workspace_root

logger = logging.getLogger(__name__)

MSG_REJECTED = "拒绝"

_LOW_WRITE_DENIED = (
    "当前权限级别为 low：{}需要 medium 及以上（AGENT_PERMISSION_LEVEL，改后重启生效）。"
)

# git 配置段禁入（与原 runner._UNZIP_ENTRY_FORBIDDEN 同口径）
_ENTRY_FORBIDDEN_PARTS = {".git", ".gitattributes", ".gitmodules"}

# 条目名/类型拒绝的原因文案统一在预扫描阶段给出（写盘零开始）
_MAX_LIST_PREVIEW = 20

_AUDIT_LOG_MAX = 200  # 审计日志里 archive/dest 参数的最长展示长度


def _max_entries() -> int:
    return _env_int("AGENT_EXTRACT_MAX_ENTRIES", 20000, 1, 1_000_000)


def _max_total_bytes() -> int:
    return _env_int("AGENT_EXTRACT_MAX_MB", 1024, 1, 102_400) * 1024 * 1024


def _entry_problem(name: str) -> str | None:
    """压缩包条目名静态校验：返回拒绝原因，None 表示可落盘。

    与原 runner._zip_scan/_tar_scan 同口径：``/`` 与 ``\\`` 都按分隔符拆段
    （宁可误拒包内带反斜杠的怪名——来源不可信，保守无害）。
    """
    if not name or name.strip() == "":
        return "存在空条目名"
    parts = PurePosixPath(name.replace("\\", "/")).parts
    if not parts:
        return f"条目名无法解析：{name[:60]!r}"
    if name.startswith(("/", "\\")) or (len(name) > 1 and name[1] == ":"):
        return f"压缩包含绝对路径条目：{name[:60]!r}"
    for part in parts:
        if part == "..":
            return f"压缩包含 .. 穿越条目：{name[:60]!r}"
        if part in _ENTRY_FORBIDDEN_PARTS:
            return f"压缩包含 git 配置条目（写入面禁止触及）：{name[:60]!r}"
    return None


def _relative_target(name: str) -> PurePosixPath:
    """归一化条目名为包内相对路径（预扫描已保证无穿越/绝对形态）。"""
    return PurePosixPath(name.replace("\\", "/"))


def _resolve_dest(root: Path, archive_rel: str, dest: str) -> tuple[Path | None, str]:
    """解析解压目标目录：工作区内子目录，默认 ``<包名>_extracted``。"""
    stem = Path(archive_rel).stem or "archive"
    if stem.endswith(".tar"):  # p.tar.gz → p（去掉压缩后缀后 tar 的 stem 是 p.tar）
        stem = stem[:-4]
    rel = dest.strip() or f"{stem}_extracted"
    p = (root / rel).resolve()
    if p == root or root not in p.parents:
        return None, "解压目标必须落在工作区子目录内（不能是工作区根）"
    return p, ""


def _scan_zip(path: Path) -> tuple[str | None, list[zipfile.ZipInfo], int]:
    """第一遍：zip 全量条目校验（名/类型/总大小）。拒绝原因或 (None, 条目, 总字节)。"""
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
    except Exception as e:
        return f"不是可识别的 zip 包（{type(e).__name__}）", [], 0
    if len(infos) > _max_entries():
        return (
            f"条目数 {len(infos)} 超过上限 {_max_entries()}（防解压炸弹），已拒绝",
            [],
            0,
        )
    total = 0
    for info in infos:
        reason = _entry_problem(info.filename)
        if reason:
            return reason, [], 0
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            return f"压缩包含符号链接条目：{info.filename[:60]!r}", [], 0
        total += info.file_size
        if total > _max_total_bytes():
            return (
                f"解压总大小超过上限 {_max_total_bytes() // (1024 * 1024)} MB"
                "（防解压炸弹），已拒绝",
                [],
                0,
            )
    return None, infos, total


def _scan_tar(path: Path) -> tuple[str | None, list[tarfile.TarInfo], int]:
    """第一遍：tar 系（gz/bz2/xz 自动识别）全量条目校验。"""
    try:
        with tarfile.open(path, "r:*") as tf:
            members = tf.getmembers()
    except Exception as e:
        return f"不是可识别的 tar 包（{type(e).__name__}）", [], 0
    if len(members) > _max_entries():
        return (
            f"条目数 {len(members)} 超过上限 {_max_entries()}（防解压炸弹），已拒绝",
            [],
            0,
        )
    total = 0
    for m in members:
        reason = _entry_problem(m.name)
        if reason:
            return reason, [], 0
        if not (m.isdir() or m.isreg()):
            return (
                f"压缩包含非常规条目（链接/设备/FIFO）：{m.name[:60]!r}",
                [],
                0,
            )
        total += m.size
        if total > _max_total_bytes():
            return (
                f"解压总大小超过上限 {_max_total_bytes() // (1024 * 1024)} MB"
                "（防解压炸弹），已拒绝",
                [],
                0,
            )
    return None, members, total


def _extract_sync(root: Path, archive_path: Path, dest: Path) -> tuple[str, int, int]:
    """第二遍写盘：文件 0600、目录 0700。返回（摘要, 文件数, 目录数）。

    预扫描已保证条目安全；zip 读入时复核实际字节（谎报 file_size 的包在此
    被截获并抛错，已落盘部分均为 0600 无害内容）。
    """
    files = dirs = 0
    if zipfile.is_zipfile(archive_path):
        with zipfile.ZipFile(archive_path) as zf:
            infos = zf.infolist()
            remaining = _max_total_bytes()
            for info in infos:
                target = dest / _relative_target(info.filename)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    dirs += 1
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.parent.is_dir():
                    raise OSError(f"父目录创建失败：{target.parent}")
                with zf.open(info) as src, open(target, "wb") as out:
                    copied = 0
                    while chunk := src.read(min(1 << 20, remaining + 1)):
                        copied += len(chunk)
                        if copied > info.file_size:
                            # 实际字节超过声明：包不可信，立即中止
                            raise OSError(
                                f"条目 {info.filename[:60]!r} 实际数据超过声明大小"
                            )
                        remaining -= len(chunk)
                        if remaining < 0:
                            raise OSError("解压总大小超过上限（实际读入复核）")
                        out.write(chunk)
                os.chmod(target, 0o600)
                files += 1
    else:
        with tarfile.open(archive_path, "r:*") as tf:
            for m in tf.getmembers():
                target = dest / _relative_target(m.name)
                if m.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    dirs += 1
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(m)
                if src is None:  # 预扫描已排除，防御性兜底
                    raise OSError(f"条目无法读取：{m.name[:60]!r}")
                with open(target, "wb") as out:
                    shutil.copyfileobj(src, out, 1 << 20)
                os.chmod(target, 0o600)
                files += 1
    # 目录权限统一收敛（含隐式创建的中间父目录）：0700——写盘期间目录 0755
    # 不构成威胁（内容全为 0600），收敛放在写完之后一次完成
    dest.chmod(0o700)
    for p in dest.rglob("*"):
        if p.is_dir():
            os.chmod(p, 0o700)
    return f"解压完成：{files} 个文件 / {dirs} 个目录", files, dirs


def register_archive_skills(registry) -> None:
    """注册压缩包技能（仅 superuser；写入面 medium+，low 档 schema 不可见）。"""
    if not at_least("medium"):
        return

    @registry.register(
        "extract",
        "把服务器工作区内的 zip / tar(.gz/.bz2/.xz) 压缩包解压到工作区子目录"
        "（仅管理员）。逐条目安全校验（拒穿越/绝对路径/git 配置/链接条目，"
        "条目数与总大小设上限），解压产物一律 0600/0700（不含执行位——"
        "需要执行请让管理员手工 chmod）。",
        {
            "type": "object",
            "properties": {
                "archive": {
                    "type": "string",
                    "description": "压缩包的相对路径，如 dist/pkg.tgz",
                },
                "dest": {
                    "type": "string",
                    "description": "解压目标子目录（相对路径），默认 <包名>_extracted",
                },
            },
            "required": ["archive"],
        },
        permission="superuser",
    )
    async def extract_skill(archive: str, dest: str = "", user_id: str = "") -> str:
        if not at_least("medium"):
            return _LOW_WRITE_DENIED.format("解压")
        if not is_superuser(user_id):
            return "无权限：仅管理员可解压文件。"
        rel = (archive or "").strip()
        if not rel:
            return f"{MSG_REJECTED}：缺少压缩包路径"
        root = workspace_root()
        try:
            src = (root / rel).resolve()
        except OSError:
            return f"{MSG_REJECTED}：路径无法解析"
        if src == root or root not in src.parents:
            return f"{MSG_REJECTED}：压缩包必须位于工作区内"
        if not src.is_file():
            return f"找不到压缩包：{rel}"
        target, reason = _resolve_dest(root, rel, dest)
        if target is None:
            return f"{MSG_REJECTED}：{reason}"
        logger.info(
            "extract: uid=%s archive=%r dest=%s",
            user_id,
            rel[:_AUDIT_LOG_MAX],
            target.name,
        )
        started = time.monotonic()
        try:
            problem, _items, _total = await asyncio.to_thread(_scan_dispatch, src)
            if problem:
                logger.warning("extract refused: uid=%s %s", user_id, problem)
                return f"{MSG_REJECTED}：{problem}"
            summary, files, dirs = await asyncio.to_thread(
                _extract_sync, root, src, target
            )
        except Exception as e:
            logger.exception("extract failed: uid=%s", user_id)
            return f"(解压失败：{type(e).__name__}，已落盘部分为 0600 无害内容)"
        elapsed = time.monotonic() - started
        listing = sorted(p.name for p in target.rglob("*"))[:_MAX_LIST_PREVIEW]
        more = "" if files + dirs <= _MAX_LIST_PREVIEW else "\n…（仅显示前 20 项）"
        body = "\n".join(listing) if listing else "(空包)"
        text = (
            f"{summary}\n目标：{target.name}（耗时 {elapsed:.1f}s）\n内容预览：\n"
            + body
            + more
        )
        # 条目名来自不可信压缩包，回显必须过围栏
        return fence_untrusted("解压结果", text, "压缩包条目名")

    @registry.register(
        "pack_zip",
        "把服务器工作区内的一个目录打包成 zip（仅管理员）。不跟随符号链接；"
        "包内条目一律记录为 0600。",
        {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "description": "要打包的工作区目录（相对路径，默认 .）",
                },
                "name": {
                    "type": "string",
                    "description": "输出的 zip 文件名（如 bundle.zip），落在工作区根",
                },
            },
            "required": ["name"],
        },
        permission="superuser",
    )
    async def pack_zip_skill(
        source: str = ".", name: str = "", user_id: str = ""
    ) -> str:
        if not at_least("medium"):
            return _LOW_WRITE_DENIED.format("打包")
        if not is_superuser(user_id):
            return "无权限：仅管理员可打包文件。"
        out_name = (name or "").strip()
        if not out_name or "/" in out_name or "\\" in out_name or ".." in out_name:
            return f"{MSG_REJECTED}：name 必须是工作区根下的单纯文件名（如 bundle.zip）"
        if not out_name.lower().endswith(".zip"):
            out_name += ".zip"
        root = workspace_root()
        try:
            src = (root / (source or ".")).resolve()
            out = (root / out_name).resolve()
        except OSError:
            return f"{MSG_REJECTED}：路径无法解析"
        if src != root and root not in src.parents:
            return f"{MSG_REJECTED}：打包目录必须位于工作区内"
        if out == root or root not in out.parents:
            return f"{MSG_REJECTED}：输出路径必须落在工作区内"
        if out == src:
            return f"{MSG_REJECTED}：输出不能与打包目录相同"
        if not src.is_dir():
            return f"找不到目录：{source}"
        logger.info(
            "pack_zip: uid=%s source=%r name=%r",
            user_id,
            source[:_AUDIT_LOG_MAX],
            out_name[:_AUDIT_LOG_MAX],
        )
        try:
            summary, count = await asyncio.to_thread(_pack_sync, src, out, root)
        except Exception as e:
            logger.exception("pack_zip failed: uid=%s", user_id)
            return f"(打包失败：{type(e).__name__})"
        return (
            fence_untrusted("打包结果", summary, "工作区文件名") if count else summary
        )


def _scan_dispatch(src: Path) -> tuple[str | None, list, int]:
    if zipfile.is_zipfile(src):
        return _scan_zip(src)
    return _scan_tar(src)


def _pack_sync(src: Path, out: Path, root: Path) -> tuple[str, int]:
    """打包 source 目录为 zip：拒 symlink、条目权限位固定 0600、总量受限。"""
    limit = _max_total_bytes()
    total = 0
    count = 0
    skipped_links = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
            dirnames[:] = [d for d in dirnames if not (Path(dirpath) / d).is_symlink()]
            for fn in filenames:
                fp = Path(dirpath) / fn
                if fp.is_symlink():
                    skipped_links += 1
                    continue
                size = fp.stat().st_size
                if total + size > limit:
                    raise OSError("打包总大小超过上限（防解压炸弹）")
                total += size
                arc = PurePosixPath(fp.relative_to(src).as_posix())
                # 条目时间戳用 UTC（ZipInfo 只是归档元数据，不是显示层时间；
                # 显示层时钟纪律见 tests/test_repo_guards.py 的 localtime 守卫）
                zi = zipfile.ZipInfo(str(arc), date_time=time.gmtime()[:6])
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.external_attr = (0o100600) << 16
                with open(fp, "rb") as f:
                    zf.writestr(zi, f.read())
                count += 1
    note = f"（跳过 {skipped_links} 个符号链接）" if skipped_links else ""
    return f"打包完成：{count} 个文件 -> {out.name}{note}", count
