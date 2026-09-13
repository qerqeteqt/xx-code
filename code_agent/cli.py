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
from pathlib import Path

from langchain_core.messages import AIMessage
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from code_agent.config import PROJECT_ROOT
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

# 记录"每个仓库最近一次的会话 id"，让 --chat 下次能自动续接上（短期记忆的一部分）。
# 放在项目根、已被 .gitignore 排除，不会进版本库。
SESSION_FILE = ".code_agent_sessions.json"


def _session_path() -> Path:
    return PROJECT_ROOT / SESSION_FILE


def _load_last_session(repo: str) -> str | None:
    try:
        data = json.loads(_session_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None  # 文件不存在或损坏都当作"没有历史会话"
    return data.get(repo)


def _save_last_session(repo: str, thread_id: str) -> None:
    path = _session_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    data[repo] = thread_id
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _fix_console_encoding() -> None:
    """Windows 控制台默认 cp936，三个标准流都强制 UTF-8。

    stdin 同样重要：交互模式下任务是从 stdin 读的（不是 argv）。重定向/管道时
    stdin 会按 cp936 解码，中文会变成**代理字符**（如 '\\udca1'），
    再发给模型 API 就会抛 `UnicodeEncodeError: surrogates not allowed`。
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def _clean(text: str) -> str:
    """清掉无法编码的代理字符（走 stdin 读中文时的残留）。

    用户输入是系统边界，在这里兜一下，避免一个坏字符就让整轮任务崩掉。
    """
    return text.encode("utf-8", "replace").decode("utf-8")


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


def _print_update(node: str, update, seen: set | None = None,
                  stats: dict | None = None) -> None:
    """打印该节点**新产生**的工具调用，并累加 token 用量。

    子图节点返回的状态里会带上它继承的整段共享历史，因此必须按消息 id 去重，
    否则会把上一个 agent 的动作误标到当前节点名下（顺带也会把用量重复计入）。
    """
    style = ROLE_STYLE.get(node, "white")
    for message in (update or {}).get("messages", []) or []:
        if seen is not None:
            message_id = getattr(message, "id", None)
            if message_id in seen:
                continue
            if message_id is not None:
                seen.add(message_id)
        if stats is not None and isinstance(message, AIMessage):
            usage = getattr(message, "usage_metadata", None) or {}
            if usage:
                stats["calls"] += 1
                stats["in"] += int(usage.get("input_tokens") or 0)
                stats["out"] += int(usage.get("output_tokens") or 0)
        for call in getattr(message, "tool_calls", None) or []:
            raw = json.dumps(call.get("args", {}), ensure_ascii=False)
            console.print(
                f"[{style}]{node:<10}[/][dim]→[/] {call.get('name')}"
                f"[dim]({escape(raw[:140])})[/]"
            )


def _new_stats() -> dict:
    return {"calls": 0, "in": 0, "out": 0}


def _print_usage(stats: dict, title: str) -> None:
    """打印一行用量统计。注意 output 里也包含 thinking 的 token，所以数字偏大属正常。"""
    if not stats["calls"]:
        return
    total = stats["in"] + stats["out"]
    console.print(
        f"[dim]{title}：模型调用 {stats['calls']} 次 · "
        f"输入 {stats['in']:,} · 输出 {stats['out']:,} · 合计 {total:,} tokens[/]"
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


def _final_answer(messages: list, before_ids: set | None = None) -> str:
    """取最后一条 AI 文本。

    `before_ids` 是**本轮开始前**已有消息的 id 集合 —— 只认这之后新产生的消息，
    否则本轮万一没产出回答，就会把**上一轮的旧回答**当成结果展示（实测踩到过）。

    用 id 集合而不是下标：方案 B 会在轮内**剪掉旧消息**，下标会漂移。
    """
    for message in reversed(messages):
        if not isinstance(message, AIMessage):
            continue
        if before_ids and getattr(message, "id", None) in before_ids:
            continue
        text = text_of(message).strip()
        if text:
            return text
    return "(本轮没有产生回答)"


def _stream_once(graph, payload, config, seen: set, turn: dict,
                 label: str | None = None):
    """跑一轮 stream；返回捕获到的 interrupt 值（没有则 None）。

    用 `["updates", "custom"]` 两种模式并用：
    - `updates`：节点完成的增量（工具调用轨迹 + token 用量），
      **interrupt 只在这个模式里出现**
    - `custom` ：supervisor 边生成边推出来的回答增量（逐字显示）

    `turn` 记录本轮状态：是否已流式输出过回答（started）+ token 用量（calls/in/out）。
    label 用于把节点名统一显示成角色名（--agent 模式下顶层是 worker 自身，
    节点名会是内部的 model/tools）。
    """
    for mode, chunk in graph.stream(payload, config, stream_mode=["updates", "custom"]):
        if mode == "custom":
            _handle_custom(chunk, turn)
            continue
        if "__interrupt__" in chunk:
            return chunk["__interrupt__"][0].value
        for node, update in chunk.items():
            _print_update(label or node, update, seen, turn)
    return None


def _handle_custom(chunk, turn: dict) -> None:
    """处理 supervisor 通过 `get_stream_writer()` 推来的 custom 块。

    - `answer_delta`：回答的正文增量，逐字打印
    - `usage`      ：一次模型调用的用量（**路由调用**不在 state 里，只能这样上报）
    """
    if not isinstance(chunk, dict):
        return
    kind = chunk.get("type")
    if kind == "answer_delta":
        if not turn.get("started"):
            console.print()  # 与上面的工具轨迹隔开
            turn["started"] = True
        console.print(chunk["text"], end="", markup=False, highlight=False)
    elif kind == "usage":
        turn["calls"] += 1
        turn["in"] += int(chunk.get("input_tokens") or 0)
        turn["out"] += int(chunk.get("output_tokens") or 0)


def _drive(graph, payload, config, label: str | None = None) -> dict:
    """驱动图跑完，遇到危险命令就暂停询问，然后恢复，最后打印本轮 token 用量。

    `seen` 跨 resume 保留，否则中断恢复后会把中断前的消息重复打印（用量也会重复计）。
    """
    from langgraph.types import Command

    seen: set = set()
    turn: dict = {"started": False, **_new_stats()}
    while True:
        pending = _stream_once(graph, payload, config, seen, turn, label)
        if pending is None:
            break
        payload = Command(resume=_ask(pending))
    if turn.get("started"):
        console.print()  # 收尾换行
    _print_usage(turn, "本轮")
    return turn


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
    root = RepoRoot(args.repo or ".")
    thread_id = args.thread_id or uuid.uuid4().hex

    console.print(f"[dim]仓库[/] {root}\n[dim]会话[/] {thread_id}\n[dim]任务[/] {args.task}")

    _check_pg(settings.pg_dsn)
    with open_checkpointer(settings.pg_dsn) as checkpointer:
        graph = build_graph(root, settings, checkpointer)
        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT}
        base = {m.id for m in graph.get_state(config).values.get("messages", [])}
        streamed = _drive(
            graph,
            {"messages": [HumanMessage(args.task)], "attempts": 0, "made_edits": False},
            config,
        )
        final = _final_answer(
            graph.get_state(config).values.get("messages", []), before_ids=base
        )

    # 回答若已边生成边显示，就不再重复渲染面板
    if not streamed.get("started"):
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

    root = RepoRoot(args.repo or ".")
    worker = build_worker(args.agent, root, Settings.from_env(), checkpointer=InMemorySaver())

    thread_id = args.thread_id or uuid.uuid4().hex
    console.print(f"[dim]仓库[/] {root}\n[dim]任务[/] {args.task}")

    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT}
    streamed = _drive(worker, {"messages": [HumanMessage(args.task)]}, config, label=args.agent)
    final = _final_answer(worker.get_state(config).values.get("messages", []))

    if not streamed.get("started"):
        console.print(
            Panel(final, title=f"{args.agent} 输出",
                  border_style=ROLE_STYLE.get(args.agent, "white"))
        )
    return 0


def cmd_chat(args: argparse.Namespace) -> int:
    """交互模式：在同一个会话里连续对话，agent 记得上文（短期记忆）。

    每轮输入都会把 `attempts` 重置为 0 —— 它是"本轮任务的调度次数上限"，
    不是整个会话的上限；不重置的话聊几轮后就会被上限立刻结束。
    """
    from langchain_core.messages import HumanMessage

    from code_agent.config import Settings
    from code_agent.graph import build_graph, open_checkpointer
    from code_agent.paths import RepoRoot

    settings = Settings.from_env()
    # 不指定 --repo 就用**当前目录** —— 像 Claude Code 那样：cd 到项目里直接开聊，
    # 不再多问一句。启动时会把实际使用的目录打出来，免得搞错。
    root = RepoRoot(args.repo or ".")

    if args.thread_id:
        thread_id, label = args.thread_id, "[dim]（指定会话）[/]"
    else:
        remembered = None if args.new else _load_last_session(str(root))
        if remembered:
            thread_id, label = remembered, "[green]（已续接上次会话）[/]"
        else:
            thread_id, label = uuid.uuid4().hex, "[dim]（新会话）[/]"

    _check_pg(settings.pg_dsn)
    console.print(f"[dim]仓库[/] {root}")
    console.print(f"[dim]会话[/] {thread_id}  {label}")
    console.print("[dim]直接输入任务即可；[/][cyan]:new[/][dim] 开新会话，[/][cyan]:q[/][dim] 退出[/]\n")

    with open_checkpointer(settings.pg_dsn) as checkpointer:
        graph = build_graph(root, settings, checkpointer)
        _save_last_session(str(root), thread_id)
        # 进程内累加，而不是回头读库求和 —— 方案 B 会剪掉带用量的消息，读库会少算
        session = _new_stats()

        while True:
            try:
                line = _clean(input(">>> ")).strip()
            except (EOFError, KeyboardInterrupt):
                console.print()
                break
            if not line:
                continue
            if line in (":q", ":quit", "exit", "quit"):
                break
            if line == ":new":
                thread_id = uuid.uuid4().hex
                _save_last_session(str(root), thread_id)
                console.print(f"[dim]已开新会话[/] {thread_id}\n")
                continue

            config = {
                "configurable": {"thread_id": thread_id},
                "recursion_limit": RECURSION_LIMIT,
            }
            payload = {"messages": [HumanMessage(line)], "attempts": 0, "made_edits": False}

            base = {m.id for m in graph.get_state(config).values.get("messages", [])}
            try:
                streamed = _drive(graph, payload, config)
            except RuntimeError as exc:
                # 单轮出错不该终结整个会话
                console.print(f"[red]本轮出错：[/]{escape(str(exc))}\n")
                continue

            for key in ("calls", "in", "out"):
                session[key] += streamed.get(key, 0)

            if not streamed.get("started"):
                final = _final_answer(
                    graph.get_state(config).values.get("messages", []), before_ids=base
                )
                console.print(Panel(final, title="最终结论", border_style="green"))

    _print_usage(session, "本次运行累计")
    console.print("\n[dim]会话已保存（内容在 PostgreSQL 里，退出不会丢）。下次继续：[/]")
    if Path.cwd() == Path(root.root):
        console.print("  [cyan]xx-code[/]  [dim]（当前目录就是该仓库，会自动续接本次会话）[/]")
    else:
        console.print(f"  [cyan]cd {root}[/]  然后 [cyan]xx-code[/]")
    console.print(
        f"  [dim]想直接指定会话：[/][cyan]xx-code --repo {root} --thread-id {thread_id}[/]"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code_agent",
        description="基于 LangGraph 的多 Agent 编码助手",
    )
    parser.add_argument("task", nargs="?", help="任务描述（自然语言）")
    parser.add_argument(
        "--repo", metavar="PATH", default=None, help="目标仓库路径（默认当前目录）"
    )
    parser.add_argument(
        "--chat", action="store_true", help="交互模式：连续对话，agent 记得上文"
    )
    parser.add_argument("--new", action="store_true", help="交互模式下强制开新会话")
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

    try:
        if args.chat:
            return cmd_chat(args)
        if args.agent:
            if not args.task:
                console.print("[red]--agent 需要同时给出任务[/]")
                return 1
            return cmd_agent(args)
        if args.task:
            return cmd_run(args)
        # 不带任何参数 → 直接进入对话（这是最常用的用法）
        return cmd_chat(args)
    except RuntimeError as exc:  # 配置缺失 / PG 连不上等，给一句人话而不是堆栈
        console.print(f"\n[red]错误：[/]{escape(str(exc))}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
