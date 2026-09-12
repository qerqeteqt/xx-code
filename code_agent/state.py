"""父图共享 State。

v1（方案 A）：所有 agent 共享 `messages` 黑板 —— worker 的文字汇报写进 messages，
supervisor 读 messages 决定下一步。因此父图只需要 `messages` + 一个轮次计数器。

注意 LangGraph 的子图状态过滤规则：worker 子图用的是默认 `AgentState`（仅 messages），
所以跨子图边界流动的只有 messages；父图额外声明的 `attempts` 对子图不可见。
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph import add_messages


class OverallState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    attempts: int
