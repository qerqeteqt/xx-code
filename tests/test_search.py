"""搜索工具的单测。

重点回归：内置 `FilesystemFileSearchMiddleware` 在本机（Windows / cp936）会
**静默跳过含中文的 UTF-8 文件**，自研版本必须能正常检索。
"""

from __future__ import annotations

import pytest

from code_agent.paths import RepoRoot
from code_agent.tools.search import MAX_GREP_MATCHES, build_search_tools


@pytest.fixture
def root(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text(
        "def foo():\n    return 1\n", encoding="utf-8"
    )
    (tmp_path / "src" / "cn.py").write_text(
        "# 中文注释：这是一个函数\ndef bar():\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "README.md").write_text("# 说明\n中文内容 NEEDLE_CN\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("NEEDLE_IN_GIT\n", encoding="utf-8")
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01NEEDLE_BIN")
    return RepoRoot(tmp_path)


@pytest.fixture
def tools(root):
    return {t.name: t for t in build_search_tools(root)}


def test_search_tool_names(root):
    assert {t.name for t in build_search_tools(root)} == {"glob_search", "grep_search"}


def test_glob_finds_python_files(tools):
    out = tools["glob_search"].invoke({"pattern": "**/*.py"})
    assert "src/main.py" in out
    assert "src/cn.py" in out


def test_glob_returns_posix_relative_paths(tools):
    out = tools["glob_search"].invoke({"pattern": "**/*.py"})
    for line in out.splitlines():
        assert not line.startswith("/"), line
        assert "\\" not in line, line


def test_glob_no_match(tools):
    assert tools["glob_search"].invoke({"pattern": "**/*.rs"}) == "No files found"


def test_grep_finds_ascii_content(tools):
    out = tools["grep_search"].invoke({"pattern": "def foo"})
    assert "src/main.py:1" in out


def test_grep_finds_text_in_chinese_file(tools):
    """回归：内置中间件在本机会搜不到含中文的 UTF-8 文件。"""
    out = tools["grep_search"].invoke({"pattern": "NEEDLE_CN"})
    assert out != "No matches found"
    assert "README.md" in out


def test_grep_matches_chinese_pattern(tools):
    out = tools["grep_search"].invoke({"pattern": "中文注释"})
    assert "src/cn.py" in out


def test_grep_skips_ignored_dirs(tools):
    assert tools["grep_search"].invoke({"pattern": "NEEDLE_IN_GIT"}) == "No matches found"


def test_grep_skips_binary_files(tools):
    assert tools["grep_search"].invoke({"pattern": "NEEDLE_BIN"}) == "No matches found"


def test_grep_include_filter(tools):
    out = tools["grep_search"].invoke({"pattern": "def ", "include": "*.py"})
    assert "src/main.py" in out
    assert "README.md" not in out


def test_grep_invalid_regex_returns_error(tools):
    out = tools["grep_search"].invoke({"pattern": "([unclosed"})
    assert out.startswith("Error")


def test_grep_truncates_long_results(tmp_path):
    (tmp_path / "many.txt").write_text("\n".join("HIT" for _ in range(500)), encoding="utf-8")
    tools = {t.name: t for t in build_search_tools(RepoRoot(tmp_path))}
    out = tools["grep_search"].invoke({"pattern": "HIT"})
    assert "已截断" in out
    assert out.count("many.txt") == MAX_GREP_MATCHES


def test_grep_escape_is_blocked(tools):
    out = tools["grep_search"].invoke({"pattern": "x", "path": "../"})
    assert out.startswith("Error")


def test_grep_missing_path_returns_error(tools):
    out = tools["grep_search"].invoke({"pattern": "x", "path": "nope"})
    assert out.startswith("Error")
