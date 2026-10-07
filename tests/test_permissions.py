import pytest

from agentcore.skills.permissions import PermissionChecker


def test_config_yaml_has_no_permissions_section():
    """P0 回归锁：config.yaml 不再提供 skills.permissions 段。

    旧段的 ``superusers: ["*"]`` 会把 PermissionChecker 变成对所有人放行，
    registry 层的 superuser 门（ops/log_tail/db_query…）形同虚设。skill 的
    superuser 门唯一来源是 .env 的 SUPERUSERS（经 NoneBot config 注入）。
    """
    from pathlib import Path

    import yaml

    cfg_path = Path(__file__).resolve().parents[1] / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    assert "permissions" not in (cfg.get("skills") or {})


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

    def test_wildcard_superuser_is_not_honored(self, caplog):
        """M5（REVIEW-de09478..workdir）：env 层 SUPERUSERS=* 不再放行所有人。

        旧语义让 registry 层全部 superuser 技能对任意人披露，而 handler 层
        is_superuser 对所有人 fail-closed——净效果=最大披露面 + 功能全瞎。
        现在 "*" 在构造期剔除并告警，按空集处理（P0 决意，勿恢复）。
        """
        import logging

        with caplog.at_level(logging.WARNING, logger="agentcore.skills.permissions"):
            checker = PermissionChecker(superusers={"*"})
        assert checker.superusers == set(), '"*" 必须被剔除'
        # superuser 级技能不得因 '*' 对任意人放行（旧语义这里返回 True）
        assert checker.is_allowed("log_tail", "anyone", None, "superuser") is False
        assert checker.is_allowed("secret_skill", "222", None, "superuser") is False
        # public 技能的默认放行不受影响（那是另一维度）
        assert checker.is_allowed("calc", "222", None, "public") is True
        assert caplog.records, "剔除通配必须告警，让错误配置及时暴露"

    def test_real_superuser_still_allowed_alongside_wildcard(self):
        """守卫：剔除 "*" 不得误伤同集合里的真实超管条目。"""
        checker = PermissionChecker(superusers={"*", "111"})
        assert checker.superusers == {"111"}
        assert checker.is_allowed("log_tail", "111", None, "superuser") is True
        assert checker.is_allowed("log_tail", "222", None, "superuser") is False

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


class TestSuperuserEnvLayer:
    """L11（REVIEW-de09478..workdir）：env 侧（非 config.yaml）的 superuser
    集合解析与通配处置，与 PermissionChecker/acl 同口径。此前只有
    config.yaml 单侧锁；根因（yaml 段）已断，这里补 env 路径的锁。
    """

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("*", set()),
            ("* ", set()),
            ("111,222", {"111", "222"}),
            ('["333"]', {"333"}),
            ("", set()),
        ],
    )
    def test_env_layer_wildcard_discarded(self, monkeypatch, raw, expected):
        from agentcore.workspace.utils import load_superusers

        if raw:
            monkeypatch.setenv("SUPERUSERS", raw)
        else:
            monkeypatch.delenv("SUPERUSERS", raising=False)
        users = load_superusers()
        users.discard("*")  # 门层剔除（acl._get_superusers / checker 同判据）
        assert users == expected
