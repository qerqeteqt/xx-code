"""文件工具的单测。"""

from __future__ import annotations

import pytest

from code_agent.paths import RepoRoot
from code_agent.tools.filesystem import build_filesystem_tools


@pytest.fixture
def root(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("line1\nline2\nline3\n", encoding="utf-8")
    return RepoRoot(tmp_path)


@pytest.fixture
def tools(root):
    return {t.name: t for t in build_filesystem_tools(root, allow_write=True)}


def test_read_only_mode_excludes_write_tools(root):
    names = {t.name for t in build_filesystem_tools(root, allow_write=False)}
    assert names == {"list_dir", "read_file"}


def test_list_dir(tools):
    out = tools["list_dir"].invoke({"path": "."})
    assert "src/" in out


def test_list_dir_missing_returns_error(tools):
    out = tools["list_dir"].invoke({"path": "nope"})
    assert out.startswith("Error")


def test_read_file_full(tools):
    out = tools["read_file"].invoke({"path": "src/main.py"})
    assert "共 3 行" in out
    assert "line1" in out and "line3" in out


def test_read_file_with_range(tools):
    out = tools["read_file"].invoke({"path": "src/main.py", "start_line": 2, "end_line": 3})
    assert "line2" in out and "line3" in out
    assert "line1" not in out


def test_read_file_start_beyond_eof(tools):
    out = tools["read_file"].invoke({"path": "src/main.py", "start_line": 99})
    assert out.startswith("Error")


def test_read_file_on_directory_returns_error(tools):
    out = tools["read_file"].invoke({"path": "src"})
    assert out.startswith("Error")


def test_read_file_truncates_long_file(tmp_path):
    (tmp_path / "big.txt").write_text(
        "\n".join(f"L{i}" for i in range(1, 1001)), encoding="utf-8"
    )
    tools = {t.name: t for t in build_filesystem_tools(RepoRoot(tmp_path), allow_write=True)}
    out = tools["read_file"].invoke({"path": "big.txt"})
    assert "截断" in out
    assert "L999" not in out          # 尾部内容被截掉


def test_write_then_read(tools):
    out = tools["write_file"].invoke({"path": "new/dir/f.txt", "content": "hello\n"})
    assert "已写入" in out
    assert "hello" in tools["read_file"].invoke({"path": "new/dir/f.txt"})


def test_edit_file_replaces_unique_match(tools):
    out = tools["edit_file"].invoke(
        {"path": "src/main.py", "old_string": "line2", "new_string": "LINE-TWO"}
    )
    assert "已修改" in out
    assert "LINE-TWO" in tools["read_file"].invoke({"path": "src/main.py"})


def test_edit_file_missing_old_string(tools):
    out = tools["edit_file"].invoke(
        {"path": "src/main.py", "old_string": "nope", "new_string": "x"}
    )
    assert out.startswith("Error") and "未找到" in out


def test_edit_file_rejects_ambiguous_match(tools):
    tools["write_file"].invoke({"path": "dup.txt", "content": "aa\naa\n"})
    out = tools["edit_file"].invoke({"path": "dup.txt", "old_string": "aa", "new_string": "bb"})
    assert out.startswith("Error") and "不唯一" in out


def test_escape_is_blocked(tools):
    out = tools["read_file"].invoke({"path": "../secret.txt"})
    assert out.startswith("Error")


def test_read_file_accepts_search_style_virtual_path(tools):
    """glob_search 返回形如 "/src/main.py" 的虚拟路径，read_file 必须能直接使用。"""
    out = tools["read_file"].invoke({"path": "/src/main.py"})
    assert not out.startswith("Error")
    assert "line1" in out


def test_write_escape_is_blocked(tools):
    out = tools["write_file"].invoke({"path": "../evil.txt", "content": "x"})
    assert out.startswith("Error")
