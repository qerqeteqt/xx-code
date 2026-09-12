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
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from code_agent.messages import text_of

AGENT_ROLES = ("explorer", "coder", "verifier")

# 图执行的步数上限（兜底，真正的轮次控制是 supervisor 的 attempts）
RECURSION_LIMIT = 250

# 各角色的配色，便于在 trace 里一眼分辨
ROLE_STYLE = {
    "supervisor": "bold cyan",
    "explorer": "green",
    "coder": "yellow",
    "verifier": "magenta",
}

console = Console(highlight=False)


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
    console.print(f"[1/3] 配置加载成功  ({settings.describe()})")

    llm = build_llm(settings)
    reply = text_of(llm.invoke("你是连通性测试。请只回复两个字：正常")).strip()
    console.print(f"[2/3] 文本调用成功  -> {reply!r}")

    from langchain_core.tools import tool

    @tool
    def echo(text: str) -> str:
        """原样返回传入的文本。"""
        return text

    result = llm.bind_tools([echo]).invoke(
        "请调用 echo 工具，把 text 参数设为 'tool-ok'，不要直接回答。"
    )
    tool_calls = getattr(result, "tool_calls", None) or []
    if not tool_calls:
        console.print("[red][3/3] 失败：模型没有返回工具调用，该端点可能不支持 tool calling[/]")
        return 2

    console.print(f"[3/3] tool calling 正常  -> {escape(json.dumps(tool_calls, ensure_ascii=False))}")
    console.print("\n[bold green]自检全部通过，环境可用。[/]")
    return 0


def _print_update(node: str, update, seen: set | None = None) -> None:
    """打印该节点**新产生**的工具调用。

    子图节点返回的状态里会带上它继承的整段共享历史，因此必须按消息 id 去重，
    否则会把上一个 agent 的动作误标到当前节点名下。
    """
    style = ROLE_STYLE.get(node, "white")
    for message in (update or {}).get("messages", []) or []:
        if seen is not None:
            message_id = getattr(message, "id", None)
            if message_id in seen:
                continue
            if message_id is not None:
                seen.add(message_id)
        for call in getattr(message, "tool_calls", None) or []:
            raw = json.dumps(call.get("args", {}), ensure_ascii=False)
            console.print(
                f"[{style}]{node:<10}[/][dim]→[/] {call.get('name')}"
                f"[dim]({escape(raw[:140])})[/]"
            )


def _ask(interrupt_value) -> str:
    """危险命令的人工确认（HITL）。"""
    body = (
        f"命令: [bold]{escape(str(interrupt_value.get('command')))}[/]\n"
        f"原因: {escape(str(interrupt_value.get('reason')))}\n"
        f"目录: [dim]{escape(str(interrupt_value.get('cwd')))}[/]"
    )
    console.print(Panel(body, title="⚠ 需要确认危险命令", border_style="red"))
    try:
        answer = input("执行吗？[y/N] ").strip().lower()
    except EOFError:
        answer = ""  # 非交互环境（stdin 不可用）时按拒绝处理，安全优先
    console.print("[red]已拒绝[/]" if answer not in ("y", "yes") else "[green]已批准[/]")
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


def _stream_once(graph, payload, config, seen: set, label: str | None = None):
    """跑一轮 stream；返回捕获到的 interrupt 值（没有则 None）。

    label 用于把节点名统一显示成角色名（--agent 模式下顶层是 worker 自身，
    节点名会是内部的 model/tools）。
    """
    for chunk in graph.stream(payload, config, stream_mode="updates"):
        if "__interrupt__" in chunk:
            return chunk["__interrupt__"][0].value
        for node, update in chunk.items():
            _print_update(label or node, update, seen)
    return None


def _drive(graph, payload, config, label: str | None = None) -> None:
    """驱动图跑完，遇到危险命令就暂停询问，然后恢复。

    `seen` 跨 resume 保留，否则中断恢复后会把中断前的消息重复打印。
    """
    from langgraph.types import Command

    seen: set = set()
    while True:
        pending = _stream_once(graph, payload, config, seen, label)
        if pending is None:
            return
        payload = Command(resume=_ask(pending))


def _check_pg(dsn: str | None) -> None:
    """提前验证 PostgreSQL 可连。

    否则连接失败时用户会看到一长串 psycopg 堆栈（其中 PG 返回的中文报错还可能因
    控制台编码问题变成乱码）。这里转成一句人话。
    """
    if not dsn:
        raise RuntimeError("缺少 AGENT_PG_DSN，请参考 .env.example 配置后再运行。")

    import psycopg

    try:
        with psycopg.connect(dsn, connect_timeout=5):
            return
    except Exception as exc:  # noqa: BLE001 - 统一转成友好提示
        raise RuntimeError(
            "无法连接 PostgreSQL。请确认：\n"
            "  1) 服务已启动（Get-Service *postgres*）\n"
            "  2) .env 里的 AGENT_PG_DSN 用户名/密码/库名正确\n"
            "  3) 数据库 langgraph_db 存在\n"
            f"  （底层错误类型：{type(exc).__name__}）"
        ) from exc


def cmd_run(args: argparse.Namespace) -> int:
    """完整模式：supervisor 调度三个 worker，PostgreSQL 持久化。"""
    from langchain_core.messages import HumanMessage

    from code_agent.config import Settings
    from code_agent.graph import build_graph, open_checkpointer
    from code_agent.paths import RepoRoot

    settings = Settings.from_env()
    root = RepoRoot(args.repo)
    thread_id = args.thread_id or uuid.uuid4().hex

    console.print(f"[dim]仓库[/] {root}\n[dim]会话[/] {thread_id}\n[dim]任务[/] {args.task}")

    _check_pg(settings.pg_dsn)
    with open_checkpointer(settings.pg_dsn) as checkpointer:
        graph = build_graph(root, settings, checkpointer)
        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT}
        _drive(graph, {"messages": [HumanMessage(args.task)], "attempts": 0}, config)
        final = _final_answer(graph.get_state(config).values.get("messages", []))

    console.print(Panel(final, title="最终结论", border_style="green"))
    return 0


def cmd_agent(args: argparse.Namespace) -> int:
    """单 agent 模式：只运行指定的 worker。

    单独运行时用 InMemorySaver，这样它调用危险命令也能正常暂停确认
    （interrupt 必须要有 checkpointer）。
    """
    from langchain_core.messages import HumanMessage
    from langgraph.checkpoint.memory import InMemorySaver

    from code_agent.config import Settings
    from code_agent.paths import RepoRoot
    from code_agent.workers import build_worker

    root = RepoRoot(args.repo)
    worker = build_worker(args.agent, root, Settings.from_env(), checkpointer=InMemorySaver())

    thread_id = args.thread_id or uuid.uuid4().hex
    console.print(f"[dim]仓库[/] {root}\n[dim]任务[/] {args.task}")

    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT}
    _drive(worker, {"messages": [HumanMessage(args.task)]}, config, label=args.agent)
    final = _final_answer(worker.get_state(config).values.get("messages", []))

    console.print(Panel(final, title=f"{args.agent} 输出", border_style=ROLE_STYLE.get(args.agent, "white")))
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

    try:
        if args.agent:
            return cmd_agent(args)
        return cmd_run(args)
    except RuntimeError as exc:  # 配置缺失 / PG 连不上等，给一句人话而不是堆栈
        console.print(f"\n[red]错误：[/]{escape(str(exc))}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
