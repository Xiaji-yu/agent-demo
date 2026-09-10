"""Skill 权限控制。"""
from __future__ import annotations


class PermissionChecker:
    """按 superuser / user / group 控制 skill 可用性，支持通配符。"""

    def __init__(
        self,
        superusers: set[str] | None = None,
        group_skills: dict[str, set[str]] | None = None,
        user_skills: dict[str, set[str]] | None = None,
        default_permission: str = "public",
    ):
        self.superusers = set(superusers or [])
        self.group_skills = {k: set(v) for k, v in (group_skills or {}).items()}
        self.user_skills = {k: set(v) for k, v in (user_skills or {}).items()}
        self.default_permission = default_permission

    def is_allowed(
        self,
        skill_name: str,
        user_id: str | None,
        group_id: str | None,
        skill_permission: str = "public",
    ) -> bool:
        # superusers 全开；集合中包含 "*" 表示所有用户都是 superuser
        if user_id and ("*" in self.superusers or user_id in self.superusers):
            return True
        # user 级覆盖
        if user_id and user_id in self.user_skills:
            if self._matches(self.user_skills[user_id], skill_name):
                return True
        # group 级覆盖
        if group_id and group_id in self.group_skills:
            if self._matches(self.group_skills[group_id], skill_name):
                return True
        # 非 public 技能（如 superuser）：仅显式授权（超管/user/group 配置）可见，
        # 不受 default_permission 影响——管理员工具的 schema 不再对普通用户暴露
        if skill_permission != "public":
            return False
        # 默认：仅 public
        return self.default_permission == "public"

    def _matches(self, allowed: set[str], skill_name: str) -> bool:
        if "*" in allowed:
            return True
        if skill_name in allowed:
            return True
        for pattern in allowed:
            if pattern.endswith(".*"):
                ns = pattern[:-2]
                if skill_name.startswith(f"{ns}."):
                    return True
        return False
