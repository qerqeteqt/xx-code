"""路径围栏的单测 —— 这是安全底线，必须守住。"""

from __future__ import annotations

import pytest

from code_agent.paths import PathEscapeError, RepoRoot


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    return RepoRoot(tmp_path)


def test_init_rejects_missing_path(tmp_path):
    with pytest.raises(FileNotFoundError):
        RepoRoot(tmp_path / "not-exist")


def test_init_rejects_file_path(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        RepoRoot(f)


def test_resolve_relative(repo, tmp_path):
    assert repo.resolve("src/main.py") == (tmp_path / "src" / "main.py").resolve()


def test_resolve_root(repo, tmp_path):
    assert repo.resolve(".") == tmp_path.resolve()


def test_resolve_absolute_inside(repo, tmp_path):
    assert repo.resolve(tmp_path / "src") == (tmp_path / "src").resolve()


def test_escape_via_parent(repo):
    with pytest.raises(PathEscapeError):
        repo.resolve("../outside.txt")


def test_escape_via_nested_parent(repo):
    with pytest.raises(PathEscapeError):
        repo.resolve("src/../../outside.txt")


def test_escape_via_absolute_outside(repo, tmp_path):
    with pytest.raises(PathEscapeError):
        repo.resolve(tmp_path.parent / "zzz.txt")


def test_relative_posix(repo, tmp_path):
    assert repo.relative(tmp_path / "src" / "main.py") == "src/main.py"


def test_resolve_virtual_path_from_search_middleware(repo, tmp_path):
    """/src/main.py 是搜索中间件返回的虚拟路径，应被当作仓库内路径。"""
    assert repo.resolve("/src/main.py") == (tmp_path / "src" / "main.py").resolve()


def test_virtual_path_cannot_escape(repo):
    with pytest.raises(PathEscapeError):
        repo.resolve("/../outside.txt")
