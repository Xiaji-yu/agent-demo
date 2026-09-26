import os
import tempfile
from pathlib import Path

import pytest

from agentcore.skills.installer import SkillInstaller
from agentcore.skills.manifest import SkillManifest


class TestSkillInstaller:
    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield d

    @pytest.fixture
    def installer(self, tmp_dir):
        return SkillInstaller(skills_dir=Path(tmp_dir))

    def test_install_and_list(self, installer):
        manifest = SkillManifest(
            name="tmp",
            description="tmp",
            type="prompt",
            prompt="p",
            parameters=[],
            permission="public",
        )
        installer.install(manifest)
        assert len(installer.list_manifests()) == 1
        assert installer.get("tmp").name == "tmp"

    def test_uninstall(self, installer):
        manifest = SkillManifest(
            name="tmp",
            description="tmp",
            type="prompt",
            prompt="p",
            parameters=[],
            permission="public",
        )
        installer.install(manifest)
        assert installer.uninstall("tmp") is True
        assert installer.get("tmp") is None
        assert installer.uninstall("tmp") is False

    def test_invalid_manifest_skipped(self, installer, tmp_dir):
        bad = os.path.join(tmp_dir, "bad.yaml")
        Path(bad).write_text("name: INVALID\n", encoding="utf-8")
        manifest = SkillManifest(
            name="good",
            description="good",
            type="prompt",
            prompt="p",
            parameters=[],
            permission="public",
        )
        installer.install(manifest)
        assert len(installer.list_manifests()) == 1
        assert installer.get("good").name == "good"


class TestUninstallNameGuard:
    """uninstall 与 install 同一名字契约：../foo 不得逃出 skills_dir（重审 P2）。"""

    def test_refuses_path_escape(self, tmp_path):
        victim = tmp_path / "outside.yaml"
        victim.write_text("harmless", encoding="utf-8")
        inst = SkillInstaller(skills_dir=tmp_path / "skills")
        assert inst.uninstall("../outside") is False
        assert victim.exists()

    def test_refuses_slash_and_empty(self, tmp_path):
        inst = SkillInstaller(skills_dir=tmp_path / "skills")
        assert inst.uninstall("a/b") is False
        assert inst.uninstall("") is False
        assert inst.uninstall("演示") is False

    def test_valid_name_still_uninstalls(self, tmp_path):
        skills = tmp_path / "skills"
        skills.mkdir()
        (skills / "weather.yaml").write_text("name: weather", encoding="utf-8")
        inst = SkillInstaller(skills_dir=skills)
        assert inst.uninstall("weather") is True
        assert not (skills / "weather.yaml").exists()
