"""人格（Persona）系统：从 md 文件加载人格，供 AgentEngine 注入 system prompt。

每个人格文件是一个 markdown，形如：

    ---
    name: assistant
    description: 通用 AI 助手（默认人格）
    default: true
    ---

    这里是人格行为指南，会作为 system prompt 的一部分注入给模型。
    留空表示不注入额外行为（保持通用助手）。
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import yaml

logger = logging.getLogger(__name__)

DEFAULT_PERSONAS_DIR = Path(__file__).resolve().parent

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass
class Persona:
    name: str
    description: str = ""
    body: str = ""
    default: bool = False
    file: str = ""  # 绝对路径，便于展示


def _split_frontmatter(text: str) -> tuple[Optional[dict], str]:
    """解析 md 的 YAML frontmatter（--- 开头）。无 frontmatter 返回 (None, 全文)。"""
    if not text.startswith("---"):
        return None, text
    lines = text.splitlines()
    if len(lines) < 2:
        return None, text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return None, text
    try:
        meta = yaml.safe_load("\n".join(lines[1:end]))
    except Exception:
        logger.warning("persona frontmatter parse failed in %s", text[:60])
        return None, text
    body = "\n".join(lines[end + 1 :]).strip()
    return (meta if isinstance(meta, dict) else {}), body


class PersonaManager:
    def __init__(self, dir_path: str | Path | None = None):
        env_dir = (os.getenv("PERSONAS_DIR") or "").strip()
        if env_dir:
            self.dir = Path(env_dir)
        elif dir_path:
            self.dir = Path(dir_path)
        else:
            self.dir = DEFAULT_PERSONAS_DIR
        self.dir = self.dir.resolve()
        self._by_name: Optional[dict[str, Persona]] = None

    def _scan(self) -> dict[str, Persona]:
        personas: dict[str, Persona] = {}
        if not self.dir.is_dir():
            logger.warning("personas dir not found: %s", self.dir)
            return personas
        for md in sorted(self.dir.glob("*.md")):
            try:
                text = md.read_text(encoding="utf-8")
            except Exception:
                logger.warning("cannot read persona file %s", md, exc_info=True)
                continue
            meta, body = _split_frontmatter(text)
            if not meta or not meta.get("name"):
                logger.warning("persona file %s missing name in frontmatter, skipped", md.name)
                continue
            name = str(meta["name"])
            if not _NAME_RE.match(name):
                logger.warning("persona name %r invalid, skipped", name)
                continue
            personas[name] = Persona(
                name=name,
                description=str(meta.get("description") or ""),
                body=body,
                default=bool(meta.get("default", False)),
                file=str(md),
            )
        return personas

    def refresh(self) -> None:
        self._by_name = self._scan()

    def list(self) -> List[Persona]:
        if self._by_name is None:
            self._by_name = self._scan()
        return sorted(self._by_name.values(), key=lambda p: (not p.default, p.name))

    def get(self, name: str) -> Optional[Persona]:
        if not _NAME_RE.match(name or ""):
            return None
        if self._by_name is None:
            self._by_name = self._scan()
        return self._by_name.get(name)

    def default(self) -> Optional[Persona]:
        for p in self.list():
            if p.default:
                return p
        return None


def load_manager() -> PersonaManager:
    return PersonaManager()
