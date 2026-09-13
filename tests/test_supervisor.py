"""supervisor 路由逻辑（不联网，直接对节点函数断言）。

这是重构时最容易悄悄弄坏的一块：路由错了，图还是会跑完，只是跑到错的成员那里。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import END

from code_agent.messages import text_of
from code_agent.supervisor import _SYSTEM, make_supervisor, prune_scratchpad


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
    assert "改了 calc.py" in text_of(reply)
    # id 逐轮唯一（锚在当前最后一条消息的 id 上），既是 resume 幂等、又不会跨轮覆盖
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
    assert "回答A" in text_of(turn1.update["messages"][0])
    assert "回答B" in text_of(turn2.update["messages"][0])


def test_prune_removes_tool_traffic_but_keeps_reports():
    """方案 B：剪掉工具草稿，只留人话（任务 / 汇报）。"""
    task = HumanMessage("任务", id="h1")
    tool_ai = AIMessage(
        content="", id="a1",
        tool_calls=[{"name": "read_file", "args": {}, "id": "c1"}],
    )
    tool_result = ToolMessage(content="文件内容", tool_call_id="c1", id="t1")
    report = AIMessage(content="我读了 calc.py", id="a2")

    removed = {m.id for m in prune_scratchpad([task, tool_ai, tool_result, report])}

    assert removed == {"a1", "t1"}, "带 tool_calls 的 AIMessage 必须与它的 ToolMessage 成对剪掉"
    assert "h1" not in removed, "用户任务要留"
    assert "a2" not in removed, "纯文本汇报要留"


def test_prune_handles_unpaired_tool_call():
    """没有配对结果的调用也要整个删掉，不能只删一半（否则下次发回 API 会报配对错误）。"""
    only_call = AIMessage(
        content="", id="a9",
        tool_calls=[{"name": "x", "args": {}, "id": "c9"}],
    )
    assert {m.id for m in prune_scratchpad([only_call])} == {"a9"}


def test_made_edits_is_recorded_even_though_scratchpad_is_pruned(scripted):
    """剪掉草稿后"是否改过代码"就翻不到了 —— 必须由 supervisor 在同一批里记进 state。"""
    edited = AIMessage(
        content="", id="a1",
        tool_calls=[{"name": "edit_file", "args": {}, "id": "e1"}],
    )
    cmd = _route(scripted, '{"next": "explorer", "reason": "r"}', extra=[edited])

    assert cmd.update["made_edits"] is True
    assert "a1" in {getattr(m, "id", None) for m in cmd.update["messages"]}, "同一批里要把它剪掉"


def test_capability_matrix_matches_worker_tools():
    """回归：supervisor 的提示词必须与各 worker 的**真实能力**对得上。

    踩过的坑：给 explorer 加上 `web_search` 之后，忘了更新 supervisor 的能力矩阵 →
    它根本不知道"联网检索"这项能力存在，于是把"联网查天气"派给了 verifier，
    而 verifier 只能用 `curl` 裸访网络 —— 绕过了有边界的检索工具。
    （提示词里的能力说明一旦和 `workers.py` 脱节，就会重复踩这个坑。）
    """
    for role in ("explorer", "coder", "verifier"):
        assert role in _SYSTEM, f"能力矩阵里要写明 {role}"

    assert "web_search" in _SYSTEM, "必须写明 explorer 能联网检索"
    assert "run_command" in _SYSTEM, "必须写明 verifier 能执行命令"

    web_line = next(ln for ln in _SYSTEM.splitlines() if "web_search" in ln)
    assert "explorer" in web_line, "要指明联网检索归 explorer"
    assert "curl" in _SYSTEM, "要明确禁止用 run_command + curl 裸访网络"


def test_digest_is_bounded(scripted):
    """会话越聊越长，喂给 supervisor 的摘要必须有长度上限（否则每轮都变慢变贵）。"""
    from code_agent.supervisor import MAX_DIGEST_CHARS, _digest

    messages = [HumanMessage("最初的任务")] + [AIMessage(content="汇报" * 300) for _ in range(20)]
    digest = _digest(messages)

    assert len(digest) <= MAX_DIGEST_CHARS + 100
    assert "最初的任务" in digest, "开头的原始任务应被保留"
    assert "已省略" in digest
