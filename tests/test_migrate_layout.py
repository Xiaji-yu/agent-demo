"""旧布局迁移（2026-10-29）：``data/workspace/{cron,personas,SOUL.md}`` → ``data/``。

迁移是「复制不搬」：旧文件必须原样保留（回滚版本时人格与定时任务不能凭空消失），
新位已存在则跳过（不覆盖用户在新位置手改的内容）。

变异锚点：
1. 去掉 ``not dst.exists()`` 保护 → 覆盖用例红
2. 把 copy 改成 move → 「旧文件保留」用例红
3. 去掉异常兜底 → 迁移失败用例红（启动被一条坏文件拖死）
"""

from __future__ import annotations

import json
import os as _os
from pathlib import Path

import pytest

from agentcore import migrate_layout as M


def _write_jobs(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "jobs": []}), encoding="utf-8")


class TestCopyFileCreatesStrictMode:
    """C5（REVIEW 2026-11-02）：迁移副本必须创建即 0600。

    原实现 ``shutil.copy2`` 继承源文件模式（源 0644 → 副本 0644）再补
    chmod；现在改成 os.open(0o600) 创建后拷内容。Windows 无 POSIX 权限
    语义，所以这里断言 fchmod 被以 0o600 调用 + 副本内容正确，落盘权限位
    由 CI（Linux）覆盖。
    """

    def test_copy_file_creates_strict_and_copies_content(self, tmp_path, monkeypatch):
        import os as _os

        src = tmp_path / "src.json"
        src.write_bytes(b'{"jobs": []}')
        dst = tmp_path / "nested" / "dst.json"
        seen: list[int] = []
        real = _os.fchmod

        def _spy(fd, mode):
            seen.append(mode)
            return real(fd, mode)

        monkeypatch.setattr(_os, "fchmod", _spy)
        moved: list[str] = []
        M._copy_file(src, dst, moved)
        assert moved == [str(dst)]
        assert dst.read_bytes() == b'{"jobs": []}'
        assert seen == [0o600]

    @pytest.mark.skipif(_os.name != "posix", reason="Windows 无 POSIX 权限语义")
    def test_copy_file_dst_is_0600_even_if_src_is_0644(self, tmp_path):
        """C5：源是 0644 时副本也必须是 0600（copy2 会继承 0644）。"""
        import os as _os

        src = tmp_path / "src.json"
        src.write_bytes(b"x")
        fd = _os.open(src, _os.O_WRONLY, 0o644)
        _os.close(fd)
        dst = tmp_path / "dst.json"
        M._copy_file(src, dst, [])
        assert _os.stat(dst).st_mode & 0o777 == 0o600


class TestLegacyPersonaLayoutIsAlreadyLive:
    """``PERSONAS_DIR`` 仍指向 workspace 布局时，写面就在旧位——迁移必须整段跳过。

    否则 ``ws/SOUL.md`` 会被拷成 ``ws/personas/SOUL.md``：一个没人读的副本，
    而且下次人工排查会以为人格有两份。
    """

    def test_no_persona_copy_when_write_root_is_legacy_dir(self, tmp_path):
        ws = tmp_path / "workspace"
        (ws / "personas" / "old").mkdir(parents=True)
        (ws / "personas" / "old" / "SOUL.md").write_text(
            "# 旧" + chr(10), encoding="utf-8"
        )
        (ws / "SOUL.md").write_text("# 默认" + chr(10), encoding="utf-8")

        moved = M.migrate_legacy_layout(
            workspace=ws,
            cron_dir=tmp_path / "cron",
            personas_dir=ws / "personas",
        )

        assert moved == []
        assert not (ws / "personas" / "SOUL.md").exists(), "不能造出没人读的副本"


class TestMigration:
    def test_copies_cron_and_keeps_legacy(self, tmp_path):
        ws = tmp_path / "workspace"
        cron = ws / "cron"
        _write_jobs(cron / "jobs.json")
        (cron / "state.json").write_text("{}", encoding="utf-8")
        target = tmp_path / "cron"

        moved = M.migrate_legacy_layout(
            workspace=ws, cron_dir=target, personas_dir=tmp_path / "personas"
        )

        assert (target / "jobs.json").is_file()
        assert (target / "state.json").is_file()
        # 复制不搬：旧文件仍在
        assert (cron / "jobs.json").is_file()
        assert len(moved) == 2

    def test_does_not_overwrite_existing_target(self, tmp_path):
        ws = tmp_path / "workspace"
        _write_jobs(ws / "cron" / "jobs.json")
        target = tmp_path / "cron"
        target.mkdir()
        (target / "jobs.json").write_text("new", encoding="utf-8")

        moved = M.migrate_legacy_layout(
            workspace=ws, cron_dir=target, personas_dir=tmp_path / "personas"
        )

        assert (target / "jobs.json").read_text(encoding="utf-8") == "new"
        assert moved == []

    def test_copies_personas_and_default_soul(self, tmp_path):
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "SOUL.md").write_text("默认人格", encoding="utf-8")
        (ws / "personas" / "alice").mkdir(parents=True)
        (ws / "personas" / "alice" / "SOUL.md").write_text("alice", encoding="utf-8")
        personas = tmp_path / "personas"

        M.migrate_legacy_layout(
            workspace=ws, cron_dir=tmp_path / "cron", personas_dir=personas
        )

        assert (personas / "SOUL.md").read_text(encoding="utf-8") == "默认人格"
        assert (personas / "alice" / "SOUL.md").read_text(encoding="utf-8") == "alice"
        # 旧位保留
        assert (ws / "personas" / "alice" / "SOUL.md").is_file()

    def test_idempotent(self, tmp_path):
        ws = tmp_path / "workspace"
        _write_jobs(ws / "cron" / "jobs.json")
        (ws / "SOUL.md").write_text("x", encoding="utf-8")
        kwargs = dict(
            workspace=ws, cron_dir=tmp_path / "cron", personas_dir=tmp_path / "p"
        )
        assert len(M.migrate_legacy_layout(**kwargs)) == 2
        assert M.migrate_legacy_layout(**kwargs) == []

    def test_same_directory_is_noop(self, tmp_path):
        ws = tmp_path / "workspace"
        _write_jobs(ws / "cron" / "jobs.json")
        assert (
            M.migrate_legacy_layout(
                workspace=ws,
                cron_dir=ws / "cron",
                personas_dir=ws / "personas",
            )
            == []
        )

    def test_failure_is_tolerated(self, tmp_path, caplog, monkeypatch):
        """复制抛 OSError（权限/磁盘满/占位）：只告警，不抛。"""
        import logging

        ws = tmp_path / "workspace"
        (ws / "cron").mkdir(parents=True)
        (ws / "cron" / "jobs.json").write_text("{}", encoding="utf-8")
        target = tmp_path / "cron"

        def boom(src, dst, length=16 * 1024):
            raise OSError("disk full")

        monkeypatch.setattr(M.shutil, "copyfileobj", boom)
        with caplog.at_level(logging.WARNING, logger="agentcore.migrate_layout"):
            moved = M.migrate_legacy_layout(
                workspace=ws, cron_dir=target, personas_dir=tmp_path / "p"
            )
        assert moved == []
        assert "迁移失败" in caplog.text
        assert (ws / "cron" / "jobs.json").is_file()
