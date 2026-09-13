"""JSONL checkpointer：落盘 / 重放 / 跨实例续跑（不联网）。

关键性质：**换一个进程（新实例）也要能接着跑**，包括 HITL 的暂停-恢复。
这是原来 PostgreSQL 提供的保证，换成文件后必须仍然成立。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from code_agent.jsonl_saver import JsonlSaver
from code_agent.state import OverallState


def _build(saver, tmp_path):
    """一个小图：记录每一步，便于检查状态是否被持久化。"""

    def node(state):
        return {"messages": [AIMessage(content=f"第 {state.get('attempts', 0) + 1} 步")]}

    builder = StateGraph(OverallState)
    builder.add_node("n", node)
    builder.add_edge(START, "n")
    builder.add_edge("n", END)
    return builder.compile(checkpointer=saver)


def test_state_survives_new_instance(tmp_path):
    """换一个 saver 实例（模拟进程重启）后，历史消息仍在。"""
    cfg = {"configurable": {"thread_id": "t1"}}

    _build(JsonlSaver(tmp_path), tmp_path).invoke(
        {"messages": [HumanMessage("开始")], "attempts": 0, "made_edits": False}, cfg
    )

    reopened = _build(JsonlSaver(tmp_path), tmp_path)
    values = reopened.get_state(cfg).values

    assert len(values["messages"]) == 2, "重启后应该还能看到「提问 + 第 1 步」"
    assert values["messages"][0].content == "开始"


def test_writes_to_disk_are_readable_jsonl(tmp_path):
    cfg = {"configurable": {"thread_id": "t2"}}
    _build(JsonlSaver(tmp_path), tmp_path).invoke(
        {"messages": [HumanMessage("开始")], "attempts": 0, "made_edits": False}, cfg
    )

    path = tmp_path / "t2.jsonl"
    assert path.exists(), "应该生成 <thread_id>.jsonl"
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines, "文件不该为空"
    import json

    assert {json.loads(ln)["op"] for ln in lines} <= {"put", "writes"}


def test_threads_are_isolated(tmp_path):
    saver = JsonlSaver(tmp_path)
    graph = _build(saver, tmp_path)
    graph.invoke(
        {"messages": [HumanMessage("A")], "attempts": 0, "made_edits": False},
        {"configurable": {"thread_id": "a"}},
    )
    graph.invoke(
        {"messages": [HumanMessage("B")], "attempts": 0, "made_edits": False},
        {"configurable": {"thread_id": "b"}},
    )

    reopened = _build(JsonlSaver(tmp_path), tmp_path)
    a = reopened.get_state({"configurable": {"thread_id": "a"}}).values["messages"]
    b = reopened.get_state({"configurable": {"thread_id": "b"}}).values["messages"]
    assert a[0].content == "A" and b[0].content == "B"


def test_concurrent_appends_do_not_corrupt(tmp_path):
    """LangGraph 的并行任务会**多线程**调用 put_writes —— 追加必须加锁。

    不加锁时两次写入会交错，行被切成半截（实测踩到过：行首是 base64 碎片、
    行尾才是完整 JSON），重放时会跳过大量"损坏"行。
    """
    import json
    import threading

    saver = JsonlSaver(tmp_path)

    def worker(i: int) -> None:
        saver._append("t", {"op": "x", "i": i, "pad": "x" * 5000})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(24)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    lines = [ln for ln in (tmp_path / "t.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 24, "每行应该是一次完整的追加"
    for line in lines:
        json.loads(line)  # 任何一行残缺都会在这里炸


def test_corrupt_line_is_skipped(tmp_path):
    """进程被杀时最后一行可能只写了一半 —— 不该让整个会话报废。"""
    cfg = {"configurable": {"thread_id": "t3"}}
    _build(JsonlSaver(tmp_path), tmp_path).invoke(
        {"messages": [HumanMessage("开始")], "attempts": 0, "made_edits": False}, cfg
    )

    path = tmp_path / "t3.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"op": "put", "cp": [')  # 半行

    reopened = _build(JsonlSaver(tmp_path), tmp_path)
    assert len(reopened.get_state(cfg).values["messages"]) == 2


def test_interrupt_and_resume_across_instances(tmp_path):
    """HITL 的关键保证：暂停后**换一个实例**仍能恢复。

    原来靠 PostgreSQL 提供；换成文件后必须仍然成立，否则危险命令确认会失效。
    """
    from langgraph.types import interrupt

    def node(state):
        decision = interrupt({"ask": "继续吗"})
        return {"messages": [AIMessage(content=f"用户说：{decision}")]}

    builder = StateGraph(OverallState)
    builder.add_node("n", node)
    builder.add_edge(START, "n")
    builder.add_edge("n", END)

    cfg = {"configurable": {"thread_id": "t4"}}
    first = builder.compile(checkpointer=JsonlSaver(tmp_path))
    first.invoke(
        {"messages": [HumanMessage("跑")], "attempts": 0, "made_edits": False}, cfg
    )
    assert first.get_state(cfg).next, "应该停在中断处"

    # 换实例（新进程）再恢复
    second = builder.compile(checkpointer=JsonlSaver(tmp_path))
    out = second.invoke(Command(resume="approve"), cfg)
    assert "用户说：approve" in out["messages"][-1].content
