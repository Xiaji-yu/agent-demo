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

    def __init__(self, *, add_error: Exception | None = None, delete_error: Exception | None = None):
        self.calls: list[tuple] = []
        self._add_error = add_error
        self._delete_error = delete_error

    async def add_file(self, path, kind=None):  # noqa: ARG002
        self.calls.append(("add", path))
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
