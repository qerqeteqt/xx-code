"""命令行入口。

M0 阶段只实现 `--check`：验证模型连通性与 tool calling 是否可用。
后续里程碑会补上 --repo 目标仓库、任务执行、HITL 交互等。
"""

from __future__ import annotations

import argparse
import json
import sys


def _fix_console_encoding() -> None:
    """Windows 控制台默认 cp936，强制 UTF-8 输出避免中文/符号报错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def cmd_check(_args: argparse.Namespace) -> int:
    """自检：配置 -> 纯文本调用 -> tool calling。"""
    from code_agent.config import Settings, build_llm

    settings = Settings.from_env()
    print(f"[1/3] 配置加载成功  ({settings.describe()})")

    llm = build_llm(settings)

    reply = llm.invoke("你是连通性测试。请只回复两个字：正常")
    text = reply.content if isinstance(reply.content, str) else str(reply.content)
    print(f"[2/3] 文本调用成功  -> {text.strip()!r}")

    from langchain_core.tools import tool

    @tool
    def echo(text: str) -> str:
        """原样返回传入的文本。"""
        return text

    reply2 = llm.bind_tools([echo]).invoke(
        "请调用 echo 工具，把 text 参数设为 'tool-ok'，不要直接回答。"
    )
    tool_calls = getattr(reply2, "tool_calls", None) or []
    if not tool_calls:
        print("[3/3] 失败：模型没有返回工具调用，该端点可能不支持 tool calling")
        print(f"       原始回复：{str(reply2.content)[:200]}")
        return 2

    print(f"[3/3] tool calling 正常  -> {json.dumps(tool_calls, ensure_ascii=False)}")
    print("\n自检全部通过，环境可用。")
    return 0


def cmd_demo_tools(_args: argparse.Namespace) -> int:
    """在临时目录里演示 M1 的文件与搜索工具（不触碰任何真实文件）。"""
    import tempfile

    from code_agent.paths import RepoRoot
    from code_agent.tools.filesystem import build_filesystem_tools
    from code_agent.tools.search import build_search_tools

    print("=" * 62)
    print("M1 工具演示 —— 全程在临时目录，不会修改任何真实文件")
    print("=" * 62)

    with tempfile.TemporaryDirectory(prefix="code_agent_demo_") as tmp:
        root = RepoRoot(tmp)
        fs = {t.name: t for t in build_filesystem_tools(root, allow_write=True)}
        search = {t.name: t for t in build_search_tools(root)}

        def step(title: str, tool_name: str, **kwargs) -> None:
            print(f"\n### {title}")
            print(f"$ {tool_name}({kwargs})")
            tool = fs.get(tool_name) or search[tool_name]
            print(tool.invoke(kwargs))

        step(
            "写入一个新文件（含中文注释，故意埋一个 bug）",
            "write_file",
            path="src/calc.py",
            content="def add(a, b):\n    # 中文注释：加法\n    return a - b  # 故意的 bug\n",
        )
        step("读取文件（带行号）", "read_file", path="src/calc.py")
        step(
            "精确替换，修掉 bug",
            "edit_file",
            path="src/calc.py",
            old_string="return a - b",
            new_string="return a + b",
        )
        step("再读一次确认", "read_file", path="src/calc.py")
        step("列目录", "list_dir", path=".")
        step("glob 查找 py 文件", "glob_search", pattern="**/*.py")
        step(
            "grep 搜中文（内置中间件在这里会失效）",
            "grep_search",
            pattern="中文注释",
        )
        step("grep 按内容定位函数", "grep_search", pattern="def add")
        step("越界访问被拦截", "read_file", path="../../etc/passwd")

    print("\n演示结束：以上文件都在临时目录，已随临时目录一起删除。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code_agent",
        description="基于 LangGraph 的多 Agent 编码助手",
    )
    parser.add_argument("--repo", metavar="PATH", help="目标仓库路径（M3 起使用）")
    parser.add_argument(
        "--check", action="store_true", help="自检：验证模型连通与 tool calling"
    )
    parser.add_argument(
        "--demo-tools",
        action="store_true",
        help="演示 M1 的文件与搜索工具（临时目录，安全）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _fix_console_encoding()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.check:
        return cmd_check(args)
    if args.demo_tools:
        return cmd_demo_tools(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
