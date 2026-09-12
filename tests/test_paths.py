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


def test_leading_slash_is_treated_as_absolute_and_rejected(repo):
    """以 / 开头的路径按绝对路径处理（仓库内搜索工具返回的是相对路径）。"""
    with pytest.raises(PathEscapeError):
        repo.resolve("/src/main.py")


def test_symlink_pointing_outside_is_rejected(repo, tmp_path):
    """指向仓库外的符号链接必须被拒绝（创建符号链接在 Windows 上可能需要权限）。"""
    outside = tmp_path.parent / "outside_target.txt"
    outside.write_text("secret\n", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境无法创建符号链接")
    with pytest.raises(PathEscapeError):
        repo.resolve("link.txt")
