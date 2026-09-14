"""scripts/ingest_kb_samples.py 的回归测试。

重点锁住评审 REVIEW-f6dffcc..08006e7.md 的 H1：``--replace`` 曾经**先删旧来源、
再写新来源**，add_file 失败时旧数据被永久删除（知识库直接少一份语料）。
现在必须「先写新、成功后再删旧」。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "ingest_kb_samples_cli", ROOT / "scripts" / "ingest_kb_samples.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_script()


class _FakeKB:
    """只记录调用顺序的假知识库。"""

    def __init__(
        self,
        *,
        add_error: Exception | None = None,
        delete_error: Exception | None = None,
    ):
        self.calls: list[tuple] = []
        self._add_error = add_error
        self._delete_error = delete_error

    async def add_file(self, path, name=None, kind=None):  # noqa: ARG002
        self.calls.append(("add", path, name))
        if self._add_error is not None:
            raise self._add_error
        return {"chunks": 3, "chunks_total": 3, "dropped": 0}

    async def delete_source(self, source_id):
        self.calls.append(("delete", source_id))
        if self._delete_error is not None:
            raise self._delete_error
        return 1


def _write_sample(tmp_path: Path, text: str = "新语料内容\n") -> Path:
    p = tmp_path / "sample.md"
    p.write_text(text, encoding="utf-8")
    return p


def _old_source(mod, path: Path, *, digest: str | None, sid: str = "7") -> dict:
    meta = {} if digest is None else {"sha256": digest}
    return {"id": sid, "name": path.name, "kind": "sample", "meta": meta}


@pytest.mark.asyncio
async def test_add_failure_keeps_old_source(mod, tmp_path, capsys):
    """H1 核心：写入失败时必须保留旧来源，且**不得**调用 delete_source。"""
    path = _write_sample(tmp_path)
    kb = _FakeKB(add_error=RuntimeError("embedding 服务挂了"))
    old = _old_source(mod, path, digest="stale-digest")

    outcome = await mod._process_file(kb, path, old, replace=True, dry_run=False)

    assert outcome == {"failed": 1}
    assert [c[0] for c in kb.calls] == ["add"], "旧来源被删了——H1 回归"
    out = capsys.readouterr().out
    assert "旧来源 #7 未删除" in out


@pytest.mark.asyncio
async def test_replace_writes_new_before_deleting_old(mod, tmp_path):
    """替换顺序必须是 add → delete（旧顺序是 delete → add）。"""
    path = _write_sample(tmp_path)
    kb = _FakeKB()
    old = _old_source(mod, path, digest="stale-digest")

    outcome = await mod._process_file(kb, path, old, replace=True, dry_run=False)

    assert [c[0] for c in kb.calls] == ["add", "delete"]
    assert kb.calls[1] == ("delete", "7")
    assert outcome.get("imported") == 1


@pytest.mark.asyncio
async def test_delete_failure_only_warns_and_counts_as_imported(mod, tmp_path, capsys):
    """新来源已入库后删旧失败：只是留下重复，不应记成失败、也不应抛异常。"""
    path = _write_sample(tmp_path)
    kb = _FakeKB(delete_error=RuntimeError("db timeout"))
    old = _old_source(mod, path, digest="stale-digest")

    outcome = await mod._process_file(kb, path, old, replace=True, dry_run=False)

    assert outcome.get("imported") == 1
    assert "failed" not in outcome
    assert "删除失败" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_new_file_never_touches_delete(mod, tmp_path):
    path = _write_sample(tmp_path)
    kb = _FakeKB()

    outcome = await mod._process_file(kb, path, None, replace=False, dry_run=False)

    assert [c[0] for c in kb.calls] == ["add"]
    assert outcome.get("imported") == 1


@pytest.mark.asyncio
async def test_unchanged_content_is_skipped(mod, tmp_path):
    """指纹相同 → 跳过，既不写也不删（幂等）。"""
    from agentcore.rag.ingest import content_digest

    path = _write_sample(tmp_path, "同样的内容\n")
    digest = content_digest(path.read_text(encoding="utf-8"))
    kb = _FakeKB()
    old = _old_source(mod, path, digest=digest)

    outcome = await mod._process_file(kb, path, old, replace=True, dry_run=False)

    assert outcome == {"skipped": 1}
    assert kb.calls == []


@pytest.mark.asyncio
async def test_without_replace_changed_file_only_pends(mod, tmp_path):
    """内容变化但没给 --replace：只提示，绝不删数据。"""
    path = _write_sample(tmp_path)
    kb = _FakeKB()
    old = _old_source(mod, path, digest="stale-digest")

    outcome = await mod._process_file(kb, path, old, replace=False, dry_run=False)

    assert outcome == {"changed_pending": 1}
    assert kb.calls == []


@pytest.mark.asyncio
async def test_dry_run_replace_touches_nothing(mod, tmp_path):
    path = _write_sample(tmp_path)
    kb = _FakeKB()
    old = _old_source(mod, path, digest="stale-digest")

    outcome = await mod._process_file(kb, path, old, replace=True, dry_run=True)

    assert outcome == {"changed_pending": 1}
    assert kb.calls == []


def test_latest_by_name_prefers_newest(mod):
    """list_sources 最新在前；同名多条时必须取第一条（最新的）。"""
    sources = [
        {"id": "9", "name": "a.md", "meta": {"sha256": "new"}},
        {"id": "3", "name": "a.md", "meta": {"sha256": "old"}},
        {"id": "4", "name": "b.md", "meta": {}},
        {"id": "5", "name": "", "meta": {}},
    ]
    by_name = mod._latest_by_name(sources)

    assert by_name["a.md"]["id"] == "9"
    assert by_name["b.md"]["id"] == "4"
    assert "" not in by_name


# ---------- 大文件自动切块：_process_split_source ----------
def _split_plan(tmp_path: Path) -> dict:
    """构造一个切块计划：big.md → big.md/001.md + big.md/002.md（源文件保留）。"""
    from agentcore.rag.ingest import content_digest

    parent = tmp_path / "big.md"
    parent.write_text("源文件内容（保留在磁盘上）", encoding="utf-8")
    part_dir = tmp_path / "big"
    part_dir.mkdir()
    units = []
    for idx, text in enumerate(["第一块内容", "第二块内容"], start=1):
        part = part_dir / f"{idx:03d}.md"
        part.write_text(text, encoding="utf-8")
        units.append(
            {
                "path": part,
                "name": f"big.md/{part.name}",
                "sha256": content_digest(text),
                "chunks": 1,
            }
        )
    return {"source": parent, "split": True, "dir": part_dir, "units": units}


def _old(name: str, *, digest: str | None, sid: str) -> dict:
    meta = {} if digest is None else {"sha256": digest}
    return {"id": sid, "name": name, "kind": "sample", "meta": meta}


@pytest.mark.asyncio
async def test_split_new_source_imports_all_parts(mod, tmp_path):
    """全新大文件：逐块写入，不需要 --replace，也不删任何来源。"""
    plan = _split_plan(tmp_path)
    kb = _FakeKB()

    outcome = await mod._process_split_source(kb, plan, [], replace=False)

    assert outcome == {"imported": 1, "imported_parts": 2}
    assert [c[0] for c in kb.calls] == ["add", "add"]
    assert [c[2] for c in kb.calls] == ["big.md/001.md", "big.md/002.md"]


@pytest.mark.asyncio
async def test_split_unchanged_is_skipped(mod, tmp_path):
    plan = _split_plan(tmp_path)
    kb = _FakeKB()
    from agentcore.rag.ingest import content_digest

    sources = [
        _old(u["name"], digest=content_digest(u["path"].read_text()), sid=str(i))
        for i, u in enumerate(plan["units"])
    ]

    outcome = await mod._process_split_source(kb, plan, sources, replace=True)

    assert outcome == {"skipped": 1}
    assert kb.calls == []


@pytest.mark.asyncio
async def test_split_changed_without_replace_only_pends(mod, tmp_path):
    """已存在的块内容变化：没有 --replace 时只提示，绝不写也不删。"""
    plan = _split_plan(tmp_path)
    kb = _FakeKB()
    sources = [_old("big.md/001.md", digest="stale", sid="10")]

    outcome = await mod._process_split_source(kb, plan, sources, replace=False)

    assert outcome == {"changed_pending": 1}
    assert kb.calls == []


@pytest.mark.asyncio
async def test_split_replace_writes_new_then_deletes_old(mod, tmp_path):
    """替换：先写入变化的块，成功后再删旧块并清理「整体旧来源」。"""
    plan = _split_plan(tmp_path)
    kb = _FakeKB()
    sources = [
        _old("big.md/001.md", digest="stale", sid="10"),
        _old("big.md", digest=None, sid="7"),
    ]

    outcome = await mod._process_split_source(kb, plan, sources, replace=True)

    assert outcome == {"imported": 1, "imported_parts": 2}
    assert [c[0] for c in kb.calls] == ["add", "add", "delete", "delete"]
    assert [c[2] for c in kb.calls[:2]] == ["big.md/001.md", "big.md/002.md"]
    assert [c[1] for c in kb.calls[2:]] == ["10", "7"]


@pytest.mark.asyncio
async def test_split_add_failure_keeps_all_old_sources(mod, tmp_path, capsys):
    """H1 延伸到切块：任一写入失败就绝不删旧来源。"""
    plan = _split_plan(tmp_path)
    kb = _FakeKB(add_error=RuntimeError("embedding 挂了"))
    sources = [
        _old("big.md/001.md", digest="stale", sid="10"),
        _old("big.md", digest=None, sid="7"),
    ]

    outcome = await mod._process_split_source(kb, plan, sources, replace=True)

    assert outcome == {"failed": 1}
    assert [c[0] for c in kb.calls] == ["add", "add"], "失败路径不得删除旧来源"
    assert "保留全部旧来源" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_split_shrink_supersedes_orphan_tail_chunk(mod, tmp_path):
    """M4 回归：源文件变短（3 块 → 2 块）后，计划外的旧 003.md 必须纳入替换。

    旧实现只按新计划的 unit 名逐条对比——孤儿 003.md 永远看不见，留成
    prune 清不掉的僵尸内容。
    """
    plan = _split_plan(tmp_path)  # 新计划只有 001/002
    kb = _FakeKB()
    sources = [
        _old("big.md/001.md", digest="stale", sid="10"),
        _old("big.md/002.md", digest="stale", sid="11"),
        _old("big.md/003.md", digest="stale", sid="12"),  # 计划外孤儿
        _old("big.md", digest=None, sid="7"),  # 曾作为整体导入
    ]

    outcome = await mod._process_split_source(kb, plan, sources, replace=True)

    assert outcome == {"imported": 1, "imported_parts": 2}
    deleted = sorted(c[1] for c in kb.calls if c[0] == "delete")
    assert deleted == ["10", "11", "12", "7"], "孤儿 003 与整体旧来源都应被替换"


@pytest.mark.asyncio
async def test_split_shadowed_old_source_prevents_false_skip(mod, tmp_path):
    """M7 回归：同名「最新一条」指纹相同、但还压着被遮蔽的旧来源时，
    不得谎报「内容未变，跳过」。"""
    from agentcore.rag.ingest import content_digest

    plan = _split_plan(tmp_path)
    kb = _FakeKB()
    u1, u2 = plan["units"]
    sources = [
        _old(u1["name"], digest=content_digest(u1["path"].read_text()), sid="20"),
        _old(u2["name"], digest=content_digest(u2["path"].read_text()), sid="21"),
        _old(u2["name"], digest="old-shadowed", sid="99"),  # 被遮蔽的旧来源
    ]

    outcome = await mod._process_split_source(kb, plan, sources, replace=False)
    assert outcome == {"changed_pending": 1}, "必须提示需要 --replace，而不是跳过"
    assert kb.calls == []

    outcome = await mod._process_split_source(kb, plan, sources, replace=True)
    # replace 模式：不重复写任何块，只清理被遮蔽的旧来源
    assert [c[0] for c in kb.calls] == ["delete"]
    assert kb.calls[0][1] == "99"
    assert outcome == {}
