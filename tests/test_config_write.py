"""config_write（D2-1 受控写入核心）纯函数测试：定位/补丁/校验/备份/原子写/审计掩码。

来源：BACKLOG §1 D 组定稿（2026-09-29 两轮交互）。护栏全部做过
"改坏实现 → 用例必须失败"的变异复核（见 TestSettingsWrite 与本文件尾注）。
"""

import stat

import pytest

from plugins.qq_agent_adapter import config_write as cw


class TestValidateValue:
    def test_bool_canonical(self):
        spec, normalized, file_text = cw.validate_value("AGENT_VISION", "TRUE")
        assert normalized is True and file_text == "true"
        _, normalized, file_text = cw.validate_value("AGENT_VISION", "off")
        assert normalized is False and file_text == "false"

    def test_bool_rejects_garbage(self):
        with pytest.raises(cw.ValueValidationError):
            cw.validate_value("AGENT_VISION", "maybe")

    def test_int_bounds(self):
        _, v, ft = cw.validate_value("AGENT_GROUP_CONTEXT_LINES", "50")
        assert v == 50 and ft == "50"
        for bad in ("0", "101", "abc", "1.5"):
            with pytest.raises(cw.ValueValidationError):
                cw.validate_value("AGENT_GROUP_CONTEXT_LINES", bad)

    def test_daily_tokens_zero_means_unlimited(self):
        _, v, ft = cw.validate_value("AGENT_BUDGET_DAILY_TOKENS", "0")
        assert v == 0 and ft == "0"

    def test_csv_limits(self):
        _, items, ft = cw.validate_value("AGENT_WAKE_WORDS", " 云崽 , 小助手 ")
        assert items == ["云崽", "小助手"] and ft == "云崽,小助手"
        with pytest.raises(cw.ValueValidationError):
            cw.validate_value("AGENT_WAKE_WORDS", ",".join("x" * 32 for _ in range(21)))
        with pytest.raises(cw.ValueValidationError):
            cw.validate_value("AGENT_WAKE_WORDS", "x" * 33)

    def test_forbidden_chars_rejected(self):
        """行格式注入面：换行=注入新键；# 引号改变 dotenv 解析边界。"""
        for bad in ("a\nAGENT_WEB_TOKEN=pwned", "a#comment", 'a"b', "a'b", "a\rb"):
            with pytest.raises(cw.ValueValidationError):
                cw.validate_value("AGENT_WAKE_WORDS", bad)

    def test_unknown_key_not_writable(self):
        with pytest.raises(cw.ValueValidationError) as ei:
            cw.validate_value("LLM_API_KEY", "sk-anything")
        assert str(ei.value) == "key_not_writable"
        with pytest.raises(cw.ValueValidationError):
            cw.validate_value("AGENT_WEB_TOKEN", "whatever")


class TestLocateAndPatch:
    BASE = "A=1\nAGENT_VISION=0\nB=2\n"

    def test_locate_hit_missing_duplicate(self):
        assert cw.locate_env_key(self.BASE, "AGENT_VISION") == 1
        assert cw.locate_env_key(self.BASE, "NOPE") == -1
        assert cw.locate_env_key(self.BASE + "AGENT_VISION=1\n", "AGENT_VISION") == -2

    def test_patch_replaces_only_target_line(self):
        new, mode = cw.patch_env_text(self.BASE, "AGENT_VISION", "true")
        assert mode == "replace"
        assert new == "A=1\nAGENT_VISION=true\nB=2\n"

    def test_patch_appends_missing_key(self):
        new, mode = cw.patch_env_text("A=1", "AGENT_VISION", "true")
        assert mode == "append"
        assert new == "A=1\n# via agent-web\nAGENT_VISION=true\n"

    def test_patch_duplicate_raises(self):
        with pytest.raises(cw.ValueValidationError):
            cw.patch_env_text(self.BASE + "AGENT_VISION=1\n", "AGENT_VISION", "true")

    def test_parse_env_value_last_wins_and_strips(self):
        text = "K= a \nOTHER=x\nK=b\n"
        assert cw.parse_env_value(text, "K") == "b"
        assert cw.parse_env_value(text, "NOPE") is None


class TestBackupAtomicVerify:
    def test_backup_and_prune(self, tmp_path):
        src = tmp_path / ".env"
        src.write_text("K=1\n", encoding="utf-8")
        bdir = tmp_path / "backups"
        first = cw.backup_file(src, bdir)
        assert first.exists() and first.read_text(encoding="utf-8") == "K=1\n"
        for _ in range(cw._BACKUP_KEEP + 3):
            cw.backup_file(src, bdir)
        left = sorted(bdir.glob("*.bak"))
        assert len(left) == cw._BACKUP_KEEP

    def test_atomic_write_and_restore(self, tmp_path):
        src = tmp_path / ".env"
        src.write_text("K=old\n", encoding="utf-8")
        cw.atomic_write(src, "K=new\n")
        assert src.read_text(encoding="utf-8") == "K=new\n"
        assert not src.with_name(".env.tmp.write").exists()
        bak = cw.backup_file(src, tmp_path / "b")
        src.write_text("K=broken\n", encoding="utf-8")
        cw.restore_file(bak, src)
        assert src.read_text(encoding="utf-8") == "K=new\n"

    def test_verify_env(self, tmp_path):
        src = tmp_path / ".env"
        src.write_text("AGENT_VISION=false\n", encoding="utf-8")
        assert cw.verify_env(src, "AGENT_VISION", "false") is True
        assert cw.verify_env(src, "AGENT_VISION", "true") is False


class TestApplyAndMask:
    def test_apply_runtime_environ_and_budget(self, monkeypatch):
        class FakeBudget:
            daily_tokens = 0
            enforce = False

        import agentcore.budget as budget_mod

        fake = FakeBudget()
        monkeypatch.setattr(budget_mod, "get_budget", lambda: fake)
        spec, normalized, file_text = cw.validate_value("AGENT_BUDGET_ENFORCE", "true")
        assert cw.apply_runtime(spec, normalized, file_text) == "live"
        import os

        assert os.environ["AGENT_BUDGET_ENFORCE"] == "true"
        assert fake.enforce is True

    def test_masked(self):
        assert cw.masked("sk-real-secret") == "***"
        assert cw.masked("x" * 500) == "x" * cw._MASK_MAX
        assert cw.masked(None) == ""

    def test_patch_preserves_crlf(self):
        """CRLF 文件：只动目标行，其余行的 \\r\\n 逐字节保留。"""
        base = "A=1\r\nAGENT_VISION=false\r\nB=2\r\n"
        new, mode = cw.patch_env_text(base, "AGENT_VISION", "true")
        assert mode == "replace"
        assert new == "A=1\r\nAGENT_VISION=true\r\nB=2\r\n"


class TestReviewFixM1M2M3:
    """REVIEW-26fec4d..3ce6e0a 修复回归：M1/M2/M3（M4-M10 见 test_web）。"""

    def test_atomic_write_preserves_mode(self, tmp_path):
        """M3：0600 的 .env 经原子写后仍 0600（旧实现静默放宽成 0644）。"""
        f = tmp_path / ".env"
        f.write_text("K=1\n", encoding="utf-8")
        f.chmod(0o600)
        cw.atomic_write(f, "K=2\n")
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
        # 0600 之外的常见模式同样继承
        f.chmod(0o640)
        cw.atomic_write(f, "K=3\n")
        assert stat.S_IMODE(f.stat().st_mode) == 0o640

    def test_atomic_write_missing_file_defaults_600(self, tmp_path):
        """M3 兜底：文件不存在时新文件按 0600（凭据文件的安全默认）。"""
        import os

        f = tmp_path / ".env"
        cw.atomic_write(f, "K=1\n")
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
        if os.getuid() != 0:  # root 下 umask 分析退化为恒真，非 root 才验
            assert (f.stat().st_mode & 0o077) == 0

    def test_apply_runtime_writes_group_context_singleton(self, monkeypatch):
        """M2：LINES/TTL 必须回写 group_context 单例属性（只在 env 里改了不算）。"""
        from plugins.qq_agent_adapter.group_context import (
            GroupContextBuffer,
        )

        original = GroupContextBuffer(max_lines=7, ttl=100.0)
        monkeypatch.setattr(
            "plugins.qq_agent_adapter.group_context.group_context", original
        )
        spec, normalized, file_text = cw.validate_value(
            "AGENT_GROUP_CONTEXT_LINES", "3"
        )
        cw.apply_runtime(spec, normalized, file_text)
        assert original.max_lines == 3
        spec2, normalized2, file_text2 = cw.validate_value(
            "AGENT_GROUP_CONTEXT_TTL", "42"
        )
        cw.apply_runtime(spec2, normalized2, file_text2)
        assert original.ttl == 42.0
