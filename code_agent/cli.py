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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code_agent",
        description="基于 LangGraph 的多 Agent 编码助手",
    )
    parser.add_argument("--repo", metavar="PATH", help="目标仓库路径（M1 起使用）")
    parser.add_argument(
        "--check", action="store_true", help="自检：验证模型连通与 tool calling"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _fix_console_encoding()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.check:
        return cmd_check(args)

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
