"""组装父图：supervisor 调度三个 worker 子图，带 PostgreSQL 持久化。

结构：

    START → supervisor ─┬→ explorer ─┐
                        ├→ coder    ─┼→ supervisor（循环）
                        ├→ verifier ─┘
                        └→ END

关键约束：**checkpointer 只装在根图**，worker 子图不装（子图自己装会冲突）。
HITL 的 `interrupt()` 从子图内传播到根图，因此暂停/恢复都由根图的 thread 负责。
"""

from __future__ import annotations

from contextlib import contextmanager

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph

from code_agent.config import Settings, build_llm
from code_agent.paths import RepoRoot
from code_agent.state import OverallState
from code_agent.supervisor import make_supervisor
from code_agent.workers import build_coder, build_explorer, build_verifier

BUILDERS = {
    "explorer": build_explorer,
    "coder": build_coder,
    "verifier": build_verifier,
}


@contextmanager
def open_checkpointer(dsn: str | None):
    """PostgreSQL checkpointer。

    `setup()` 会建表（幂等），首次在别的机器上跑时也能自动初始化。
    """
    if not dsn:
        raise RuntimeError(
            "缺少 AGENT_PG_DSN，无法启用持久化。请参考 .env.example 配置。"
        )
    with PostgresSaver.from_conn_string(dsn) as saver:
        saver.setup()
        yield saver


def build_graph(root: RepoRoot, settings: Settings, checkpointer):
    """构建并编译父图。"""
    graph = StateGraph(OverallState)
    graph.add_node(
        "supervisor",
        make_supervisor(build_llm(settings)),
        destinations=(*BUILDERS, END),
    )
    for name, builder in BUILDERS.items():
        graph.add_node(name, builder(root, settings))
        graph.add_edge(name, "supervisor")
    graph.add_edge(START, "supervisor")
    return graph.compile(checkpointer=checkpointer)
