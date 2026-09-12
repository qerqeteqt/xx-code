"""工具层共用的辅助。"""

from __future__ import annotations

import functools
from typing import Callable

from langgraph.errors import GraphBubbleUp


def safe(fn: Callable) -> Callable:
    """保证工具不抛异常：任何异常都转成 "Error: ..." 字符串返回给模型。

    必须兜住，因为 `create_agent` 默认只把参数校验错误转成 ToolMessage，
    工具体里抛出的其他异常会冒泡崩掉整个 LangGraph run。

    但 **`GraphBubbleUp` 必须放行**：`interrupt()` 抛的 `GraphInterrupt` 是它的子类，
    而它也继承自 `Exception` —— 若一并吞掉，HITL 暂停将永远不触发。
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except GraphBubbleUp:
            raise  # LangGraph 控制流异常（interrupt 等），必须放行
        except Exception as exc:  # noqa: BLE001 - 有意兜住其余全部异常
            return f"Error: {type(exc).__name__}: {exc}"

    return wrapper

