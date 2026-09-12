"""命令行入口。

用法：
    python -m code_agent.cli --check
    python -m code_agent.cli --repo <路径> "任务"                   # supervisor 调度三个 worker
    python -m code_agent.cli --repo <路径> --agent explorer "任务"   # 只跑单个 worker
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid

from langchain_core.messages import AIMessage

from code_agent.messages import text_of

AGENT_ROLES = ("explorer", "coder", "verifier")

# 图执行的步数上限（兜底，真正的轮次控制是 supervisor 的 attempts）
RECURSION_LIMIT = 250


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
    print(f"[2/3] 文本调用成功  -> {text_of(llm.invoke('你是连通性测试。请只回复两个字：正常')).strip()!r}")

    from langchain_core.tools import tool

    @tool
    def echo(text: str) -> str:
        """原样返回传入的文本。"""
        return text

    reply = llm.bind_tools([echo]).invoke(
        "请调用 echo 工具，把 text 参数设为 'tool-ok'，不要直接回答。"
    )
    tool_calls = getattr(reply, "tool_calls", None) or []
    if not tool_calls:
        print("[3/3] 失败：模型没有返回工具调用，该端点可能不支持 tool calling")
        return 2

    print(f"[3/3] tool calling 正常  -> {json.dumps(tool_calls, ensure_ascii=False)}")
    print("\n自检全部通过，环境可用。")
    return 0


def _print_update(node: str, update, seen: set | None = None) -> None:
    """打印该节点**新产生**的工具调用。

    子图节点返回的状态里会带上它继承的整段共享历史，因此必须按消息 id 去重，
    否则会把上一个 agent 的动作误标到当前节点名下。
    """
    for message in (update or {}).get("messages", []) or []:
        if seen is not None:
            message_id = getattr(message, "id", None)
            if message_id in seen:
                continue
            if message_id is not None:
                seen.add(message_id)
        for call in getattr(message, "tool_calls", None) or []:
            raw = json.dumps(call.get("args", {}), ensure_ascii=False)
            print(f"[{node}] → {call.get('name')}({raw[:140]})")


def _ask(interrupt_value) -> str:
    """危险命令的人工确认（HITL）。"""
    print("\n" + "!" * 58)
    print("需要你确认一条危险命令：")
    print(f"  命令: {interrupt_value.get('command')}")
    print(f"  原因: {interrupt_value.get('reason')}")
    print(f"  目录: {interrupt_value.get('cwd')}")
    try:
        answer = input("  执行吗？[y/N] ").strip().lower()
    except EOFError:
        answer = ""  # 非交互环境（stdin 不可用）时按拒绝处理，安全优先
    print("!" * 58)
    return "approve" if answer in ("y", "yes") else "reject"


def _final_answer(messages: list) -> str:
    """最后一条 AI 文本（通常是 verifier 的结论）。"""
    for message in reversed(messages):
        if not isinstance(message, AIMessage):
            continue
        text = text_of(message).strip()
        if text:
            return text
    return "(无输出)"


def cmd_run(args: argparse.Namespace) -> int:
    """完整模式：supervisor 调度三个 worker，PostgreSQL 持久化。"""
    from langchain_core.messages import HumanMessage
    from langgraph.types import Command

    from code_agent.config import Settings
    from code_agent.graph import build_graph, open_checkpointer
    from code_agent.paths import RepoRoot

    settings = Settings.from_env()
    root = RepoRoot(args.repo)
    thread_id = args.thread_id or uuid.uuid4().hex

    print(f"[run] 仓库: {root}")
    print(f"[run] 会话: {thread_id}")
    print(f"[run] 任务: {args.task}")

    with open_checkpointer(settings.pg_dsn) as checkpointer:
        graph = build_graph(root, settings, checkpointer)
        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT}
        payload: object = {"messages": [HumanMessage(args.task)], "attempts": 0}

        seen: set = set()  # 跨 resume 保留，否则中断恢复后会把中断前的消息重复打印
        while True:
            pending = None
            for chunk in graph.stream(payload, config, stream_mode="updates"):
                if "__interrupt__" in chunk:
                    pending = chunk["__interrupt__"][0].value
                    break
                for node, update in chunk.items():
                    _print_update(node, update, seen)
            if pending is None:
                break
            payload = Command(resume=_ask(pending))

        print("-" * 58)
        print(_final_answer(graph.get_state(config).values.get("messages", [])))
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
    config = {"configurable": {"thread_id": args.thread_id or uuid.uuid4().hex}}
    payload = {"messages": [HumanMessage(args.task)]}
    for chunk in worker.stream(payload, config, stream_mode="updates"):
        for node, update in chunk.items():
            _print_update(node, update)
            for message in (update or {}).get("messages", []) or []:
                final = message

    print("-" * 58)
    print(text_of(final).strip() if final is not None else "(无输出)")
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
        "--agent", choices=AGENT_ROLES, help="只运行单个 worker，而不是完整调度"
    )
    parser.add_argument("--thread-id", help="会话 ID，用于跨次运行续接同一会话")
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
    if args.agent:
        return cmd_agent(args)
    return cmd_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
