import os
import tempfile
from pathlib import Path

import pytest

from agentcore.skills.manifest import SkillManifest
from agentcore.skills.installer import SkillInstaller


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
        with open(bad, "w", encoding="utf-8") as f:
            f.write("name: INVALID\n")
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
