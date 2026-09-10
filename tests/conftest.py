import json

import pytest


@pytest.fixture(autouse=True)
def _isolate_personas_env(monkeypatch):
    """防止开发者/CI 设置的 PERSONAS_DIR 污染人格相关测试。"""
    monkeypatch.delenv("PERSONAS_DIR", raising=False)


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
