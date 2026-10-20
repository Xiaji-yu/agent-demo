"""config.yaml 受控写入：文本级逐键补丁（保注释/顺序）+ 校验 + 失败还原。

为什么不 safe_load→safe_dump：那会把注释/空行/顺序全抹掉——config.yaml 的
注释本身就是运维文档。用例的第一组就是钉这个。
"""

import pytest

from plugins.qq_agent_adapter import config_yaml as cy


@pytest.fixture(autouse=True)
def _clean_diagnostics():
    """这些用例会写审计事件；不清会给 test_diagnostics 留下跨文件污染。"""
    from agentcore.diagnostics import clear

    clear()
    yield
    clear()


SAMPLE = """\
# 顶部说明
agent:
  max_iterations: 8          # 单轮最多调几次工具
  summary_enabled: true      # 超预算才激活
  history_token_budget: 3000   # CJK≈1/字

archive:
  dir: data/archive          # 全量聊天记录
  keep_days: 7
"""


class TestPatch:
    def test_preserves_comments_and_other_bytes(self):
        new, old_line, new_line = cy.patch_yaml_text(
            SAMPLE, "agent.history_token_budget", "4000"
        )
        assert new_line == "  history_token_budget: 4000   # CJK≈1/字"
        assert "  max_iterations: 8          # 单轮最多调几次工具" in new
        assert new.startswith("# 顶部说明\nagent:\n")
        assert "  dir: data/archive          # 全量聊天记录" in new

    def test_only_target_line_changes(self):
        new, _old, _new = cy.patch_yaml_text(SAMPLE, "agent.max_iterations", "10")
        before = SAMPLE.split("\n")
        after = new.split("\n")
        assert [
            i for i, (a, b) in enumerate(zip(before, after, strict=False)) if a != b
        ] == [2]

    def test_missing_key_raises(self):
        with pytest.raises(ValueError, match="yaml_key_not_found"):
            cy.patch_yaml_text(SAMPLE, "agent.nope", "1")

    def test_missing_section_raises(self):
        with pytest.raises(ValueError, match="yaml_section_not_found"):
            cy.patch_yaml_text(SAMPLE, "nope.key", "1")

    def test_duplicate_key_raises(self):
        text = SAMPLE.replace(
            "  max_iterations: 8", "  max_iterations: 8\n  max_iterations: 9"
        )
        with pytest.raises(ValueError, match="yaml_duplicate_key_lines"):
            cy.patch_yaml_text(text, "agent.max_iterations", "10")

    def test_nested_path_rejected(self):
        with pytest.raises(ValueError, match="yaml_path_not_supported"):
            cy.patch_yaml_text(SAMPLE, "push.jobs.name", "x")


class TestValidate:
    def test_bool_canonical(self):
        _, v, ft = cy.validate_value("agent.summary_enabled", "FALSE")
        assert v is False and ft == "false"

    def test_int_bounds(self):
        with pytest.raises(ValueError, match="int_below_min"):
            cy.validate_value("agent.history_token_budget", "10")
        with pytest.raises(ValueError, match="int_expect_number"):
            cy.validate_value("agent.history_token_budget", "many")

    def test_float_bounds(self):
        _, v, ft = cy.validate_value("rag.threshold", "0.42")
        assert v == 0.42 and ft == "0.42"
        with pytest.raises(ValueError, match="float_above_max"):
            cy.validate_value("rag.threshold", "1.5")

    def test_cron_quoted(self):
        _, _v, ft = cy.validate_value("rag.digest_cron", "0 4 * * *")
        assert ft == '"0 4 * * *"', "含空格的字符串要加引号，避免歧义"

    @pytest.mark.parametrize("bad", ["a\nb", "a#b"])
    def test_line_injection_rejected(self, bad):
        with pytest.raises(ValueError, match="forbidden"):
            cy.validate_value("rag.digest_cron", bad)

    def test_non_writable_path_rejected(self):
        with pytest.raises(ValueError, match="yaml_key_not_writable"):
            cy.validate_value("archive.keep_days", "1")
        with pytest.raises(ValueError, match="yaml_key_not_writable"):
            cy.validate_value("push.jobs", "x")


class TestWrite:
    def _env(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(SAMPLE, encoding="utf-8")
        monkeypatch.setenv("AGENT_CONFIG", str(cfg))
        monkeypatch.setenv("AGENT_WEB_BACKUP_DIR", str(tmp_path / "bk"))
        return cfg

    def test_write_roundtrip_and_semantics(self, tmp_path, monkeypatch):
        cfg = self._env(tmp_path, monkeypatch)
        spec, norm, ft = cy.validate_value("agent.history_token_budget", "4000")
        result = cy.write_yaml_key(spec, norm, ft)
        assert result.mode == "yaml" and result.old.strip().startswith(
            "history_token_budget: 3000"
        )
        text = cfg.read_text(encoding="utf-8")
        assert "history_token_budget: 4000" in text
        assert "# CJK≈1/字" in text, "注释必须保留"
        assert any((tmp_path / "bk").glob("config.yaml.*.bak"))

    def test_verify_failure_restores(self, tmp_path, monkeypatch):
        cfg = self._env(tmp_path, monkeypatch)
        original = cfg.read_text(encoding="utf-8")
        monkeypatch.setattr(cy, "_verify", lambda *a, **kw: False)
        spec, norm, ft = cy.validate_value("agent.max_iterations", "10")
        with pytest.raises(Exception, match="rolled_back"):
            cy.write_yaml_key(spec, norm, ft)
        assert cfg.read_text(encoding="utf-8") == original

    def test_symlink_fail_closed(self, tmp_path, monkeypatch, symlinks_supported):
        target = tmp_path / "real.yaml"
        target.write_text(SAMPLE, encoding="utf-8")
        link = tmp_path / "config.yaml"
        link.symlink_to(target)
        monkeypatch.setenv("AGENT_CONFIG", str(link))
        spec, norm, ft = cy.validate_value("agent.max_iterations", "10")
        with pytest.raises(ValueError, match="config_file_is_symlink"):
            cy.write_yaml_key(spec, norm, ft)
