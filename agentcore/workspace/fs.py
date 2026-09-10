"""工作区文件系统：单一共享目录，所有路径锁定在 <root>/ 内（防穿越）。"""
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
        if p.is_dir():
            return f"目标是目录：{rel}"
        p.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._write_sync, p, content or "")
        return f"{MSG_WRITTEN} {rel}（{p.stat().st_size} 字节）"

    @staticmethod
    def _write_sync(p: Path, content: str) -> None:
        # 原子写：并发读不会看到截断文件
        tmp = p.with_name(p.name + ".part")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, p)

    async def mkdir(self, rel: str) -> str:
        p = self.resolve(rel)
        p.mkdir(parents=True, exist_ok=True)
        return f"{MSG_DIR_READY}：{rel}"

    async def delete(self, rel: str) -> str:
        """删除单文件或空目录；非空目录拒绝（防误删）。"""
        p = self.resolve(rel)
        return await self._delete_path(p, rel)

    async def delete_abs(self, abs_path: str | Path) -> str:
        """按绝对路径删除（二次确认用）；再次校验必须在工作区内。"""
        p = Path(abs_path).resolve()
        if p != self.root and self.root not in p.parents:
            raise ValueError(MSG_OUT_OF_ROOT)
        return await self._delete_path(p, str(p))

    async def _delete_path(self, p: Path, label: str) -> str:
        if not p.exists():
            return f"路径不存在：{label}"
        if p.is_dir():
            if any(p.iterdir()):
                return f"目录非空，请先删除其中内容（安全限制）：{label}"
            p.rmdir()
            return f"{MSG_DELETED_DIR}：{label}"
        p.unlink()
        return f"{MSG_DELETED_FILE}：{label}"
