"""交互模式的会话记忆：记录"每个仓库最近一次会话"（纯文件读写，不联网）。"""

from __future__ import annotations

import pytest

from code_agent import cli


@pytest.fixture
def session_file(tmp_path, monkeypatch):
    """把会话记录文件重定向到临时目录，别污染项目。"""
    path = tmp_path / "sessions.json"
    monkeypatch.setattr(cli, "_session_path", lambda: path)
    return path


def test_unknown_repo_has_no_session(session_file):
    assert cli._load_last_session("D:/repo") is None


def test_save_then_load(session_file):
    cli._save_last_session("D:/repo", "abc123")
    assert cli._load_last_session("D:/repo") == "abc123"


def test_sessions_are_tracked_per_repo(session_file):
    cli._save_last_session("D:/a", "id-a")
    cli._save_last_session("D:/b", "id-b")
    assert cli._load_last_session("D:/a") == "id-a"
    assert cli._load_last_session("D:/b") == "id-b"


def test_overwrite_keeps_latest(session_file):
    cli._save_last_session("D:/a", "old")
    cli._save_last_session("D:/a", "new")
    assert cli._load_last_session("D:/a") == "new"


def test_corrupt_file_recovers(session_file):
    """记录文件损坏时不该崩，应当作空并在下次写入时恢复。"""
    session_file.write_text("{这不是 json", encoding="utf-8")
    assert cli._load_last_session("D:/repo") is None
    cli._save_last_session("D:/repo", "fresh")
    assert cli._load_last_session("D:/repo") == "fresh"
