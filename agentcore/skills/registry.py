from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

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
    ):
        self.name = name
        self.description = description
        self.params_schema = params_schema
        self.handler = handler
        self.permission = permission

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
    """Skill 注册表：带权限过滤 + 按用户/群可见性。"""

    def __init__(self, permission_checker: PermissionChecker | None = None):
        self.skills: dict[str, Skill] = {}
        self.permission_checker = permission_checker

    def register(
        self,
        name: str,
        description: str,
        params_schema: dict,
        permission: str = "public",
    ):
        def decorator(handler: Handler):
            self.skills[name] = Skill(name, description, params_schema, handler, permission)
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
        except Exception as e:
            logger.exception("skill execution failed: %s", name)
            return f"Error: {e}"

    def is_allowed(self, skill_name: str, user_id: str | None, group_id: str | None) -> bool:
        skill = self.skills.get(skill_name)
        if not skill:
            return False
        return self._is_allowed(skill, user_id, group_id)

    def _is_allowed(self, skill: Skill, user_id: str | None, group_id: str | None) -> bool:
        if self.permission_checker:
            return self.permission_checker.is_allowed(skill.name, user_id, group_id)
        return skill.permission == "public"


registry = SkillRegistry()
