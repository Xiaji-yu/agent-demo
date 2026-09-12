from agentcore.skills.permissions import PermissionChecker


class TestPermissionChecker:
    def test_superuser_allowed(self):
        checker = PermissionChecker(superusers={"111", "222"})
        assert checker.is_allowed("search_web", "111", None) is True

    def test_non_superuser_allowed_for_public(self):
        checker = PermissionChecker(superusers={"111"})
        # public skill 对非 superuser 默认允许
        assert checker.is_allowed("search_web", "222", None) is True

    def test_non_superuser_denied_for_private(self):
        checker = PermissionChecker(superusers={"111"}, default_permission="private")
        assert checker.is_allowed("secret_skill", "222", None) is False

    def test_wildcard_superuser_allows_all(self):
        checker = PermissionChecker(superusers={"*"})
        assert checker.is_allowed("search_web", "anyone", None) is True

    def test_group_skill_allowed(self):
        checker = PermissionChecker(
            superusers=set(),
            group_skills={"123": {"search_web", "calc"}},
            default_permission="public",
        )
        assert checker.is_allowed("search_web", "222", "123") is True

    def test_group_skill_denied_for_private(self):
        checker = PermissionChecker(
            superusers=set(),
            group_skills={"123": {"search_web"}},
            default_permission="private",
        )
        assert checker.is_allowed("calc", "222", "123") is False

    def test_user_skill_allowed(self):
        checker = PermissionChecker(
            superusers=set(),
            user_skills={"222": {"calc"}},
            default_permission="public",
        )
        assert checker.is_allowed("calc", "222", None) is True

    def test_user_skill_denied_for_private(self):
        checker = PermissionChecker(
            superusers=set(),
            user_skills={"222": {"calc"}},
            default_permission="private",
        )
        assert checker.is_allowed("search_web", "333", None) is False

    def test_default_permission_public(self):
        checker = PermissionChecker(superusers=set(), default_permission="public")
        assert checker.is_allowed("search_web", "222", None) is True

    def test_default_permission_private(self):
        checker = PermissionChecker(superusers=set(), default_permission="private")
        assert checker.is_allowed("search_web", "222", None) is False

    def test_namespace_wildcard(self):
        checker = PermissionChecker(
            superusers=set(),
            user_skills={"222": {"web.*"}},
            default_permission="private",
        )
        assert checker.is_allowed("web.search_web", "222", None) is True
        assert checker.is_allowed("web.fetch_url", "222", None) is True
        assert checker.is_allowed("calc", "222", None) is False

    def test_empty_superusers_denies_private(self):
        checker = PermissionChecker(superusers=set(), default_permission="private")
        assert checker.is_allowed("secret_skill", "222", None) is False
