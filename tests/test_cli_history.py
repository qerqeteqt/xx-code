"""`--history` 的记录合并（纯函数）。

核心性质：被剪掉的消息**仍留在更早的快照里**，所以能从历史里合并出完整记录，
同时要知道哪些已经不在（模型）上下文里。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, ToolMessage

from code_agent.cli import merge_history


class _FakeSnapshot:
    """模拟 langgraph 的 StateSnapshot（只用到 .values）。"""

    def __init__(self, messages):
        self.values = {"messages": messages}


def test_merge_history_recovers_pruned_messages():
    tool_call = AIMessage(
        content="", id="a1",
        tool_calls=[{"name": "read_file", "args": {"path": "x"}, "id": "c1"}],
    )
    tool_result = ToolMessage(content="文件内容", tool_call_id="c1", id="t1")
    report = AIMessage(content="我读了 x", id="a2")

    # 快照顺序是"从新到旧"：最新状态里草稿已被剪掉
    newest = _FakeSnapshot([report])
    older = _FakeSnapshot([tool_call, tool_result, report])

    merged, latest_ids = merge_history([newest, older])

    assert [m.id for m in merged] == ["a1", "t1", "a2"], "要按最早出现的顺序合并出完整记录"
    assert latest_ids == {"a2"}, "只有汇报还在上下文里，草稿已被剪"


def test_merge_history_handles_empty():
    merged, latest_ids = merge_history([])
    assert merged == [] and latest_ids == set()
