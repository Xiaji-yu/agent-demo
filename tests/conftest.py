import pytest


@pytest.fixture(autouse=True)
def _isolate_personas_env(monkeypatch):
    """防止开发者/CI 设置的 PERSONAS_DIR 污染人格相关测试。"""
    monkeypatch.delenv("PERSONAS_DIR", raising=False)
