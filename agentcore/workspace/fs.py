"""工作区文件系统：单一共享目录，所有路径锁定在 <root>/ 内（防穿越）。"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class WorkspaceFS:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def resolve(self, rel: str) -> Path:
        """把相对路径解析到工作区根目录内的绝对路径；越界抛 ValueError。"""
        rel = (rel or ".").strip()
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise ValueError("路径超出工作区，已拒绝")
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
        text = p.read_text(encoding="utf-8", errors="replace")
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n…（文件过大，仅显示前 {max_chars} 字符）"
        return text

    async def write(self, rel: str, content: str) -> str:
        p = self.resolve(rel)
        if p.is_dir():
            return f"目标是目录：{rel}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content or "", encoding="utf-8")
        return f"已写入 {rel}（{p.stat().st_size} 字节）"

    async def mkdir(self, rel: str) -> str:
        p = self.resolve(rel)
        p.mkdir(parents=True, exist_ok=True)
        return f"目录就绪：{rel}"

    async def delete(self, rel: str) -> str:
        """删除单文件或空目录；非空目录拒绝（防误删）。"""
        p = self.resolve(rel)
        return await self._delete_path(p, rel)

    async def delete_abs(self, abs_path: str | Path) -> str:
        """按绝对路径删除（二次确认用）；再次校验必须在工作区内。"""
        p = Path(abs_path).resolve()
        if p != self.root and self.root not in p.parents:
            raise ValueError("路径超出工作区，已拒绝")
        return await self._delete_path(p, str(p))

    async def _delete_path(self, p: Path, label: str) -> str:
        if not p.exists():
            return f"路径不存在：{label}"
        if p.is_dir():
            if any(p.iterdir()):
                return f"目录非空，请先删除其中内容（安全限制）：{label}"
            p.rmdir()
            return f"已删除空目录：{label}"
        p.unlink()
        return f"已删除文件：{label}"
