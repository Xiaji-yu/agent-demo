"""Skill 系统：可权限化、可配置化的能力注册表。"""
from .registry import SkillRegistry, Skill, registry
from .permissions import PermissionChecker
from .builtin import register_builtin_skills

__all__ = ["SkillRegistry", "Skill", "PermissionChecker", "register_builtin_skills", "registry"]
