"""``agentcore.outbound_files``：最近发出文件登记（识别协议端自回声）。

来源：2026-11-06/07 线上——``upload_private_file`` 发出的文件被协议端以普通
``message`` 事件回传，适配层当成「用户发了空消息」并回了「没收到文字或图片」。
"""

import pytest

from agentcore import outbound_files as of


@pytest.fixture(autouse=True)
def _clean_registry():
    of.reset()
    yield
    of.reset()


def test_note_and_match_by_file_id():
    of.note_sent_file("u1", file_id="fid-1", name="a.txt", size=3)
    assert of.is_recent_sent_file("u1", file_id="fid-1")
    # 不同收件人/不同 id 都不算
    assert not of.is_recent_sent_file("u2", file_id="fid-1")
    assert not of.is_recent_sent_file("u1", file_id="fid-2")


def test_hit_is_consumed():
    """命中即消费：同一文件被重复回传或用户重发时不再无限吞掉。"""
    of.note_sent_file("u1", file_id="fid-1")
    assert of.is_recent_sent_file("u1", file_id="fid-1")
    assert not of.is_recent_sent_file("u1", file_id="fid-1")


def test_name_size_fallback_and_size_mismatch():
    of.note_sent_file("u1", name="report.md", size=12)
    assert not of.is_recent_sent_file("u1", name="report.md", size=11)
    assert of.is_recent_sent_file("u1", name="report.md", size=12)


def test_missing_size_matches_by_name_only():
    """协议端回传没带 file_size 时按文件名兜底（否则回声漏网）。"""
    of.note_sent_file("u1", name="report.md", size=12)
    assert of.is_recent_sent_file("u1", name="report.md")


def test_entry_expires(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(of, "_clock", lambda: now[0])
    of.note_sent_file("u1", file_id="fid-1")
    now[0] += of._TTL_SECONDS + 1
    assert not of.is_recent_sent_file("u1", file_id="fid-1")


def test_registry_is_bounded(monkeypatch):
    monkeypatch.setattr(of, "_MAX_ENTRIES", 3)
    now = [1.0]
    monkeypatch.setattr(of, "_clock", lambda: now[0])
    for i in range(10):
        now[0] += 1
        of.note_sent_file("u1", file_id=f"fid-{i}")
    assert len(of._entries) <= 3


def test_empty_target_is_ignored():
    of.note_sent_file("", file_id="fid-1")
    assert not of.is_recent_sent_file("", file_id="fid-1")
