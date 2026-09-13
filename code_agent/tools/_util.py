"""工具层共用的辅助。"""

from __future__ import annotations

import functools
import random
import time
from typing import Callable

import httpx
from langgraph.errors import GraphBubbleUp

# 会被重试的异常：只放"过一会儿再试可能就好了"的网络类错误。
_TRANSIENT_EXC = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
    httpx.NetworkError,
    httpx.ProxyError,
    ConnectionError,
    TimeoutError,
)

# 这些 HTTP 状态码是瞬时的；401/403（key 不对）/404 之类重试没意义
_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504})

# 明确"重试也没用"的 OSError 子类（参数/权限问题）
_NOT_TRANSIENT_OSERROR = (
    FileNotFoundError,
    FileExistsError,
    PermissionError,
    NotADirectoryError,
    IsADirectoryError,
)


def is_transient(exc: BaseException) -> bool:
    """判断是不是"瞬时错误"（值得重试）。

    三类算瞬时：网络库异常、限流/网关状态码、以及**网络类的 `OSError`**。

    最后那条是实测补上的：Tavily 把底层错误包成
    `requests.exceptions.ConnectionError` —— 它**不是**内置 `ConnectionError` 的子类，
    而是 `OSError` 的孙子（`ConnectionError → RequestException → OSError`）。
    只认内置 `ConnectionError` 的话，**真实网络故障会被判成非瞬时、永不重试**（实测踩到）。

    其余一律当非瞬时（默认不重试更安全）。
    """
    if isinstance(exc, _TRANSIENT_EXC):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _TRANSIENT_STATUS
    if isinstance(exc, OSError) and not isinstance(exc, _NOT_TRANSIENT_OSERROR):
        return True
    return False


def _log_and_sleep(fn: Callable, attempt: int, reason: str, base_delay: float,
                   label: str | None = None) -> None:
    # 指数退避 + 抖动（避免多个调用同时重试撞在一起）
    delay = base_delay * (2 ** (attempt - 1)) * (1 + random.random() * 0.3)
    name = label or getattr(fn, "__name__", "tool")
    print(f"[retry] {name} 第 {attempt} 次失败（{reason}），{delay:.1f}s 后重试")
    time.sleep(delay)


def with_retry(fn: Callable, *args, attempts: int = 3, base_delay: float = 0.5,
               retry_if: Callable[[object], object] | None = None,
               label: str | None = None, **kwargs):
    """对**瞬时错误**重试；非瞬时错误立即抛出。

    **只用于幂等操作**（如联网检索）。千万不要套在 `run_command` 上 —— 命令不幂等，
    可能已经执行了一半（比如提交成功但读响应时超时），重试会造成重复副作用。

    `retry_if`：有些库**不抛异常**，而是把错误包在返回值里（Tavily 就是返回
    `{"error": Exception(...)}`）。传一个"这个结果算失败吗"的判断函数，
    才能在"返回了错误"时也触发重试 —— 否则重试逻辑是**死的**（实测踩到）。
    返回值可以是 `bool`，也可以是**字符串原因**（会出现在重试日志里，便于排查）。

    两条硬约束（都是踩过坑换来的）：

    1. **`GraphBubbleUp` 立即放行、绝不重试**。`interrupt()` 抛的 `GraphInterrupt`
       是它的子类，而它也继承自 `Exception` —— 一旦被重试逻辑吞掉，
       **HITL 暂停会静默失效**（命令没执行、用户却收不到确认提示，实测踩到过）。
    2. **重试要有日志**。否则线上失败时你根本看不出它重试过。
    """
    for attempt in range(1, attempts + 1):
        try:
            result = fn(*args, **kwargs)
        except GraphBubbleUp:
            raise  # LangGraph 控制流异常，绝不重试
        except Exception as exc:  # noqa: BLE001 - 分类后再决定
            if not is_transient(exc) or attempt >= attempts:
                raise
            _log_and_sleep(fn, attempt, type(exc).__name__, base_delay, label)
            continue

        # 没抛异常：可能错误藏在返回值里（见 retry_if 说明）
        reason = retry_if(result) if retry_if is not None else None
        if not reason or attempt >= attempts:
            return result
        _log_and_sleep(
            fn, attempt, reason if isinstance(reason, str) else "可重试的失败",
            base_delay, label,
        )

    raise AssertionError("unreachable")  # 循环内必定 return 或 raise


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

