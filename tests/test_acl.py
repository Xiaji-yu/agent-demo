import os
import pytest

from plugins.qq_agent_adapter.acl import is_allowed


class FakePrivateEvent:
    def __init__(self, user_id):
        self.user_id = user_id
        self.group_id = None

    def get_user_id(self):
        return str(self.user_id)


class FakeGroupEvent:
    def __init__(self, user_id, group_id):
        self.user_id = user_id
        self.group_id = group_id

    def get_user_id(self):
        return str(self.user_id)


class TestACL:
    def setup_method(self):
        os.environ["SUPERUSERS"] = "111,222"
        os.environ["ALLOWED_GROUPS"] = "333"
        # reload module to pick up env changes
        import importlib
        import plugins.qq_agent_adapter.acl as acl_mod

        importlib.reload(acl_mod)
        self.acl = acl_mod

    def teardown_method(self):
        os.environ.pop("SUPERUSERS", None)
        os.environ.pop("ALLOWED_GROUPS", None)

    def test_superuser_private_allowed(self):
        assert self.acl.is_allowed(FakePrivateEvent(111)) is True

    def test_non_superuser_private_denied(self):
        assert self.acl.is_allowed(FakePrivateEvent(999)) is False

    def test_superuser_group_allowed(self):
        assert self.acl.is_allowed(FakeGroupEvent(111, 333)) is True

    def test_non_superuser_allowed_group_allowed(self):
        assert self.acl.is_allowed(FakeGroupEvent(999, 333)) is True

    def test_non_superuser_denied_group_not_allowed(self):
        assert self.acl.is_allowed(FakeGroupEvent(999, 444)) is False
