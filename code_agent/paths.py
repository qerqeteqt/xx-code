"""目标仓库路径围栏。

所有文件工具都必须经 `RepoRoot` 解析路径，确保操作不会越出仓库根目录。

安全性依赖 `Path.resolve()`：它会展开 `..`、跟随符号链接。因此一个指向仓库外
的符号链接也会被解析到仓库外，从而被 `is_relative_to` 判定为越界并拒绝。
"""

from __future__ import annotations

from pathlib import Path


class PathEscapeError(ValueError):
    """请求的路径越出了仓库根目录。"""


class RepoRoot:
    """封装目标仓库根目录，提供受限的路径解析。"""

    def __init__(self, root: str | Path) -> None:
        candidate = Path(root).expanduser()
        if not candidate.exists():
            raise FileNotFoundError(f"仓库路径不存在: {candidate}")
        if not candidate.is_dir():
            raise NotADirectoryError(f"仓库路径不是目录: {candidate}")
        self.root: Path = candidate.resolve()

    def resolve(self, relative: str | Path) -> Path:
        """把仓库内路径解析为绝对路径；越界则抛 `PathEscapeError`。

        接受相对仓库根的路径（如 `"src/main.py"`）或仓库内的绝对路径。
        以 `/` 开头的路径按**绝对路径**处理，因此通常会被判定越界 ——
        本项目的搜索工具返回的是相对路径，不需要虚拟路径约定。
        """
        raw = Path(str(relative))
        candidate = raw if raw.is_absolute() else self.root / raw
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.root):
            raise PathEscapeError(
                f"路径越出仓库范围: {relative!r} -> {resolved}\n仓库根: {self.root}"
            )
        return resolved

    def relative(self, path: Path | str) -> str:
        """转成相对仓库根的 POSIX 风格路径（供工具输出展示）。"""
        resolved = Path(path).resolve()
        try:
            return resolved.relative_to(self.root).as_posix()
        except ValueError:
            return resolved.as_posix()

    def __str__(self) -> str:
        return str(self.root)

    def __repr__(self) -> str:
        return f"RepoRoot({self.root!r})"
