import json

import pytest


@pytest.fixture(autouse=True)
def _isolate_personas_env(monkeypatch):
    """防止开发者/CI 设置的 PERSONAS_DIR 污染人格相关测试。

    注意：这是「隔离」而非「屏蔽」——PERSONAS_DIR 的行为由
    test_personas.py 里的显式用例正向覆盖，避免该分支永不执行。
    """
    monkeypatch.delenv("PERSONAS_DIR", raising=False)


@pytest.fixture
def symlinks_supported(tmp_path):
    """符号链接可用性探测。

    部分平台（未开启开发者模式的 Windows、受限容器）会**静默丢弃**符号链接：
    ``symlink_to`` 不报错但链接不存在，导致链接相关断言假失败。此类平台直接跳过，
    避免把「平台不支持」误报成「安全属性回归」。
    """
    target = tmp_path / ".symlink_target"
    target.write_text("x", encoding="utf-8")
    probe = tmp_path / ".symlink_probe"
    try:
        probe.symlink_to(target)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"当前平台无法创建符号链接：{e}")
    if not probe.is_symlink():
        pytest.skip("当前平台创建符号链接后不可见（静默丢弃），跳过链接相关用例")
    return True


@pytest.fixture(scope="session", autouse=True)
def _guard_destructive_pg_tests():
    """PG 集成测试会 TRUNCATE 表，绝不允许指向生产库。

    教训：一次端到端验证误用 DATABASE_URL（生产库）执行了 TRUNCATE，
    清空了真实会话历史——所以把「测试库不能等于生产库」变成硬断言。
    用 `python scripts/scratch_db.py create` 生成 TEST_DATABASE_URL。
    """
    import os
    import urllib.parse as up

    test_url = (os.getenv("TEST_DATABASE_URL") or "").strip()
    if not test_url:
        return
    prod_url = (os.getenv("DATABASE_URL") or "").strip()
    assert test_url != prod_url, (
        "TEST_DATABASE_URL 不能与 DATABASE_URL 相同：这些测试会清空表数据。"
        "请用 scripts/scratch_db.py create 建独立临时库。"
    )
    db_name = up.urlparse(test_url).path.lstrip("/").lower()
    assert any(t in db_name for t in ("test", "scratch")), (
        f"TEST_DATABASE_URL 的库名 {db_name!r} 不含 test/scratch，拒绝在疑似生产库上跑破坏性测试"
    )


@pytest.fixture(scope="session", autouse=True)
def _normalize_superusers_env():
    """与 bot.py 相同的归一化：逗号/裸数字形式转 JSON 数组。

    nonebot pydantic v2 要求 SUPERUSERS 是 set[str]，裸数字会被 dotenv 推断为
    int 直接让 nonebot.init() 校验失败（H4：插件 import 冒烟测试依赖 init）。
    """
    import os
    from pathlib import Path

    if Path(".env").is_file():
        from dotenv import load_dotenv

        load_dotenv(Path(".env"), override=False)
    raw = os.getenv("SUPERUSERS", "")
    if raw and not raw.strip().startswith("["):
        os.environ["SUPERUSERS"] = json.dumps(
            [x.strip() for x in raw.split(",") if x.strip()]
        )
