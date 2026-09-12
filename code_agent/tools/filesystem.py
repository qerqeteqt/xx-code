"""仓库内的文件读写工具。

两条硬性约定：
1. **所有路径都经 `RepoRoot` 围栏**，越界直接返回错误，绝不操作仓库外文件。
2. **工具绝不抛异常**（`_safe` 兜底），错误以字符串返回给模型，让它自我纠正。
   否则异常会冒泡崩掉整个 LangGraph run（`create_agent` 默认只吞参数校验错误）。
3. **输出有源头截断**（`MAX_READ_LINES` 等），避免整块文件塞爆上下文。
"""

from __future__ import annotations

from langchain_core.tools import tool

from code_agent.paths import RepoRoot
from code_agent.tools._util import safe

# 源头截断阈值：这是最便宜、最有效的上下文压缩手段
MAX_READ_LINES = 400
MAX_LIST_ENTRIES = 200


def build_filesystem_tools(root: RepoRoot, *, allow_write: bool) -> list:
    """构建文件工具集。

    Args:
        root: 目标仓库根目录（路径围栏）。
        allow_write: 是否包含写类工具。Explorer 传 False（只读，最小权限）。
    """

    @tool
    @safe
    def list_dir(path: str = ".") -> str:
        """列出仓库内某个目录下的文件与子目录。

        Args:
            path: 相对仓库根的目录路径，默认为仓库根目录。
        """
        target = root.resolve(path)
        if not target.exists():
            return f"Error: 路径不存在: {path}"
        if not target.is_dir():
            return f"Error: 不是目录: {path}"

        entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        if not entries:
            return f"(空目录) {root.relative(target)}"

        lines: list[str] = []
        for i, entry in enumerate(entries):
            if i >= MAX_LIST_ENTRIES:
                lines.append(f"... 还有 {len(entries) - MAX_LIST_ENTRIES} 项未显示")
                break
            if entry.is_dir():
                lines.append(f"{root.relative(entry)}/")
            else:
                try:
                    size = entry.stat().st_size
                except OSError:
                    size = 0
                lines.append(f"{root.relative(entry)}  ({size} B)")
        return "\n".join(lines)

    @tool
    @safe
    def read_file(path: str, start_line: int = 1, end_line: int | None = None) -> str:
        """读取仓库内某个文本文件的内容，返回带行号的文本。

        文件较长时请用 start_line / end_line 分段读取，避免一次读入过多内容。

        Args:
            path: 相对仓库根的文件路径。
            start_line: 起始行号，从 1 开始，默认 1。
            end_line: 结束行号（含），默认读到文件末尾。
        """
        target = root.resolve(path)
        if not target.exists():
            return f"Error: 文件不存在: {path}"
        if not target.is_file():
            return f"Error: 不是文件: {path}"

        text = target.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        total = len(lines)

        start = max(1, int(start_line))
        end = total if end_line is None else min(int(end_line), total)
        if start > total:
            return f"Error: start_line={start} 超出文件总行数 {total}"

        selected = lines[start - 1 : end]
        truncated = len(selected) > MAX_READ_LINES
        if truncated:
            selected = selected[:MAX_READ_LINES]

        shown_end = start + len(selected) - 1
        header = f"# {root.relative(target)}  (共 {total} 行，显示 {start}-{shown_end})"
        if truncated or end < total:
            header += "\n# 提示：文件较长已被截断，可用 start_line/end_line 读取其余部分"

        width = len(str(shown_end))
        body = "\n".join(
            f"{no:>{width}}\t{line}" for no, line in enumerate(selected, start=start)
        )
        return f"{header}\n{body}"

    @tool
    @safe
    def write_file(path: str, content: str) -> str:
        """把内容整体写入文件（已存在则覆盖，不存在则创建，自动建父目录）。

        修改已有文件的局部内容请优先用 edit_file，它更安全。

        Args:
            path: 相对仓库根的文件路径。
            content: 要写入的完整文本内容。
        """
        target = root.resolve(path)
        if target.exists() and target.is_dir():
            return f"Error: 目标是目录，无法写入: {path}"

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"已写入 {root.relative(target)}（{len(content.encode('utf-8'))} 字节）"

    @tool
    @safe
    def edit_file(path: str, old_string: str, new_string: str) -> str:
        """对文件做精确字符串替换（只替换唯一匹配的一处）。

        old_string 必须与文件中已有内容**完全一致**（含缩进和换行），且只能出现一次；
        否则会报错。请先用 read_file 确认原文。这是修改代码的首选方式。

        Args:
            path: 相对仓库根的文件路径。
            old_string: 要被替换掉的原文片段（必须在文件中唯一）。
            new_string: 替换成的新内容。
        """
        if old_string == "":
            return "Error: old_string 不能为空"

        target = root.resolve(path)
        if not target.is_file():
            return f"Error: 文件不存在: {path}"

        text = target.read_text(encoding="utf-8", errors="replace")
        count = text.count(old_string)
        if count == 0:
            return (
                "Error: 未找到 old_string，无法替换。"
                "请先 read_file 确认原文（缩进、空白、换行需完全一致）。"
            )
        if count > 1:
            return (
                f"Error: old_string 在文件中出现 {count} 次，不唯一。"
                "请补充更多上下文使其唯一。"
            )

        target.write_text(text.replace(old_string, new_string, 1), encoding="utf-8")
        return f"已修改 {root.relative(target)}：替换 1 处"

    read_only = [list_dir, read_file]
    writable = [write_file, edit_file]
    return read_only + writable if allow_write else read_only
