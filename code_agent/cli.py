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

from code_agent.config import SESSION_DIR
from code_agent.memory import build_store
from code_agent.messages import text_of
from code_agent.transcript import Transcript

AGENT_ROLES = ("explorer", "coder", "verifier")

# 长期记忆：检索注入用。
# 注意注入阈值（0.35）比写入去重的阈值（0.92）**宽松得多** —— 实测换个说法来问，
# 最高相似度也只有 0.6 左右；用 0.92 当检索阈值会什么都搜不到。
MEMORY_MARKER = "[相关记忆]"
MEMORY_TOP_K = 3
MEMORY_MIN_SCORE = 0.35

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

# 记录"每个仓库最近一次的会话 id"，让 --chat 下次能自动续接上。
# 与会话内容放在同一个目录（.code_agent_sessions/），已被 .gitignore 排除。
def _session_path() -> Path:
    return SESSION_DIR / "index.json"


def _load_last_session(repo: str) -> str | None:
    try:
        data = json.loads(_session_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None  # 文件不存在或损坏都当作"没有历史会话"
    return data.get(repo)


def _save_last_session(repo: str, thread_id: str) -> None:
    path = _session_path()
    path.parent.mkdir(parents=True, exist_ok=True)
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
        tool_calls = getattr(message, "tool_calls", None) or []
        for call in tool_calls:
            raw = json.dumps(call.get("args", {}), ensure_ascii=False)
            console.print(
                f"[{style}]{node:<10}[/][dim]→[/] {call.get('name')}"
                f"[dim]({escape(raw[:140])})[/]"
            )

        # 顺带写进人可读记录（supervisor 的最终回答由 _drive 统一记录，这里跳过）
        transcript = (stats or {}).get("transcript")
        if transcript is not None and isinstance(message, AIMessage):
            if tool_calls:
                transcript.tools(node, tool_calls)
            text = text_of(message).strip()
            if text and not (message.id or "").startswith("supervisor-answer-"):
                transcript.report(node, text)


def _new_stats() -> dict:
    return {"calls": 0, "in": 0, "out": 0}


def _config(thread_id: str, repo: str | None = None) -> dict:
    """构造运行配置。带上 repo 元信息，`--history` 才能按仓库筛会话。"""
    config: dict = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": RECURSION_LIMIT,
    }
    if repo:
        config["metadata"] = {"repo": repo}
    return config


def merge_history(states: list) -> tuple[list, set]:
    """把多个历史快照合并成一份**完整**记录（含已被剪掉的消息）。

    LangGraph 每走一步存一个快照，所以被剪掉的消息仍留在更早的快照里。
    这里从最旧的快照往前扫、按消息 id 去重，得到按时间顺序的完整记录。

    返回 (完整消息列表, 最新状态里仍存在的 id 集合) —— 后者用来标注哪些"已不在上下文里"。
    """
    latest_ids = set()
    if states:
        for message in states[0].values.get("messages", []) or []:
            latest_ids.add(getattr(message, "id", None) or id(message))

    merged: list = []
    seen: set = set()
    for state in reversed(states):  # states 是"从新到旧"，反过来即从最早开始
        for message in state.values.get("messages", []) or []:
            key = getattr(message, "id", None) or id(message)
            if key in seen:
                continue
            seen.add(key)
            merged.append(message)
    return merged, latest_ids


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

    - `decision`     ：路由决策（supervisor 不再自己 print，由这里显示与记录）
    - `answer_delta` ：回答的正文增量，逐字打印
    - `usage`        ：一次模型调用的用量（**路由调用**不在 state 里，只能这样上报）
    """
    if not isinstance(chunk, dict):
        return
    kind = chunk.get("type")
    transcript = turn.get("transcript")

    if kind == "decision":
        next_ = str(chunk.get("next", ""))
        reason = str(chunk.get("reason", ""))
        console.print(f"[bold cyan][supervisor][/] [dim]下一步 →[/] {next_}（{escape(reason)}）")
        if transcript is not None:
            transcript.decision(next_, reason)
    elif kind == "answer_delta":
        if not turn.get("started"):
            console.print()  # 与上面的工具轨迹隔开
            turn["started"] = True
        text = chunk["text"]
        turn["answer"] = turn.get("answer", "") + text
        console.print(text, end="", markup=False, highlight=False)
    elif kind == "usage":
        turn["calls"] += 1
        turn["in"] += int(chunk.get("input_tokens") or 0)
        turn["out"] += int(chunk.get("output_tokens") or 0)


def _drive(graph, payload, config, label: str | None = None,
           transcript: Transcript | None = None) -> dict:
    """驱动图跑完，遇到危险命令就暂停询问，然后恢复，最后打印本轮 token 用量。

    `seen` 跨 resume 保留，否则中断恢复后会把中断前的消息重复打印（用量也会重复计）。
    `transcript` 传了就同时写一份人可读的 Markdown 记录。
    """
    from langgraph.types import Command

    seen: set = set()
    turn: dict = {"started": False, "answer": "", "transcript": transcript, **_new_stats()}
    while True:
        pending = _stream_once(graph, payload, config, seen, turn, label)
        if pending is None:
            break
        payload = Command(resume=_ask(pending))
    if turn.get("started"):
        console.print()  # 收尾换行
    if transcript is not None and turn.get("answer"):
        transcript.answer(turn["answer"])
    _print_usage(turn, "本轮")
    return turn


def _memory_block(store, query: str) -> str:
    """检索长期记忆并压成一段可注入的文本。检索不到就返回空串。

    注意这里的 `min_score`（0.35）比写入去重的阈值（0.92）**宽松得多** ——
    实测换个说法来问，最高分也只有 0.6 左右；用 0.92 当检索阈值会什么都搜不到。
    两个阈值用途不同，别混。
    """
    hits = store.search(query, k=MEMORY_TOP_K, min_score=MEMORY_MIN_SCORE)
    if not hits:
        return ""
    lines = "\n".join(f"- [{m.kind}] {m.text}" for m in hits)
    return f"{MEMORY_MARKER}\n{lines}"


def _has_memory_block(messages: list) -> bool:
    """这个会话里是否已经注入过记忆（用来保证只注入一次）。"""
    return any(
        isinstance(m, HumanMessage) and text_of(m).startswith(MEMORY_MARKER)
        for m in messages
    )


def _extract_memories(store, settings, messages: list, thread_id: str) -> None:
    """会话结束：用**独立的一次 LLM 调用**把对话抽成长期记忆。

    刻意放在图之外、会话收尾时做：不阻塞主流程，而且抽取失败也不影响任务本身
    （`extract_operations` 内部吞异常）。
    """
    from code_agent.config import build_llm
    from code_agent.memory import apply_operations, extract_operations

    conversation = "\n".join(
        f"{'用户' if message.type == 'human' else '助手'}: {text_of(message).strip()[:400]}"
        for message in messages
        if text_of(message).strip()
    )
    if len(conversation) < 200:  # 太短（比如只问了一句）就别抽了，免得把寒暄也记下来
        return

    console.print("[dim]正在整理长期记忆…[/]")
    operations = extract_operations(build_llm(settings), conversation, store.all())
    if not operations:
        console.print("[dim]本次没有值得长期记住的内容。[/]")
        return
    added, updated, deleted = apply_operations(store, operations, source=thread_id)
    console.print(f"[dim]长期记忆已更新：新增 {added} · 更新 {updated} · 删除 {deleted}[/]")


def _print_memory(store) -> None:
    """`:memory` —— 让人看看它到底记住了什么。"""
    rows = store.all()
    if not rows:
        console.print("[dim]长期记忆还是空的。[/]")
        return
    console.print(f"[bold]长期记忆（{len(rows)} 条）[/]")
    for m in rows:
        console.print(f"  [dim]\\[{m.kind}][/] {m.text}")


def _list_sessions(checkpointer, graph, root: str) -> None:
    """列出会话（文件名本身就是可读的：日期目录 / 时间-摘要-短id）。"""
    index: dict[str, str] = {}
    try:
        raw = json.loads(_session_path().read_text(encoding="utf-8"))
        index = {thread_id: repo for repo, thread_id in raw.items()}
    except (OSError, json.JSONDecodeError):
        pass

    sessions = checkpointer.sessions()
    console.print(f"[dim]仓库[/] {root}")
    if not sessions:
        console.print("\n[dim]还没有任何会话。跑一次 `xx-code` 就会生成。[/]")
        return

    console.print(f"\n[bold]会话（{len(sessions)} 个，按最近使用排序）[/]")
    for thread_id, path in sessions[:15]:
        try:
            msgs = graph.get_state(
                {"configurable": {"thread_id": thread_id}}
            ).values.get("messages", [])
        except Exception:  # noqa: BLE001 - 单个会话读不出来不该影响整个列表
            msgs = []
        relative = path.relative_to(SESSION_DIR).as_posix()
        repo = index.get(thread_id, "(未标记)")
        console.print(f"  [cyan]{relative}[/]")
        console.print(f"      [dim]{len(msgs)} 条消息 · {repo} · id={thread_id[:8]}[/]")

    console.print(
        "\n[dim]看某一条的完整记录（含被剪掉的工具调用与结果）：[/]\n"
        "  [cyan]xx-code --history --thread-id <id，粘贴前几位即可>[/]\n"
        f"[dim]文件也可以直接用编辑器打开：[/][cyan]{SESSION_DIR}[/]"
    )


def _print_transcript(graph, thread_id: str) -> None:
    """打印某个会话的完整记录，并标出哪些消息已不在（模型）上下文里。"""
    states = list(graph.get_state_history({"configurable": {"thread_id": thread_id}}))
    if not states:
        console.print(f"[red]找不到会话：[/]{thread_id}")
        return

    merged, latest_ids = merge_history(states)
    console.print(
        f"[dim]会话[/] {thread_id}\n"
        f"[dim]{len(states)} 个快照，合并后 {len(merged)} 条消息；"
        f"标 [yellow]·已剪[/] 的表示当前不在模型上下文里（记录本身仍在文件里）[/]\n"
    )
    for message in merged:
        kind = type(message).__name__
        style = {
            "HumanMessage": "cyan",
            "AIMessage": "white",
            "ToolMessage": "dim",
        }.get(kind, "white")
        pruned = "" if (getattr(message, "id", None) or id(message)) in latest_ids else "  [yellow]·已剪[/]"
        calls = [c["name"] for c in (getattr(message, "tool_calls", None) or [])]
        body = text_of(message).strip().replace("\n", " ")
        console.print(f"[{style}]{kind:12}[/]{pruned} {body[:150]}")
        if calls:
            console.print(f"[dim]             → 调用 {calls}[/]")


def _resolve_thread(checkpointer, prefix: str) -> str | None:
    """把（可能是前缀的）id 解析成完整 thread_id —— 列表里只显示前 8 位，方便粘贴。"""
    ids = [thread_id for thread_id, _ in checkpointer.sessions()]
    if prefix in ids:
        return prefix
    hits = [thread_id for thread_id in ids if thread_id.startswith(prefix)]
    if len(hits) == 1:
        return hits[0]
    console.print(
        f"[red]id 不唯一或不存在：[/]{prefix}"
        + (f"（匹配到 {len(hits)} 个）" if hits else "")
    )
    return None


def cmd_history(args: argparse.Namespace) -> int:
    """查看历史记录：不带 --thread-id 列会话，带了则打印完整记录。"""
    from code_agent.config import Settings
    from code_agent.graph import build_graph, open_checkpointer
    from code_agent.paths import RepoRoot

    settings = Settings.from_env()
    root = RepoRoot(args.repo or ".")

    with open_checkpointer() as checkpointer:
        graph = build_graph(root, settings, checkpointer)
        if args.thread_id:
            thread_id = _resolve_thread(checkpointer, args.thread_id)
            if thread_id is None:
                return 1
            _print_transcript(graph, thread_id)
        else:
            _list_sessions(checkpointer, graph, str(root))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """完整模式：supervisor 调度三个 worker，会话存本地 JSONL。"""
    from langchain_core.messages import HumanMessage

    from code_agent.config import Settings
    from code_agent.graph import build_graph, open_checkpointer
    from code_agent.paths import RepoRoot

    settings = Settings.from_env()
    root = RepoRoot(args.repo or ".")
    thread_id = args.thread_id or uuid.uuid4().hex

    console.print(f"[dim]仓库[/] {root}\n[dim]会话[/] {thread_id}\n[dim]任务[/] {args.task}")

    with open_checkpointer() as checkpointer:
        graph = build_graph(root, settings, checkpointer)
        # 文件名里用第一句任务作摘要（只在会话首次落盘时生效）
        checkpointer.set_label(thread_id, args.task)
        _save_last_session(str(root), thread_id)  # 一次性任务也能被 --history 标上仓库、被下次续聊
        transcript = Transcript(checkpointer.transcript_path(thread_id))
        transcript.header(str(root), thread_id)
        transcript.user(args.task)

        config = _config(thread_id, str(root))
        base = {m.id for m in graph.get_state(config).values.get("messages", [])}
        streamed = _drive(
            graph,
            {"messages": [HumanMessage(args.task)], "attempts": 0, "made_edits": False},
            config,
            transcript=transcript,
        )
        final = _final_answer(
            graph.get_state(config).values.get("messages", []), before_ids=base
        )

    # 回答若已边生成边显示，就不再重复渲染面板
    if not streamed.get("started"):
        console.print(Panel(final, title="最终结论", border_style="green"))
        transcript.answer(final)  # 非流式路径也要记进记录文件
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

    config = _config(thread_id, str(root))
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

    store = build_store(settings, str(root))
    console.print(f"[dim]仓库[/] {root}")
    console.print(f"[dim]会话[/] {thread_id}  {label}")
    console.print(
        "[dim]长期记忆[/] "
        + (f"[green]开[/]（{store.count()} 条，:memory 查看）" if store else "[dim]关（未配置 Milvus / Embedding）[/]")
    )
    console.print("[dim]直接输入任务即可；[/][cyan]:new[/][dim] 开新会话，[/][cyan]:q[/][dim] 退出[/]\n")

    with open_checkpointer() as checkpointer:
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
            if line == ":memory":
                if store is None:
                    console.print("[dim]长期记忆未启用（缺 AGENT_MILVUS_URI 或 DASHSCOPE_API_KEY）[/]\n")
                else:
                    _print_memory(store)
                    console.print()
                continue

            config = _config(thread_id, str(root))
            payload = {"messages": [HumanMessage(line)], "attempts": 0, "made_edits": False}

            # 会话第一次落盘时，用第一句任务给文件起个可读的摘要名
            if not checkpointer.has_session(thread_id):
                checkpointer.set_label(thread_id, line)

            transcript = Transcript(checkpointer.transcript_path(thread_id))
            transcript.header(str(root), thread_id)
            transcript.user(line)

            current = graph.get_state(config).values.get("messages", [])
            base = {m.id for m in current}

            # 长期记忆：**只在还没注入过时注入一次**。
            # 用"还没注入过"而不是"第一个用户轮"，是为了避免用户第一句只是寒暄
            # （"你好"）时拿寒暄去检索、白跑一次还把位置占掉。
            if store is not None and not _has_memory_block(current):
                block = _memory_block(store, line)
                if block:
                    payload["messages"].insert(0, HumanMessage(block))
                    console.print("[dim]（已注入相关长期记忆）[/]")
            try:
                streamed = _drive(graph, payload, config, transcript=transcript)
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
                transcript.answer(final)

        # 读一段对话用于抽取记忆 —— 必须在 checkpointer 关掉之前读
        final_messages = graph.get_state(_config(thread_id, str(root))).values.get("messages", [])

    if store is not None:
        _extract_memories(store, settings, final_messages, thread_id)

    _print_usage(session, "本次运行累计")
    console.print(f"\n[dim]会话已保存到 {SESSION_DIR}（退出不会丢）。下次继续：[/]")
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
        "--history", action="store_true",
        help="查看历史记录：列出会话；配合 --thread-id 打印完整记录（含被剪掉的工具调用）",
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

    try:
        if args.history:
            return cmd_history(args)
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
