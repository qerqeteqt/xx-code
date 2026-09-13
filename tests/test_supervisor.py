"""supervisor 路由逻辑（不联网，直接对节点函数断言）。

这是重构时最容易悄悄弄坏的一块：路由错了，图还是会跑完，只是跑到错的成员那里。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END

from code_agent.supervisor import make_supervisor


def _route(model, *contents: str, attempts: int = 0, extra: list | None = None,
           max_attempts: int = 8):
    """注意：收尾（finish / 到达上限）时 supervisor 会**再调一次模型**生成给用户的回答，
    所以那些用例要传两个 contents（第一个是路由 JSON，第二个是回答）。"""
    llm = model([AIMessage(content=c) for c in contents])
    messages = [HumanMessage("任务"), *(extra or [])]
    return make_supervisor(llm, max_attempts=max_attempts)(
        {"messages": messages, "attempts": attempts}
    )


def test_json_route_to_coder(scripted):
    assert _route(scripted, '{"next": "coder", "reason": "r"}').goto == "coder"


def test_finish_maps_to_end(scripted):
    cmd = _route(scripted, '{"next": "finish", "reason": "done"}', "已完成")
    assert cmd.goto == END


def test_instruction_is_injected_as_human_message(scripted):
    """给成员的指令必须以 HumanMessage 注入 —— 手工构造 AIMessage 会让思考模型报 400。"""
    cmd = _route(
        scripted, '{"next": "coder", "reason": "r", "instruction": "把 add 改成加法"}'
    )
    injected = cmd.update["messages"]
    assert len(injected) == 1
    assert isinstance(injected[0], HumanMessage)
    assert "把 add 改成加法" in injected[0].content


def test_finish_produces_user_facing_answer(scripted):
    """收尾时必须给用户一个回答。

    三个 worker 都是工具驱动的，遇到"你刚才改了什么？"这类提问没人能答；
    若只是静默 END，CLI 会把上一轮的旧报告当成答案显示（实测踩到过）。
    """
    cmd = _route(scripted, '{"next": "finish", "reason": "done"}', "改了 calc.py")
    assert cmd.goto == END
    reply = cmd.update["messages"][0]
    assert isinstance(reply, AIMessage)
    assert "改了 calc.py" in reply.content
    # 确定性 id：节点在 resume 后会重跑，靠 id 去重避免同一条回答被追加两次
    assert reply.id.startswith("supervisor-answer-")


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
    cmd = _route(scripted, '{"next": "coder", "reason": "r"}', "已尽力，结束",
                 attempts=8, max_attempts=8)
    assert cmd.goto == END


def test_attempts_increments(scripted):
    cmd = _route(scripted, '{"next": "coder", "reason": "r"}', attempts=2)
    assert cmd.update["attempts"] == 3


def test_attempts_is_a_per_turn_counter(scripted):
    """attempts 约束的是"本轮任务"，不是整个会话。

    CLI 每轮用户输入都会把它重置为 0；不重置的话，交互模式下聊几轮之后
    状态里残留的 attempts 会让新一轮一开始就被上限结束。
    """
    assert _route(scripted, '{"next": "coder"}', attempts=8).goto == END
    assert _route(scripted, '{"next": "coder"}', attempts=0).goto == "coder"


def test_answer_ids_do_not_collide_across_turns(scripted):
    """回归：回答的 id 必须逐轮唯一。

    曾经用 `supervisor-answer-{attempts}` 当确定性 id，而 attempts 每轮都会重置，
    于是第二轮的 id 与第一轮**相同** → `add_messages` 按 id 去重，把新回答
    **覆盖到对话开头的旧位置**，CLI 取"最后一条 AI 消息"时拿到的还是上一轮的旧回答；
    同时那条被覆盖的历史消息也被污染了。
    """
    llm = scripted([
        AIMessage(content='{"next": "finish"}'), AIMessage(content="回答A"),
        AIMessage(content='{"next": "finish"}'), AIMessage(content="回答B"),
    ])
    node = make_supervisor(llm)

    turn1 = node({"messages": [HumanMessage("任务1", id="msg-1")], "attempts": 0})
    turn2 = node({
        "messages": [HumanMessage("任务1", id="msg-1"), HumanMessage("任务2", id="msg-2")],
        "attempts": 0,   # 每轮都从 0 开始（CLI 会重置）
    })

    assert turn1.update["messages"][0].id != turn2.update["messages"][0].id
    assert "回答A" in turn1.update["messages"][0].content
    assert "回答B" in turn2.update["messages"][0].content


def test_digest_is_bounded(scripted):
    """会话越聊越长，喂给 supervisor 的摘要必须有长度上限（否则每轮都变慢变贵）。"""
    from code_agent.supervisor import MAX_DIGEST_CHARS, _digest

    messages = [HumanMessage("最初的任务")] + [AIMessage(content="汇报" * 300) for _ in range(20)]
    digest = _digest(messages)

    assert len(digest) <= MAX_DIGEST_CHARS + 100
    assert "最初的任务" in digest, "开头的原始任务应被保留"
    assert "已省略" in digest
