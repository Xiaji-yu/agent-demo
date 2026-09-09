"""Skill 安装器：持久化到 data/skills/，支持安装/卸载/列出。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from agentcore.skills.manifest import SkillManifest

logger = logging.getLogger(__name__)

DEFAULT_SKILLS_DIR = Path("data/skills")
INSTALLED_FILE = "installed.yaml"


class SkillInstaller:
    def __init__(self, skills_dir: Path | None = None):
        self.skills_dir = skills_dir or DEFAULT_SKILLS_DIR
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, SkillManifest] = {}
        self._load_all()

    def _load_all(self) -> None:
        self._cache.clear()
        for path in sorted(self.skills_dir.glob("*.yaml")):
            if path.name == INSTALLED_FILE:
                continue
            try:
                manifest = SkillManifest.from_yaml(path.read_text(encoding="utf-8"))
                self._cache[manifest.name] = manifest
            except Exception as e:
                logger.warning("Failed to load skill %s: %s", path, e)

    def list_manifests(self) -> list[SkillManifest]:
        return list(self._cache.values())

    def get(self, name: str) -> SkillManifest | None:
        return self._cache.get(name)

    def install(self, manifest: SkillManifest) -> None:
        manifest.validate()
        target = self.skills_dir / f"{manifest.name}.yaml"
        target.write_text(manifest.to_yaml(), encoding="utf-8")
        self._cache[manifest.name] = manifest
        logger.info("Installed skill: %s -> %s", manifest.name, target)

    def uninstall(self, name: str) -> bool:
        target = self.skills_dir / f"{name}.yaml"
        if not target.exists():
            return False
        target.unlink()
        self._cache.pop(name, None)
        logger.info("Uninstalled skill: %s", name)
        return True
