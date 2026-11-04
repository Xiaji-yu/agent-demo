"""评审产物自检脚本（check_review_docs.py）的单元守卫。

M8（REVIEW-6867995..e079b8d）：_changelog_sections 旧实现取
``text[m.start():]`` 到文件尾，段正文被历史段污染——「该版本段没提到键名」
变成「从最新段起全文都没提到」，发布漏记门禁在「键名曾于历史段出现」的形态
下静默漏报。本文件锁住「段正文裁到下一个段标题」的行为。
"""

import importlib.util
from pathlib import Path


def _load():
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "check_review_docs", root / "scripts" / "check_review_docs.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_changelog_sections_cut_at_next_heading(tmp_path, monkeypatch):
    mod = _load()
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "# Changelog\n\n"
        "## [2.0.0] - 2026-11-04\n"
        "新版本说明 AGENT_NEW_KEY\n\n"
        "## [1.0.0] - 2026-01-01\n"
        "旧版本补记 AGENT_NEW_KEY\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "CHANGELOG", changelog)
    secs = mod._changelog_sections()
    assert set(secs) == {"2.0.0", "1.0.0"}
    assert "AGENT_NEW_KEY" in secs["2.0.0"]
    assert "## [1.0.0]" not in secs["2.0.0"], (
        "段正文不得包含更老的段——旧实现裁到文件尾，判据被历史段污染（M8）"
    )
    assert "AGENT_NEW_KEY" in secs["1.0.0"]


def test_changelog_last_section_runs_to_eof(tmp_path, monkeypatch):
    """最老的段没有「下一个标题」，正文到文件尾。"""
    mod = _load()
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "## [2.0.0] - 2026-11-04\n新版\n\n## [1.0.0] - 2026-01-01\n旧版正文\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "CHANGELOG", changelog)
    secs = mod._changelog_sections()
    assert secs["1.0.0"].rstrip().endswith("旧版正文")


def test_changelog_missing_file_is_empty(tmp_path, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "CHANGELOG", tmp_path / "nope.md")
    assert mod._changelog_sections() == {}
