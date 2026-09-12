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
from typing import Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END
from langgraph.types import Command
from pydantic import BaseModel, ValidationError

from code_agent.messages import text_of

MAX_ATTEMPTS = 8

_EDIT_TOOLS = ("write_file", "edit_file")

_SYSTEM = """你是 Supervisor，一个多 Agent 编码团队的调度者。

团队成员及其能力边界（派错人会白跑一轮，务必看清）：
- explorer：只能读（列目录、读文件、检索）——用于定位代码
- coder：能读、能改代码；**不能执行任何命令**
- verifier：能读、能**执行命令（run_command）、跑测试**；不能改代码

因此：需要「执行命令 / 跑测试」时必须派 verifier；需要「修改代码」时派 coder。

你的职责：根据当前进展，决定**下一步**由谁执行，或者结束（finish）。
- verifier 明确判定「通过」后即应 finish。
- 结束前先自检：任务里每一项是否都已由具备相应能力的成员尝试过？
  例如要求执行命令的任务，若还没派过 verifier，就不该结束。
- 若某项操作已被用户拒绝、或同一成员反复尝试仍无进展，就不要再重派，
  直接 finish 并在理由中说明无法完成的原因。

只输出一个 JSON 对象，不要输出任何其他文字、不要用 markdown 代码块：
{"next": "explorer" | "coder" | "verifier" | "finish", "reason": "一句话理由",
 "instruction": "交给该成员的具体指令，祈使句、直接可执行（finish 时留空字符串）"}"""


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
    return "\n".join(lines) or "（暂无进展）"


def _has_edits(messages: list) -> bool:
    return any(
        call.get("name") in _EDIT_TOOLS
        for message in messages
        for call in (getattr(message, "tool_calls", None) or [])
    )


def _rule_based(messages: list, attempts: int) -> Route:
    """确定性兜底路由（仅在模型路由不可用时使用）。"""
    if attempts == 0:
        return Route(next="explorer", reason="兜底：先定位代码")
    if not _has_edits(messages):
        return Route(next="coder", reason="兜底：尚无任何改动")
    return Route(next="verifier", reason="兜底：有改动，交给验证")


def make_supervisor(llm, max_attempts: int = MAX_ATTEMPTS):
    """构建 supervisor 节点函数（返回 `Command` 直接路由，无需 conditional_edges）。"""

    def supervisor(state) -> Command:
        messages = state["messages"]
        attempts = state.get("attempts", 0) + 1

        if attempts > max_attempts:
            print(f"[supervisor] 已达最大轮次 {max_attempts}，结束。")
            return Command(goto=END, update={"attempts": attempts})

        prompt = [
            SystemMessage(_SYSTEM),
            HumanMessage(f"{_digest(messages)}\n\n请决定下一步（只输出 JSON）。"),
        ]
        try:
            route = _parse_route(text_of(llm.invoke(prompt)))
        except Exception:  # noqa: BLE001 - 模型调用失败也要能降级
            route = None
        if route is None:
            route = _rule_based(messages, attempts - 1)

        print(f"[supervisor] 下一步 → {route.next}（{route.reason}）")
        goto = END if route.next == "finish" else route.next
        if goto == END:
            return Command(goto=END, update={"attempts": attempts})
        # 以 HumanMessage 注入具体指令：给目标成员一个新鲜的祈使句。
        # 实测不这么做时，成员会看到上一位的结论而"照抄不动手"（方案 A 共享历史的固有代价）。
        # 用 HumanMessage 而非 AIMessage，是因为思考模型不允许手工构造 assistant 消息。
        directive = HumanMessage(f"[Supervisor 指令] {route.instruction or route.reason}")
        return Command(goto=goto, update={"attempts": attempts, "messages": [directive]})

    return supervisor
