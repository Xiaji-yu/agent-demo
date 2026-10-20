"""archive_skills（extract / pack_zip）回归测试。

来源：REVIEW-6745556..d730827 H1 的方案 B 落地——白名单退役外部 zip/unzip/tar，
解压/打包改 Python 标准库逐条目手写。安全契约（每条都有对应用例，改坏实现必须
失败）：
- 产物一律 0600/0700（包内 0777 不可表达——「内容→执行位」转化器根除）
- 两遍法：坏条目/超限在任何字节落盘前拒绝（零落盘断言）
- 拒穿越/绝对路径/.git*/链接与设备条目（zip 与 tar 双侧）
- 条目数/总大小上限（zip bomb 防护）
- 条目名回显过围栏；仅 superuser + medium+ 双层
"""

import io
import os
import stat
import tarfile
import zipfile
from pathlib import Path

import pytest

import agentcore.skills.archive_skills as ar
from agentcore.skills.archive_skills import register_archive_skills
from agentcore.skills.permissions import PermissionChecker
from agentcore.skills.registry import SkillRegistry


@pytest.fixture
def registry(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPERUSERS", '["10000"]')
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "ws"))
    reg = SkillRegistry(permission_checker=PermissionChecker(superusers={"10000"}))
    register_archive_skills(reg)
    return reg


def _ws(registry, tmp_path) -> Path:
    root = tmp_path / "ws"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _make_zip(path: Path, entries: dict[str, bytes], mode: int = 0o777) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zi = zipfile.ZipInfo(name)
            zi.external_attr = (0o100000 | mode) << 16
            zf.writestr(zi, data)


def _make_tar(
    path: Path,
    entries: dict[str, bytes],
    *,
    mode: int = 0o777,
    compress: str = "",
    links: list[tuple[str, str, bool]] | None = None,
) -> None:
    suffix = {"gz": "w:gz", "bz2": "w:bz2", "xz": "w:xz", "": "w"}[compress]
    with tarfile.open(path, suffix) as tf:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            tf.addfile(info, io.BytesIO(data))
        for name, target, is_hard in links or []:
            info = tarfile.TarInfo(name)
            if is_hard:
                info.type = tarfile.LNKTYPE
                info.linkname = target
            else:
                info.type = tarfile.SYMTYPE
                info.linkname = target
            tf.addfile(info)


class TestExtractPositive:
    @pytest.mark.asyncio
    async def test_zip_files_and_dirs_zero_exec_bits(self, registry, tmp_path):
        root = _ws(registry, tmp_path)
        _make_zip(root / "pkg.zip", {"a.sh": b"#!/bin/sh\n", "sub/b.txt": b"hi"})
        out = await registry.execute(
            "extract",
            archive="pkg.zip",
            user_id="10000",
        )
        assert "2 个文件" in out, out
        dest = root / "pkg_extracted"
        assert (dest / "a.sh").read_bytes() == b"#!/bin/sh\n"
        assert stat.S_IMODE((dest / "a.sh").stat().st_mode) == 0o600
        assert stat.S_IMODE((dest / "sub").stat().st_mode) == 0o700
        # 围栏在位（条目名回显是不可信内容）
        assert "解压结果开始" in out and "解压结果结束" in out

    @pytest.mark.asyncio
    @pytest.mark.parametrize("compress", ["", "gz", "bz2", "xz"])
    async def test_tar_formats(self, registry, tmp_path, compress):
        root = _ws(registry, tmp_path)
        name = "p.tar" + ("." + compress if compress else "")
        _make_tar(root / name, {"run.sh": b"x", "d/f": b"y"}, compress=compress)
        out = await registry.execute(
            "extract",
            archive=name,
            user_id="10000",
        )
        assert "2 个文件" in out, out
        dest = root / "p_extracted"
        assert stat.S_IMODE((dest / "run.sh").stat().st_mode) == 0o600
        assert (dest / "d" / "f").read_bytes() == b"y"

    @pytest.mark.asyncio
    async def test_explicit_dest(self, registry, tmp_path):
        root = _ws(registry, tmp_path)
        _make_zip(root / "pkg.zip", {"a.txt": b"1"})
        out = await registry.execute(
            "extract",
            archive="pkg.zip",
            dest="out/dir",
            user_id="10000",
        )
        assert (root / "out" / "dir" / "a.txt").exists(), out


class TestZipLiarFileSize:
    """写盘期复核：包声明 file_size 小于实际字节时必须中止（L6）。"""

    def test_copy_limited_rejects_actual_over_declared(self):
        with pytest.raises(OSError, match="实际数据超过声明"):
            ar._copy_limited(io.BytesIO(b"hello"), io.BytesIO(), 1, 100)

    def test_copy_limited_rejects_total_budget(self):
        with pytest.raises(OSError, match="总大小超过上限"):
            ar._copy_limited(io.BytesIO(b"hello"), io.BytesIO(), 10, 2)

    def test_extract_aborts_on_liar_archive(self, tmp_path, monkeypatch):
        """端到端：zipfile 自己也可能先抛 BadZipFile；两者都算拒绝。"""
        archive = tmp_path / "a.zip"
        _make_zip(archive, {"a.txt": b"hello"})
        original = zipfile.ZipFile.infolist

        def liar(self):
            infos = original(self)
            for info in infos:
                info.file_size = 1
            return infos

        monkeypatch.setattr(zipfile.ZipFile, "infolist", liar)
        with pytest.raises((OSError, zipfile.BadZipFile)):
            ar._extract_sync(tmp_path, archive, tmp_path / "out")


class TestExtractRefused:
    """两遍法核心：拒绝发生在任何字节落盘之前。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "entry", ["../evil.sh", "/abs/evil.sh", "a\\..\\..\\b", "sub/.git/config"]
    )
    async def test_zip_bad_entries_zero_landing(self, registry, tmp_path, entry):
        root = _ws(registry, tmp_path)
        _make_zip(root / "bad.zip", {entry: b"x"})
        out = await registry.execute(
            "extract",
            archive="bad.zip",
            user_id="10000",
        )
        assert "拒绝" in out, out
        assert not (root / "bad_extracted").exists(), "两遍法：预扫描拒绝必须零落盘"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("entry", ["../evil.sh", "/abs/evil.sh", "sub/.git/config"])
    async def test_tar_bad_entries(self, registry, tmp_path, entry):
        root = _ws(registry, tmp_path)
        _make_tar(root / "bad.tar", {entry: b"x"})
        out = await registry.execute(
            "extract",
            archive="bad.tar",
            user_id="10000",
        )
        assert "拒绝" in out, out
        assert not (root / "bad_extracted").exists()

    @pytest.mark.asyncio
    async def test_zip_symlink_entry_refused(self, registry, tmp_path):
        root = _ws(registry, tmp_path)
        with zipfile.ZipFile(root / "link.zip", "w") as zf:
            zi = zipfile.ZipInfo("lnk")
            zi.create_system = 3  # Unix
            zi.external_attr = 0o120777 << 16  # S_IFLNK
            zf.writestr(zi, "/etc/passwd")
        out = await registry.execute(
            "extract",
            archive="link.zip",
            user_id="10000",
        )
        assert "拒绝" in out and "符号链接" in out, out
        assert not (root / "link_extracted").exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "link", [("lnk", "/etc/passwd", False), ("hlnk", "run.sh", True)]
    )
    async def test_tar_link_entries_refused(self, registry, tmp_path, link):
        root = _ws(registry, tmp_path)
        _make_tar(
            root / "links.tar",
            {"run.sh": b"x"},
            links=[(link[0], link[1], link[2])],
        )
        out = await registry.execute(
            "extract",
            archive="links.tar",
            user_id="10000",
        )
        assert "拒绝" in out, out
        assert not (root / "links_extracted").exists()

    @pytest.mark.asyncio
    async def test_entry_count_cap(self, registry, tmp_path, monkeypatch):
        root = _ws(registry, tmp_path)
        monkeypatch.setattr(ar, "_max_entries", lambda: 2)
        _make_zip(root / "many.zip", {f"f{i}.txt": b"x" for i in range(3)})
        out = await registry.execute(
            "extract",
            archive="many.zip",
            user_id="10000",
        )
        assert "拒绝" in out and "上限" in out, out
        assert not (root / "many_extracted").exists()

    @pytest.mark.asyncio
    async def test_total_size_cap_zip_declared(self, registry, tmp_path, monkeypatch):
        root = _ws(registry, tmp_path)
        monkeypatch.setattr(ar, "_max_total_bytes", lambda: 1024)
        _make_zip(root / "big.zip", {"big.bin": b"x" * 4096})
        out = await registry.execute(
            "extract",
            archive="big.zip",
            user_id="10000",
        )
        assert "拒绝" in out and "上限" in out, out
        assert not (root / "big_extracted").exists()

    @pytest.mark.asyncio
    async def test_total_size_cap_tar(self, registry, tmp_path, monkeypatch):
        root = _ws(registry, tmp_path)
        monkeypatch.setattr(ar, "_max_total_bytes", lambda: 1024)
        _make_tar(root / "big.tar", {"big.bin": b"x" * 4096})
        out = await registry.execute(
            "extract",
            archive="big.tar",
            user_id="10000",
        )
        assert "拒绝" in out, out
        assert not (root / "big_extracted").exists()

    @pytest.mark.asyncio
    async def test_archive_outside_workspace(self, registry, tmp_path):
        _ws(registry, tmp_path)
        outside = tmp_path / "outside.zip"
        _make_zip(outside, {"a.txt": b"1"})
        out = await registry.execute(
            "extract",
            archive=str(outside),
            user_id="10000",
        )
        assert "拒绝" in out, out

    @pytest.mark.asyncio
    async def test_dest_outside_or_root(self, registry, tmp_path):
        root = _ws(registry, tmp_path)
        _make_zip(root / "pkg.zip", {"a.txt": b"1"})
        for dest in ("../evil", "/etc", "."):
            out = await registry.execute(
                "extract",
                archive="pkg.zip",
                dest=dest,
                user_id="10000",
            )
            assert "拒绝" in out, (dest, out)
        assert not (root / "pkg_extracted").exists()

    @pytest.mark.asyncio
    async def test_not_an_archive(self, registry, tmp_path):
        root = _ws(registry, tmp_path)
        (root / "plain.bin").write_bytes(b"not an archive" * 100)
        out = await registry.execute(
            "extract",
            archive="plain.bin",
            user_id="10000",
        )
        assert "拒绝" in out, out

    @pytest.mark.asyncio
    async def test_missing_archive(self, registry, tmp_path):
        _ws(registry, tmp_path)
        out = await registry.execute(
            "extract",
            archive="nope.zip",
            user_id="10000",
        )
        assert "找不到" in out, out


class TestExtractAuth:
    @pytest.mark.asyncio
    async def test_non_superuser_denied(self, registry, tmp_path):
        _ws(registry, tmp_path)
        out = await registry.execute(
            "extract",
            archive="x.zip",
            user_id="20002",
        )
        # registry 对无权限者与 unknown 同文案（防探测），不会出现「无权限」明文
        assert "unknown skill" in out, out

    def test_low_level_not_registered(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SUPERUSERS", '["10000"]')
        monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "ws"))
        monkeypatch.setenv("AGENT_PERMISSION_LEVEL", "low")
        reg = SkillRegistry(permission_checker=PermissionChecker(superusers={"10000"}))
        register_archive_skills(reg)
        names = {s["function"]["name"] for s in reg.get_schemas("10000", None)}
        assert "extract" not in names and "pack_zip" not in names


class TestPackZip:
    @pytest.mark.asyncio
    async def test_pack_records_0600_and_skips_symlinks(self, registry, tmp_path):
        root = _ws(registry, tmp_path)
        (root / "src").mkdir()
        (root / "src" / "a.txt").write_text("hello")
        (root / "src" / "d").mkdir()
        (root / "src" / "d" / "b.txt").write_text("world")
        os.symlink("/etc/passwd", root / "src" / "evil")
        out = await registry.execute(
            "pack_zip",
            source="src",
            name="bundle.zip",
            user_id="10000",
        )
        assert "打包完成" in out and "符号链接" in out, out
        zp = root / "bundle.zip"
        with zipfile.ZipFile(zp) as zf:
            names = set(zf.namelist())
            attrs = {i.filename: i.external_attr >> 16 for i in zf.infolist()}
            content = zf.read("a.txt")
        assert names == {"a.txt", "d/b.txt"}
        assert all((m & 0o777) == 0o600 for m in attrs.values())
        assert content == b"hello"

    @pytest.mark.asyncio
    async def test_name_injection_refused(self, registry, tmp_path):
        _ws(registry, tmp_path)
        for name in ("../evil.zip", "a/b.zip", "x\\y.zip", "..", ""):
            out = await registry.execute(
                "pack_zip",
                source=".",
                name=name,
                user_id="10000",
            )
            assert "拒绝" in out, (name, out)

    @pytest.mark.asyncio
    async def test_source_outside_refused(self, registry, tmp_path):
        root = _ws(registry, tmp_path)
        out = await registry.execute(
            "pack_zip",
            source="..",
            name="b.zip",
            user_id="10000",
        )
        assert "拒绝" in out, out
        assert not (root / "b.zip").exists()


class TestCompressionToolsRemoved:
    """白名单退役：外部 zip/unzip/tar 任何参数形态一律拒绝（H1 方案 B）。"""

    def test_permitted_rejects_all(self, tmp_path):
        from agentcore.workspace.runner import permitted

        cases = [
            ("unzip", ["-o", "-d", "out", "a.zip"]),
            ("unzip", ["-l", "a.zip"]),
            ("tar", ["-xf", "a.tar", "-C", "out"]),
            ("tar", ["-xzf", "a.tgz"]),
            ("zip", ["-r", "out.zip", "dir"]),
            ("zip", []),
        ]
        for exe, args in cases:
            ok, reason = permitted(exe, args, root=tmp_path)
            assert not ok, (exe, args)
            assert "不在白名单" in reason or "仅允许" in reason, (exe, reason)

    def test_low_still_rejects_curl(self, tmp_path):
        from agentcore.workspace.runner import permitted

        ok, reason = permitted(
            "curl", ["https://example.com"], root=tmp_path, readonly_only=True
        )
        assert not ok and "low" in reason
