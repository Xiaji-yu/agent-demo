"""工作区文件系统：单一共享目录，所有路径锁定在 <root>/ 内（防穿越）。

安全边界（H1）：git 元数据（``.git``、``.gitattributes``、``.gitmodules``）可驱动
白名单命令执行外部程序，创建/修改/删除类操作一律拒绝；读取不受限。
另外工作区根下的 ``SOUL.md`` / ``personas`` / ``cron`` 是**保留名**（人格与定时
任务配置的历史落点，2026-10-29 起迁出），写入/建目录/删除一律拒绝——见
``_RESERVED_ROOT_NAMES``；读取照旧（排障与"确认回迁没发生"要用）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_SNIFF_BYTES = 8000
_MAX_READ_BYTES = 2 * 1024 * 1024  # 文本最多读 2MB 参与截断，避免全量读大文件
# 仅剔除真正的控制字符（保留 \t \n \r）
_CONTROL_RE = re.compile(r"[\x00-\x08\x0e-\x1f\x7f]")

# 用户可见结果标记：生产与测试共用，避免断言绑死在零散字面量上（文案改动只需改这里）
MSG_WRITTEN = "已写入"
MSG_DELETED_FILE = "已删除文件"
MSG_DELETED_DIR = "已删除空目录"
MSG_DIR_READY = "目录就绪"
MSG_OUT_OF_ROOT = "路径超出工作区，已拒绝"
MSG_GIT_PROTECTED = (
    "禁止修改 git 内部文件（.git、.gitattributes、.gitmodules 由沙箱保护，只读）"
)
# 人格 / 定时任务配置的**历史落点**（2026-10-29 起迁出工作区）：这些名字在
# 工作区根下保留。不停的原因：允许 LLM 在工作区里造一份 `SOUL.md` / `cron/jobs.json`
# 只会在磁盘上留一份永不生效的假配置，让人误以为"注入成功"或"修复失败"；
# 而真正生效的那份在下次迭代里就不该被 fs_* 触达。比较用小写：Windows/mac 的
# 文件系统大小写不敏感，`soul.md` 与 `SOUL.md` 是同一个文件。
_RESERVED_ROOT_NAMES = frozenset({"soul.md", "personas", "cron"})
MSG_RESERVED_NAME = "该路径是保留名（人格与定时任务配置不在工作区，走 web 人格页 / 主机上的 jobs.json），已拒绝"


def reserved_root(rel: Path) -> bool:
    """相对路径是否落在保留名根下。

    独立的写面（如 ``extract`` 技能自己用 ``os.open`` 落盘）不走
    :func:`_assert_writable`，必须复用这个判定，否则同一份「假配置」还能从
    解压通道写进工作区（2026-10-29 评审发现）。
    """
    if not rel.parts:
        return False
    # Win32 建文件时会把名字尾部的点与空格剥掉（``cron.`` → ``cron``），而本函数
    # 在目标**不存在**时只看到词法形态：``resolve()`` 对不存在的路径只做归一、
    # 不碰尾部点，于是 ``cron./jobs.json`` / ``SOUL.md.`` / ``personas./x`` 一路
    # 绿灯，落盘后却正是保留名本体（2026-11-02 实跑复现）。目标已存在时
    # realpath 会归一到 canonical、后续即被拒——缺口只在**首写**那一瞬间。
    # 统一先剥尾部点与空格再比对：Linux/macOS 不剥，多剥一次只是让近似名也
    # fail-closed，方向安全。
    return rel.parts[0].rstrip(" .").casefold() in _RESERVED_ROOT_NAMES


# H1（根治写入面）：这些路径能驱动 git 执行外部命令——``.gitattributes`` 的
# ``filter=``/``diff=`` 属性配合 repo 配置里的驱动定义，可让白名单内的 ``git diff``
# 执行任意命令；``.git`` 内部（含 worktree 场景下 ``.git`` 指针文件本身）承载这些配置。
# 创建/修改/删除一律拒绝（读不受限，fs_read/fs_list 仍可查看）。
_GIT_PROTECTED_PARTS = {".git", ".gitattributes", ".gitmodules"}


def _assert_writable(p: Path, root: Path) -> None:
    """创建/修改/删除类操作的目标不得是 git 元数据或落在 ``.git`` 内部。

    按路径**组件**判定（而非仅看最终文件名）：``.git/hooks/pre-commit``、
    嵌套仓库的 ``vendor/lib/.git/config``、以及落入名为 ``.gitattributes``
    的目录内部同样被拒（fail-closed）。p 必须已在 root 内（越界由 resolve 拒绝）。
    """
    try:
        rel = p.relative_to(root)
    except ValueError:
        return
    if reserved_root(rel):
        raise ValueError(MSG_RESERVED_NAME)
    if any(part in _GIT_PROTECTED_PARTS for part in rel.parts):
        raise ValueError(MSG_GIT_PROTECTED)


def _looks_binary(data: bytes) -> bool:
    if not data:
        return False
    if b"\x00" in data:
        return True
    sample = data[:_SNIFF_BYTES]
    weird = sum(1 for b in sample if b < 9 or (13 < b < 32) or b == 127)
    return weird / len(sample) > 0.1


class WorkspaceFS:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def resolve(self, rel: str) -> Path:
        """把相对路径解析到工作区根目录内的绝对路径；越界抛 ValueError。"""
        rel = (rel or ".").strip()
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise ValueError(MSG_OUT_OF_ROOT)
        return p

    async def list(self, rel: str = ".") -> str:
        p = self.resolve(rel)
        if not p.exists():
            return f"路径不存在：{rel}"
        if p.is_file():
            return rel
        entries = sorted(p.iterdir(), key=lambda x: x.name)
        if not entries:
            return "(空目录)"
        lines = []
        for e in entries:
            kind = "DIR " if e.is_dir() else "FILE"
            size = e.stat().st_size if e.is_file() else 0
            lines.append(f"{kind} {size:>9}  {e.name}")
        return "\n".join(lines)

    async def read(self, rel: str, max_chars: int = 6000) -> str:
        p = self.resolve(rel)
        if not p.is_file():
            return f"文件不存在或不是文件：{rel}"
        # 同步 IO 放线程池，避免大文件读阻塞事件循环（所有会话共用一个 loop）
        return await asyncio.to_thread(self._read_sync, p, max_chars)

    def _read_sync(self, p: Path, max_chars: int) -> str:
        try:
            size = p.stat().st_size
            with open(p, "rb") as f:
                head = f.read(_SNIFF_BYTES)
                if _looks_binary(head):
                    return f"(二进制文件，{size} 字节，未显示内容)"
                data = head + f.read(min(size, _MAX_READ_BYTES) - len(head))
        except OSError as e:
            return f"(读取失败：{e.strerror or e})"
        text = _CONTROL_RE.sub("", data.decode("utf-8", errors="replace"))
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n…（文件过大，仅显示前 {max_chars} 字符）"
        return text

    async def write(self, rel: str, content: str) -> str:
        p = self.resolve(rel)
        _assert_writable(p, self.root)
        if p.is_dir():
            return f"目标是目录：{rel}"
        p.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._write_sync, p, content or "")
        return f"{MSG_WRITTEN} {rel}（{p.stat().st_size} 字节）"

    @staticmethod
    def _write_sync(p: Path, content: str) -> None:
        # 原子写：并发读不会看到截断文件；中途失败必须清掉 .part 残骸
        # （否则 fs_list 会看到一个永远写不完的半成品），权限 0600——
        # 工作区是用户私有内容，不该默认全局可读。
        # M2（REVIEW-de09478..workdir）：**创建即 0600**（os.open 显式 mode）：
        # 旧实现 write_text 后 chmod，umask 022 下 chmod 之前 .part 是 0o644，
        # 崩溃/被杀即留下全局可读的中间产物（对照 config_write.atomic_write）。
        tmp = p.with_name(p.name + ".part")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, p)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    async def mkdir(self, rel: str) -> str:
        p = self.resolve(rel)
        _assert_writable(p, self.root)
        p.mkdir(parents=True, exist_ok=True)
        return f"{MSG_DIR_READY}：{rel}"

    async def delete(self, rel: str) -> str:
        """删除单文件或空目录；非空目录拒绝（防误删）。"""
        p = self.resolve(rel)
        _assert_writable(p, self.root)
        return await self._delete_path(p, rel)

    async def delete_abs(self, abs_path: str | Path) -> str:
        """按绝对路径删除（二次确认用）；再次校验必须在工作区内。"""
        p = Path(abs_path).resolve()
        if p != self.root and self.root not in p.parents:
            raise ValueError(MSG_OUT_OF_ROOT)
        return await self._delete_path(p, str(p))

    async def _delete_path(self, p: Path, label: str) -> str:
        _assert_writable(p, self.root)  # delete_abs 等旁路入口同样受限（纵深）
        if not p.exists():
            return f"路径不存在：{label}"
        if p.is_dir():
            if any(p.iterdir()):
                return f"目录非空，请先删除其中内容（安全限制）：{label}"
            p.rmdir()
            return f"{MSG_DELETED_DIR}：{label}"
        p.unlink()
        return f"{MSG_DELETED_FILE}：{label}"
