"""图的接线与子图状态合并（离线，不联网）。

这两条是重构方案 B 时的安全网：接线错了图照样能跑，只是把任务派给错的人；
而子图状态合并若失效，消息会被重复累加、上下文迅速膨胀。
"""

from __future__ import annotations

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from code_agent.graph import build_graph
from code_agent.paths import RepoRoot
from code_agent.state import OverallState

WORKERS = ("explorer", "coder", "verifier")


def test_graph_compiles_with_expected_wiring(tmp_path, fake_settings):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")

    graph = build_graph(RepoRoot(tmp_path), fake_settings, InMemorySaver())
    draw = graph.get_graph()

    assert {"supervisor", *WORKERS} <= set(draw.nodes)
    edges = {(edge.source, edge.target) for edge in draw.edges}
    assert ("__start__", "supervisor") in edges
    for worker in WORKERS:
        assert (worker, "supervisor") in edges, f"{worker} 干完必须回到 supervisor"
        assert ("supervisor", worker) in edges, f"supervisor 必须能路由到 {worker}"


def test_subgraph_messages_are_not_duplicated(scripted, tmp_path):
    """子图返回的状态包含它继承的整段历史，靠 add_messages 去重 —— 不能重复累加。"""
    worker = create_agent(
        scripted([AIMessage(content="子图汇报")]), system_prompt="测试用"
    )
    builder = StateGraph(OverallState)
    builder.add_node("worker", worker)
    builder.add_edge(START, "worker")
    builder.add_edge("worker", END)
    app = builder.compile(checkpointer=InMemorySaver())

    out = app.invoke(
        {"messages": [HumanMessage("任务")], "attempts": 0},
        {"configurable": {"thread_id": "dup"}},
    )

    humans = [m for m in out["messages"] if isinstance(m, HumanMessage)]
    assert len(humans) == 1, "HumanMessage 被重复累加了"
