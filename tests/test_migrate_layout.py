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
from pathlib import Path

from agentcore import migrate_layout as M


def _write_jobs(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "jobs": []}), encoding="utf-8")


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

        def boom(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr(M.shutil, "copy2", boom)
        with caplog.at_level(logging.WARNING, logger="agentcore.migrate_layout"):
            moved = M.migrate_legacy_layout(
                workspace=ws, cron_dir=target, personas_dir=tmp_path / "p"
            )
        assert moved == []
        assert "迁移失败" in caplog.text
        assert (ws / "cron" / "jobs.json").is_file()
