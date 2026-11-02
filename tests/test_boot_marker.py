"""启动标记（data/state/boot.json）：内容、权限、失败降级。

它是 /update 唯一的重启成功信号，也是「现在跑的是哪个提交」的权威答案；
写失败必须只告警（版本指纹不该阻塞启动）。
"""

from __future__ import annotations

import json
import os

from plugins.qq_agent_adapter import boot_marker


def test_writes_pid_sha_and_is_private(tmp_path):
    target = tmp_path / "state" / "boot.json"

    assert boot_marker.write_boot_marker(target, now=123.0) is True

    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["pid"] == os.getpid()
    assert data["started_at"] == 123.0
    assert set(data) >= {"pid", "sha", "branch", "version", "started_at"}
    assert target.stat().st_mode & 0o777 == 0o600, (
        "部署版本与进程号不该给同机其他用户看"
    )


def test_write_failure_is_soft(tmp_path):
    blocker = tmp_path / "state"
    blocker.write_text("not a directory", encoding="utf-8")

    assert boot_marker.write_boot_marker(blocker / "boot.json") is False
