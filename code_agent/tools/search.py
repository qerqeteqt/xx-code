"""代码搜索工具（glob_search / grep_search）。

**为什么不用内置的 `FilesystemFileSearchMiddleware`：**

它的纯 Python 回退（本机没有 ripgrep，必走这条）用 `file_path.read_text()`
且**不带 encoding 参数**，Windows 上默认是 cp936，遇到含中文的 UTF-8 文件会抛
`UnicodeDecodeError` 并被 `except (UnicodeDecodeError, PermissionError): continue`
**静默跳过**。实测：纯 ASCII 文件能搜到，含中文的文件一律搜不到 —— 对中文代码库
等于搜索报废。

自研版本相对内置版的改进：
- 显式 `encoding="utf-8", errors="replace"`，中文文件正常检索
- 返回**仓库相对路径的 POSIX 风格**（无前导 `/`），与 filesystem 工具一致
- 输出截断（`MAX_GREP_MATCHES` / `MAX_GLOB_MATCHES`），控制上下文占用
- 跳过二进制文件与常见忽略目录（`.git` / `__pycache__` / `node_modules` 等）
- 经 `safe` 兜底，绝不抛异常
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Iterator

from langchain_core.tools import tool

from code_agent.paths import RepoRoot
from code_agent.tools._util import safe

MAX_GLOB_MATCHES = 200
MAX_GREP_MATCHES = 100
MAX_FILE_BYTES = 10 * 1024 * 1024  # 单个文件超过 10MB 不检索
MAX_LINE_CHARS = 200               # 单行内容展示上限

IGNORE_DIRS = frozenset(
    {
        ".git", "__pycache__", ".idea", ".vscode",
        ".venv", "venv", "env", "node_modules",
        "dist", "build", ".pytest_cache", ".mypy_cache",
        ".ruff_cache", ".ipynb_checkpoints",
    }
)


def _ignored(path: Path) -> bool:
    return any(part in IGNORE_DIRS for part in path.parts)


def _iter_text_files(base: Path) -> Iterator[Path]:
    """遍历 base 下的文本文件，跳过忽略目录、超大文件与二进制文件。"""
    for path in sorted(base.rglob("*")):
        if not path.is_file() or _ignored(path):
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            with path.open("rb") as handle:
                head = handle.read(8192)
        except OSError:
            continue
        if b"\x00" in head:  # 含 NUL 字节，判定为二进制
            continue
        yield path


def build_search_tools(root: RepoRoot) -> list:
    """构建搜索工具集（glob_search / grep_search）。"""

    @tool
    @safe
    def glob_search(pattern: str, path: str = ".") -> str:
        """按文件名模式查找仓库内的文件，返回仓库相对路径列表。

        支持 glob 通配与 `**` 递归，例如 `"**/*.py"`、`"src/**/*.ts"`。
        找不到内容时用 grep_search。

        Args:
            pattern: glob 模式，如 "**/*.py"。
            path: 起始目录（相对仓库根），默认仓库根。
        """
        base = root.resolve(path)
        if not base.exists():
            return f"Error: 路径不存在: {path}"
        if not base.is_dir():
            return f"Error: 不是目录: {path}"

        found = [
            root.relative(p)
            for p in base.glob(pattern)
            if p.is_file() and not _ignored(p)
        ]
        if not found:
            return "No files found"

        found.sort()
        if len(found) > MAX_GLOB_MATCHES:
            extra = len(found) - MAX_GLOB_MATCHES
            found = found[:MAX_GLOB_MATCHES]
            return "\n".join(found) + f"\n... 另有 {extra} 个文件未显示（请缩小模式）"
        return "\n".join(found)

    @tool
    @safe
    def grep_search(pattern: str, path: str = ".", include: str | None = None) -> str:
        """在仓库内按**正则**检索文件内容，返回 `文件:行号:内容` 形式的匹配行。

        用于按内容定位代码，例如查找函数定义、变量名、报错信息。

        Args:
            pattern: 正则表达式，如 "def .*build_llm"。
            path: 检索的文件或目录（相对仓库根），默认整个仓库。
            include: 只检索文件名匹配该 glob 的文件，如 "*.py"；默认全部。
        """
        base = root.resolve(path)
        if base.is_file():
            files: list[Path] = [base]
        elif base.is_dir():
            files = list(_iter_text_files(base))
        else:
            return f"Error: 路径不存在: {path}"

        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return f"Error: 正则表达式无效: {exc}"

        matches: list[str] = []
        truncated = False
        for file in files:
            if include and not fnmatch.fnmatch(file.name, include):
                continue
            text = file.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    shown = line.strip()[:MAX_LINE_CHARS]
                    matches.append(f"{root.relative(file)}:{lineno}:{shown}")
                    if len(matches) >= MAX_GREP_MATCHES:
                        truncated = True
                        break
            if truncated:
                break

        if not matches:
            return "No matches found"
        result = "\n".join(matches)
        if truncated:
            result += f"\n... 匹配数超过 {MAX_GREP_MATCHES} 条已截断（请用更精确的模式）"
        return result

    return [glob_search, grep_search]
