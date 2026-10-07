"""Skill 权限控制。"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class PermissionChecker:
    """按 superuser / user / group 控制 skill 可用性，支持通配符。

    M5（REVIEW-de09478..workdir）：superuser 集合不支持 ``"*"`` 通配。
    历史坑：env ``SUPERUSERS=*`` 会让 checker 对任何人放行（registry 层全部
    superuser 技能披露），而 handler 层 ``is_superuser`` 对所有人 fail-closed
    ——净效果=超级用户 schema 全披露 + 功能全瞎，且与「勿恢复任何形式
    superusers 通配」的 P0 决意相悖。现在 ``"*"`` 在构造期即被剔除并告警
    （按空集处理），"wildcard" 只保留给 group/user 技能名匹配（``*``、
    ``ns.*``）——那是另一维度，不受影响。
    """

    def __init__(
        self,
        superusers: set[str] | None = None,
        group_skills: dict[str, set[str]] | None = None,
        user_skills: dict[str, set[str]] | None = None,
        default_permission: str = "public",
    ):
        self.superusers = set(superusers or [])
        if "*" in self.superusers:
            logger.warning(
                "[permissions] superusers 含 '*' 通配：已按空集剔除（勿恢复任何"
                "形式的 superusers 通配——P0 决意，REVIEW-de09478..workdir M5）"
            )
            self.superusers.discard("*")
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
        # superusers 全开（精确匹配；"*" 通配已在构造期剔除——M5）
        if user_id and user_id in self.superusers:
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
