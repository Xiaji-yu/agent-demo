"""bot.py 启动期 SUPERUSERS 规范化的行为锁。

来源：2026-10-27 容器首启事故——照 `.env.example` 模板生成的 `.env` 未填
`SUPERUSERS`（模板默认空行，注释承诺「留空 = 没有超管」），但 bot.py 只把
**非空**值转成 JSON 数组，空值原样穿透给 NoneBot → pydantic-settings 对
「存在但为空」的 complex 字段照样 ``json.loads("")`` →
``SettingsError: error parsing env var "superusers"``，启动即崩（宿主机与
容器同坑，容器表现为 restart 循环）。

用子进程跑**生产 import 路径**（cwd=仓库根，与 Dockerfile 构建期
``python -c "import bot"`` 冒烟同口径）：进程内单测无法复现——nonebot 已在
本进程初始化过，``import bot`` 不会重新走 ``init()``。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_fresh_process(env_superusers: str) -> subprocess.CompletedProcess[str]:
    """以指定 SUPERUSERS 值在干净子进程 import bot，返回 CompletedProcess。

    SUPERUSERS 显式放在**环境层**：本地 `.env` 里若配了真实超管，
    load_dotenv(override=False) 不会覆盖已存在的键——测试对本机 .env 免疫。
    """
    import os

    env = dict(os.environ)
    env["SUPERUSERS"] = env_superusers
    script = (
        "import bot\n"
        "from nonebot import get_driver\n"
        "print('SUPERUSERS_PARSED:',"
        " sorted(get_driver().config.superusers))\n"
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


class TestSuperusersEnvNormalization:
    def test_empty_superusers_env_starts_without_settings_error(self):
        """模板默认形态（SUPERUSERS= 空行）必须能启动 = 没有超管。

        修前：SettingsError → returncode != 0（变异复核即抓这条）。
        """
        proc = _run_fresh_process("")
        assert proc.returncode == 0, f"子进程失败：\n{proc.stderr[-2000:]}"
        assert "SettingsError" not in proc.stderr, proc.stderr[-2000:]
        assert "SUPERUSERS_PARSED: []" in proc.stdout, proc.stdout[-1000:]

    def test_whitespace_superusers_env_treated_as_empty(self):
        """纯空白值与空值同语义（strip 后为空 → pop）。"""
        proc = _run_fresh_process("   ")
        assert proc.returncode == 0, f"子进程失败：\n{proc.stderr[-2000:]}"
        assert "SUPERUSERS_PARSED: []" in proc.stdout, proc.stdout[-1000:]

    def test_plain_qq_list_normalized_to_json_set(self):
        """裸 QQ 号逗号列表（模板推荐写法）被转成 set[str]——既有行为防回归。"""
        proc = _run_fresh_process("123456, 789")
        assert proc.returncode == 0, f"子进程失败：\n{proc.stderr[-2000:]}"
        assert "SUPERUSERS_PARSED: ['123456', '789']" in proc.stdout, proc.stdout[
            -1000:
        ]
