"""命令行入口。

用法：
    python -m code_agent.cli --check
    python -m code_agent.cli --repo <路径> --agent explorer "任务"
    python -m code_agent.cli --repo <路径> "任务"          # supervisor 模式（M3）
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid

AGENT_ROLES = ("explorer", "coder", "verifier")


def _fix_console_encoding() -> None:
    """Windows 控制台默认 cp936，强制 UTF-8 输出避免中文/符号报错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def _text_of(message) -> str:
    """取出消息中的文本。

    本模型的 content 不是字符串，而是 block 列表（含 thinking 块），
    所以必须筛出 type == "text" 的块，不能直接当字符串用。
    """
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    parts = [
        block.get("text", "")
        for block in (content or [])
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return "".join(parts)


def cmd_check(_args: argparse.Namespace) -> int:
    """自检：配置 -> 纯文本调用 -> tool calling。"""
    from code_agent.config import Settings, build_llm

    settings = Settings.from_env()
    print(f"[1/3] 配置加载成功  ({settings.describe()})")

    llm = build_llm(settings)

    reply = llm.invoke("你是连通性测试。请只回复两个字：正常")
    print(f"[2/3] 文本调用成功  -> {_text_of(reply).strip()!r}")

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
        return 2

    print(f"[3/3] tool calling 正常  -> {json.dumps(tool_calls, ensure_ascii=False)}")
    print("\n自检全部通过，环境可用。")
    return 0


def cmd_agent(args: argparse.Namespace) -> int:
    """单 agent 模式：只运行指定的 worker。"""
    from langchain_core.messages import HumanMessage

    from code_agent.config import Settings
    from code_agent.paths import RepoRoot
    from code_agent.workers import build_worker

    root = RepoRoot(args.repo)
    worker = build_worker(args.agent, root, Settings.from_env())

    print(f"[{args.agent}] 仓库: {root}")
    print(f"[{args.agent}] 任务: {args.task}\n")

    final = None
    config = {"configurable": {"thread_id": uuid.uuid4().hex}}
    payload = {"messages": [HumanMessage(args.task)]}
    for chunk in worker.stream(payload, config, stream_mode="updates"):
        for update in chunk.values():
            for message in (update or {}).get("messages", []) or []:
                final = message
                for call in getattr(message, "tool_calls", None) or []:
                    raw = json.dumps(call.get("args", {}), ensure_ascii=False)
                    print(f"  → {call.get('name')}({raw[:160]})")

    print("-" * 60)
    print(_text_of(final) if final is not None else "(无输出)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code_agent",
        description="基于 LangGraph 的多 Agent 编码助手",
    )
    parser.add_argument("task", nargs="?", help="任务描述（自然语言）")
    parser.add_argument(
        "--repo", metavar="PATH", default=".", help="目标仓库路径（默认当前目录）"
    )
    parser.add_argument(
        "--agent", choices=AGENT_ROLES, help="单 agent 模式：只运行指定 worker"
    )
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

    if not args.task:
        parser.print_help()
        return 0

    if not args.agent:
        print("supervisor 模式将在 M3 提供；当前请用 --agent {explorer,coder,verifier}。")
        return 1

    return cmd_agent(args)


if __name__ == "__main__":
    raise SystemExit(main())
