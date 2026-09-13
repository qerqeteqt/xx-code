"""父图共享 State。

**方案 B**：共享的 `messages` 里**只保留「人话」** —— 用户任务、supervisor 指令、
各成员的最终汇报。worker 内部的工具调用与工具结果会被 supervisor 在下一次运行开始时
**剪掉**（见 `supervisor.prune_scratchpad`），从而止住上下文膨胀、消除交叉污染
（实测 coder 会照着 verifier 的 `run_command` 去调一个它没有的工具）。

因为草稿会被剪掉，"是否已改动过代码"就不能再靠翻消息判断，
所以单独用一个 `made_edits` 记录 —— 由 supervisor 观察到编辑调用时置位。

注意 LangGraph 的子图状态过滤规则：worker 子图用的是默认 `AgentState`（仅 messages），
所以跨子图边界流动的只有 messages；父图额外声明的 `attempts` 与 `made_edits` 对子图不可见。
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph import add_messages


class OverallState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    attempts: int
    made_edits: bool
