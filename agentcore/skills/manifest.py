"""Skill 清单：YAML 解析、校验、序列化。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


RE_NAME = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$")


@dataclass
class SkillManifest:
    name: str
    description: str
    type: str = "prompt"
    prompt: str = ""
    parameters: list[dict[str, Any]] = field(default_factory=list)
    permission: str = "public"

    def __post_init__(self):
        self.validate()

    def validate(self) -> None:
        if not RE_NAME.match(self.name):
            raise ValueError(f"Invalid skill name: {self.name}")
        if self.type not in {"prompt", "tool"}:
            raise ValueError(f"Invalid skill type: {self.type}")
        if self.type == "prompt" and not self.prompt.strip():
            raise ValueError("Prompt skills require a non-empty prompt")
        if self.permission not in {"public", "superuser", "private"}:
            raise ValueError(f"Invalid permission: {self.permission}")

    @classmethod
    def from_yaml(cls, text: str) -> SkillManifest:
        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            raise ValueError("Skill manifest must be a YAML mapping")
        manifest = cls(
            name=str(data.get("name", "")).strip(),
            description=str(data.get("description", "")).strip(),
            type=str(data.get("type", "prompt")).strip(),
            prompt=str(data.get("prompt", "")).strip(),
            parameters=[p for p in (data.get("parameters") or []) if isinstance(p, dict)],
            permission=str(data.get("permission", "public")).strip(),
        )
        manifest.validate()
        return manifest

    def to_yaml(self) -> str:
        return yaml.safe_dump(
            {
                "name": self.name,
                "description": self.description,
                "type": self.type,
                "prompt": self.prompt,
                "parameters": self.parameters,
                "permission": self.permission,
            },
            allow_unicode=True,
            sort_keys=False,
        )
