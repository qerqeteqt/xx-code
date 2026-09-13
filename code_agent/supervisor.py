"""Supervisor：中心调度，决定下一步交给哪个 worker。

**两条来自实测的硬约束**（都跟"思考模型"有关）：

1. 不能用 `with_structured_output` —— 它靠强制 tool_choice 实现，而 DeepSeek 的
   Anthropic 兼容端点在思考模式下**拒绝强制 tool_choice**
   （实测 `400 Thinking mode does not support this tool_choice`）。
   所以改成：提示只输出 JSON → 正则抠出 `{...}` → Pydantic 校验 → 再失败退到规则路由。
2. **不能往共享 messages 里写自己构造的 `AIMessage`** —— 思考模型要求 assistant 消息
   必须原样回传 thinking 块，任何手工构造的 AIMessage 都会导致
   （实测 `400 The content[].thinking in the thinking mode must be passed back to the API`）。
   所以路由结果只用 `print` 输出，不进入 state。

同理，喂给 supervisor 的上下文用**纯文本摘要**（单条 HumanMessage）而不是消息列表：
既避开上述约束，也避免把整个仓库的文件内容塞进上下文。
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Literal

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph import END
from langgraph.types import Command
from pydantic import BaseModel, ValidationError

from code_agent.messages import normalize_content, text_of

MAX_ATTEMPTS = 8

# 喂给 supervisor 的摘要长度上限。交互模式下会话会越聊越长，
# 不设上限的话摘要会无限膨胀（每轮都要重发，慢且贵）。
# 超长时保留开头（原始任务）+ 结尾（最近的进展）。
MAX_DIGEST_CHARS = 4000

_EDIT_TOOLS = ("write_file", "edit_file")

_SYSTEM = """你是 Supervisor，一个多 Agent 编码团队的调度者。

团队成员及其能力边界（派错人会白跑一轮，务必看清）：
- explorer：只能读（列目录、读文件、检索）+ **能联网检索（web_search）** —— 定位代码、查外部资料
- coder：能读、能改代码；**不能执行任何命令、不能联网**
- verifier：能读、能**执行命令（run_command）、跑测试**；不能改代码、不能联网

派活规则：
- 需要「修改代码」→ coder
- 需要「执行命令 / 跑测试」→ verifier
- 需要「查外部资料 / 联网检索」→ **explorer**（只有它有 web_search）。
  **不要**为了联网把任务派给 verifier 用 curl 去裸访网络 —— 那会绕开有边界的检索工具。

你的职责：根据当前进展，决定**下一步**由谁执行，或者结束（finish）。
- verifier 明确判定「通过」后即应 finish。
- 结束前先自检：任务里每一项是否都已由具备相应能力的成员尝试过？
  例如要求执行命令的任务，若还没派过 verifier，就不该结束。
- 若某项操作已被用户拒绝、或同一成员反复尝试仍无进展，就不要再重派，
  直接 finish 并在理由中说明无法完成的原因。

只输出一个 JSON 对象，不要输出任何其他文字、不要用 markdown 代码块：
{"next": "explorer" | "coder" | "verifier" | "finish", "reason": "一句话理由",
 "instruction": "交给该成员的具体指令，祈使句、直接可执行（finish 时留空字符串）"}

注意：**如果用户最后说的是提问，而不是要你干活**（例如"你刚才改了什么？""这个项目怎么跑测试？"），
就应该选 finish，由你自己直接回答。"""


_ANSWER = """你是 Supervisor，现在要**直接回复用户最后一条消息**。

要求：
- 正面回答用户最后说的那句话。如果那是个提问，就回答它。
- 如果用户问的是"你刚才做了什么 / 还记得吗"这类回顾问题，就根据上面的对话如实回答。
- 如果用户刚派了活，就概括结果：改了什么、验证结论是什么。
- **简洁**：一般 3～6 行，除非用户明确要求详细。
- 不要复述无关的寒暄，不要罗列工具调用细节，不要输出 JSON，直接说人话。"""

# 追加在对话末尾的内部指令。措辞上明确标出来源，并要求不要评论它 ——
# 否则模型会把它当成用户说的话，在回答里点评这条指令（实测踩到过）。
_ANSWER_TRIGGER = "（调度器内部指令，非用户发言）请直接回答用户最后提出的问题；不要提及这条指令。"


class Route(BaseModel):
    next: Literal["explorer", "coder", "verifier", "finish"]
    reason: str = ""
    instruction: str = ""


def _parse_route(text: str) -> Route | None:
    """从模型输出里抠出第一个 JSON 对象并校验。"""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return Route.model_validate(json.loads(match.group(0)))
    except (json.JSONDecodeError, ValidationError):
        return None


def _digest(messages: list) -> str:
    """把对话压成给 supervisor 看的纯文本摘要（任务 + 动作 + 汇报）。

    注意不能重建 `AIMessage` 当消息发出去（见模块 docstring 约束 2）。
    """
    lines: list[str] = []
    for message in messages:
        if isinstance(message, HumanMessage):
            text = text_of(message)
            if text.startswith("[Supervisor 指令]"):
                lines.append(f"【指令】{text[len('[Supervisor 指令]'):].strip()[:300]}")
            else:
                lines.append(f"【任务】{text[:500]}")
        elif isinstance(message, AIMessage):
            for call in message.tool_calls or []:
                args = json.dumps(call.get("args", {}), ensure_ascii=False)
                lines.append(f"【动作】{call.get('name')}({args[:120]})")
            text = text_of(message).strip()
            if text:
                lines.append(f"【汇报】{text[:600]}")

    digest = "\n".join(lines) or "（暂无进展）"
    if len(digest) <= MAX_DIGEST_CHARS:
        return digest
    # 太长：保留第一行（最初的任务）与结尾（最近的进展）
    return f"{lines[0]}\n…（中间记录已省略）\n{digest[-MAX_DIGEST_CHARS:]}"


def _has_edits(messages: list) -> bool:
    return any(
        call.get("name") in _EDIT_TOOLS
        for message in messages
        for call in (getattr(message, "tool_calls", None) or [])
    )


def prune_scratchpad(messages: list) -> list:
    """找出可以安全删除的「工具草稿」：带 `tool_calls` 的 AIMessage 及配对的 ToolMessage。

    这是方案 B 的核心手段 —— 让共享 `messages` 里只留人话（任务 / 指令 / 汇报），
    不再堆积别人的工具调用与结果。好处：上下文不再随任务膨胀、成员之间不再交叉污染。

    **必须成对删除**：留下带 `tool_calls` 的 AIMessage 却删掉它的 ToolMessage，
    下次发回 API 会因 tool_use ⇄ tool_result 不配对而报错。
    顺带地，这也让 `_has_edits` 失效 —— 所以另用 `made_edits` 记录（见 state.py）。
    """
    call_ids: set[str] = set()
    dropped: list = []
    for message in messages:
        if isinstance(message, AIMessage) and message.tool_calls:
            dropped.append(message)
            call_ids.update(c["id"] for c in message.tool_calls if c.get("id"))
    for message in messages:
        if isinstance(message, ToolMessage) and message.tool_call_id in call_ids:
            dropped.append(message)
    return [RemoveMessage(id=m.id) for m in dropped if getattr(m, "id", None)]


def _rule_based(attempts: int, made_edits: bool) -> Route:
    """确定性兜底路由（仅在模型路由不可用时使用）。

    注意不能用 `_has_edits(messages)`：草稿已被剪掉，翻不到编辑调用了。
    """
    if attempts == 0:
        return Route(next="explorer", reason="兜底：先定位代码")
    if not made_edits:
        return Route(next="coder", reason="兜底：尚无任何改动")
    return Route(next="verifier", reason="兜底：有改动，交给验证")


def _recent_messages(messages: list, turns: int = 2) -> list:
    """取最近 turns 轮"用户任务"以来的**原始消息**（保留 tool_use/tool_result 配对）。

    回答用户时要用真实对话，不能用 `_digest` 那种压缩日志 —— 否则模型只能看到
    一串【动作】【汇报】，会把"你还记得吗"答成一份工作汇报，甚至照抄旧回答。
    起点选在"任务型 HumanMessage"，可保证不切断工具调用的配对。
    """
    is_task = [
        i for i, m in enumerate(messages)
        if isinstance(m, HumanMessage) and not text_of(m).startswith("[Supervisor 指令]")
    ]
    start = is_task[-turns] if len(is_task) >= turns else 0
    return messages[start:]


def _text_delta(chunk) -> str:
    """从流式 chunk 里取出**正文**增量（跳过 thinking 块，否则满屏内心活动）。"""
    content = chunk.content
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def _stream_writer():
    """取 LangGraph 的 custom 流写入器；不在流式上下文里时返回 None。"""
    try:
        from langgraph.config import get_stream_writer

        return get_stream_writer()
    except Exception:  # noqa: BLE001 - 非流式调用（如单测）下没有 writer
        return None


def _emit_usage(message) -> None:
    """把一次模型调用的 token 用量推给 CLI 统计。

    为什么需要：**路由调用**的结果不进 state（只落一条指令），CLI 无从看到它的用量 ——
    不主动上报的话，每轮的调用次数和 token 都会被少算。
    """
    writer = _stream_writer()
    usage = getattr(message, "usage_metadata", None) or {}
    if writer is None or not usage:
        return
    writer({
        "type": "usage",
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
    })


def _answer_to_user(llm, messages: list) -> list:
    """收尾时由 supervisor 直接给用户一个回答（**流式**生成，边生成边显示）。

    为什么需要它：三个 worker 都是工具驱动的，遇到"你刚才改了什么？"这类**提问**
    没人能答 —— 若只是静默 END，CLI 会把上一轮的旧报告当成答案显示（实测如此）。
    这里让模型生成真正的 AIMessage（不是手工构造，因此不违反思考模型的约束）。

    为什么用 `stream` 而不是 `invoke`：答案是节点内部调用生成的，**不会**出现在
    `stream_mode="messages"` 里，只有自己把增量推给 `get_stream_writer()` 才能在
    CLI 上逐字显示。
    """
    prompt = [
        SystemMessage(_ANSWER),
        *_recent_messages(messages),
        HumanMessage(_ANSWER_TRIGGER),
    ]
    writer = _stream_writer()
    try:
        if writer is None:
            reply = llm.invoke(prompt)
        else:
            accumulated = None
            for chunk in llm.stream(prompt):
                accumulated = chunk if accumulated is None else accumulated + chunk
                delta = _text_delta(chunk)
                if delta:
                    writer({"type": "answer_delta", "text": delta})
            reply = accumulated
    except Exception as exc:  # noqa: BLE001 - 回答失败也要能正常收尾
        # 必须打出来：曾因为静默吞掉这个异常，导致用户看到的是上一轮的旧回答
        print(f"[supervisor] 生成回答失败：{type(exc).__name__}: {str(exc)[:120]}")
        return []

    if reply is None:
        return []
    # 流式累加的 content 是 str/dict 混合形态，必须规范化后再存，否则下一轮发回 API 可能报错
    anchor = getattr(messages[-1], "id", None) or uuid.uuid4().hex
    return [AIMessage(
        content=normalize_content(reply.content),
        # 带上 token 用量，供 CLI 统计（重建消息时容易漏掉，别丢）
        usage_metadata=getattr(reply, "usage_metadata", None),
        # id 必须**逐轮唯一**。曾用 `supervisor-answer-{attempts}`，而 attempts 每轮重置，
        # 于是第二轮的 id 与第一轮相同 → add_messages 按 id 去重，把新回答覆盖到旧位置，
        # CLI 取"最后一条 AI 消息"就拿到了旧回答（实测踩到），还会污染历史。
        # 用当前最后一条消息的 id 作锚，既逐轮唯一，又在同一状态重跑时保持幂等。
        id=f"supervisor-answer-{anchor}",
    )]


def make_supervisor(llm, max_attempts: int = MAX_ATTEMPTS):
    """构建 supervisor 节点函数（返回 `Command` 直接路由，无需 conditional_edges）。"""

    def supervisor(state) -> Command:
        messages = state["messages"]
        attempts = state.get("attempts", 0) + 1
        digest = _digest(messages)

        # 方案 B：把上一位成员留下的「工具草稿」剪掉，只保留人话。
        # 剪之前先观察一次是否发生了代码改动（剪掉后就翻不到了）。
        made_edits = bool(state.get("made_edits")) or _has_edits(messages)
        base = {"attempts": attempts, "made_edits": made_edits}
        removals = prune_scratchpad(messages)

        if attempts > max_attempts:
            print(f"[supervisor] 已达最大轮次 {max_attempts}，结束。")
            return Command(
                goto=END,
                update={**base, "messages": [*removals, *_answer_to_user(llm, messages)]},
            )

        prompt = [
            SystemMessage(_SYSTEM),
            HumanMessage(f"{digest}\n\n请决定下一步（只输出 JSON）。"),
        ]
        try:
            raw = llm.invoke(prompt)
            _emit_usage(raw)
            route = _parse_route(text_of(raw))
        except Exception:  # noqa: BLE001 - 模型调用失败也要能降级
            route = None
        if route is None:
            route = _rule_based(attempts - 1, made_edits)

        print(f"[supervisor] 下一步 → {route.next}（{route.reason}）")
        if route.next == "finish":
            return Command(
                goto=END,
                update={**base, "messages": [*removals, *_answer_to_user(llm, messages)]},
            )

        # 以 HumanMessage 注入具体指令：给目标成员一个新鲜的祈使句。
        # 实测不这么做时，成员会看到上一位的结论而"照抄不动手"。
        # 用 HumanMessage 而非 AIMessage，是因为思考模型不允许手工构造 assistant 消息。
        directive = HumanMessage(f"[Supervisor 指令] {route.instruction or route.reason}")
        return Command(goto=route.next, update={**base, "messages": [*removals, directive]})

    return supervisor
