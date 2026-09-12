"""Skill 系统：可权限化、可配置化的能力注册表。"""

from .builtin import register_builtin_skills
from .permissions import PermissionChecker
from .registry import Skill, SkillRegistry, registry

__all__ = [
    "SkillRegistry",
    "Skill",
    "PermissionChecker",
    "register_builtin_skills",
    "registry",
]
