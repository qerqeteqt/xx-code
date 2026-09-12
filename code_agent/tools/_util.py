"""工具层共用的辅助。"""

from __future__ import annotations

import functools
from typing import Callable


def safe(fn: Callable) -> Callable:
    """保证工具不抛异常：任何异常都转成 "Error: ..." 字符串返回给模型。

    必须兜住，因为 `create_agent` 默认只把参数校验错误转成 ToolMessage，
    工具体里抛出的其他异常会冒泡崩掉整个 LangGraph run。
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 有意兜住全部异常
            return f"Error: {type(exc).__name__}: {exc}"

    return wrapper
