from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

from agentcore.llm.client import LLMClient
from agentcore.skills.permissions import PermissionChecker

logger = logging.getLogger(__name__)

Handler = Callable[..., Awaitable[Any]]


class Skill:
    def __init__(
        self,
        name: str,
        description: str,
        params_schema: dict,
        handler: Handler,
        permission: str = "public",
        manifest: Any = None,
    ):
        self.name = name
        self.description = description
        self.params_schema = params_schema
        self.handler = handler
        self.permission = permission
        self.manifest = manifest

    def to_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.params_schema,
            },
        }


class SkillRegistry:
    """Skill 注册表：带权限过滤 + 按用户/群可见性 + 动态安装/卸载。"""

    def __init__(self, permission_checker: PermissionChecker | None = None):
        self.skills: dict[str, Skill] = {}
        self.permission_checker = permission_checker

    def register(
        self,
        name: str,
        description: str,
        params_schema: dict,
        permission: str = "public",
        manifest: Any = None,
    ):
        def decorator(handler: Handler):
            self.skills[name] = Skill(name, description, params_schema, handler, permission, manifest)
            return handler

        return decorator

    def set_permission_checker(self, checker: PermissionChecker) -> None:
        self.permission_checker = checker

    def get_schemas(self, user_id: str | None = None, group_id: str | None = None) -> list[dict]:
        return [
            s.to_openai_schema()
            for s in self.skills.values()
            if self._is_allowed(s, user_id, group_id)
        ]

    async def execute(
        self,
        name: str,
        user_id: str | None = None,
        group_id: str | None = None,
        **kwargs,
    ) -> str:
        skill = self.skills.get(name)
        if not skill:
            return f"Error: unknown skill {name}"
        if not self._is_allowed(skill, user_id, group_id):
            return f"Error: permission denied for skill {name}"
        try:
            return await skill.handler(**kwargs)
        except Exception:
            logger.exception("skill execution failed: %s", name)
            return "Error: skill 执行失败，请稍后再试。"

    def is_allowed(self, skill_name: str, user_id: str | None, group_id: str | None) -> bool:
        skill = self.skills.get(skill_name)
        if not skill:
            return False
        return self._is_allowed(skill, user_id, group_id)

    def _is_allowed(self, skill: Skill, user_id: str | None, group_id: str | None) -> bool:
        if self.permission_checker:
            return self.permission_checker.is_allowed(skill.name, user_id, group_id)
        return skill.permission == "public"

    def install(self, manifest: Any, handler: Handler | None = None) -> None:
        """动态安装 skill：若未提供 handler，则安装为 prompt skill。"""
        params_schema = {
            "type": "object",
            "properties": {p["name"]: p for p in (manifest.parameters or [])},
            "required": [p["name"] for p in (manifest.parameters or []) if p.get("required")],
        }
        if handler is None:
            handler = _make_prompt_skill_handler(manifest)
        self.skills[manifest.name] = Skill(
            name=manifest.name,
            description=manifest.description,
            params_schema=params_schema,
            handler=handler,
            permission=manifest.permission,
            manifest=manifest,
        )
        logger.info("Installed skill: %s", manifest.name)

    def uninstall(self, name: str) -> bool:
        if name not in self.skills:
            return False
        del self.skills[name]
        logger.info("Uninstalled skill: %s", name)
        return True


def _make_prompt_skill_handler(manifest: Any):
    async def _handler(**kwargs):
        return _run_prompt_skill(manifest, kwargs)

    _handler.__name__ = f"prompt_skill_{manifest.name}"
    return _handler


async def _run_prompt_skill(manifest: Any, arguments: dict) -> str:
    try:
        from agentcore.llm.client import LLMClient
        from agentcore.skills.installer import SkillInstaller

        installer = SkillInstaller()
        llm = LLMClient()

        user_content = json.dumps(arguments, ensure_ascii=False)
        messages = [
            {"role": "system", "content": manifest.prompt},
            {"role": "user", "content": user_content},
        ]
        response = await llm.chat(messages, tools=None)
        choice = (response.get("choices") or [{}])[0].get("message") or {}
        return (choice.get("content") or "").strip() or "（skill 无输出）"
    except Exception:
        logger.exception("prompt skill execution failed: %s", manifest.name)
        return "Error: prompt skill 执行失败，请稍后再试。"


registry = SkillRegistry()
