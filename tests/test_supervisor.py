"""supervisor 路由逻辑（不联网，直接对节点函数断言）。

这是重构时最容易悄悄弄坏的一块：路由错了，图还是会跑完，只是跑到错的成员那里。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END

from code_agent.supervisor import make_supervisor


def _route(model, content, *, attempts: int = 0, extra: list | None = None,
           max_attempts: int = 8):
    llm = model([AIMessage(content=content)])
    messages = [HumanMessage("任务"), *(extra or [])]
    return make_supervisor(llm, max_attempts=max_attempts)(
        {"messages": messages, "attempts": attempts}
    )


def test_json_route_to_coder(scripted):
    assert _route(scripted, '{"next": "coder", "reason": "r"}').goto == "coder"


def test_finish_maps_to_end(scripted):
    assert _route(scripted, '{"next": "finish", "reason": "done"}').goto == END


def test_instruction_is_injected_as_human_message(scripted):
    """给成员的指令必须以 HumanMessage 注入 —— 手工构造 AIMessage 会让思考模型报 400。"""
    cmd = _route(
        scripted, '{"next": "coder", "reason": "r", "instruction": "把 add 改成加法"}'
    )
    injected = cmd.update["messages"]
    assert len(injected) == 1
    assert isinstance(injected[0], HumanMessage)
    assert "把 add 改成加法" in injected[0].content


def test_finish_does_not_inject_message(scripted):
    cmd = _route(scripted, '{"next": "finish", "reason": "done"}')
    assert "messages" not in cmd.update


def test_invalid_json_falls_back_to_rules(scripted):
    # attempts=0 → 兜底先派 explorer
    assert _route(scripted, "我就是不输出 JSON").goto == "explorer"


def test_fallback_prefers_verifier_when_edits_present(scripted):
    edited = AIMessage(content="", tool_calls=[{"name": "edit_file", "args": {}, "id": "1"}])
    assert _route(scripted, "不是 JSON", attempts=1, extra=[edited]).goto == "verifier"


def test_model_exception_falls_back_to_rules(scripted):
    """模型调用炸了也不能让 supervisor 成为崩溃点。"""

    class Boom:
        def invoke(self, *args, **kwargs):
            raise RuntimeError("boom")

    cmd = make_supervisor(Boom())({"messages": [HumanMessage("任务")], "attempts": 0})
    assert cmd.goto == "explorer"


def test_attempts_cap_ends_graph(scripted):
    cmd = _route(scripted, '{"next": "coder", "reason": "r"}', attempts=8, max_attempts=8)
    assert cmd.goto == END


def test_attempts_increments(scripted):
    cmd = _route(scripted, '{"next": "coder", "reason": "r"}', attempts=2)
    assert cmd.update["attempts"] == 3
