"""工具重试：只重试瞬时错误，且绝不吞掉 LangGraph 的控制流异常（纯函数，不联网）。"""

from __future__ import annotations

import time

import httpx
import pytest
from langgraph.errors import GraphBubbleUp

from code_agent.tools._util import is_transient, with_retry


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """测试里别真的等退避。"""
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


def test_transient_error_is_retried_then_succeeds():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("连接失败")
        return "ok"

    assert with_retry(flaky) == "ok"
    assert calls["n"] == 3


def test_exhausts_attempts_then_raises():
    calls = {"n": 0}

    def always_timeout():
        calls["n"] += 1
        raise httpx.TimeoutException("超时")

    with pytest.raises(httpx.TimeoutException):
        with_retry(always_timeout, attempts=3)
    assert calls["n"] == 3, "应该正好试 3 次"


def test_non_transient_error_is_not_retried():
    calls = {"n": 0}

    def bad():
        calls["n"] += 1
        raise ValueError("参数写错了")

    with pytest.raises(ValueError):
        with_retry(bad, attempts=3)
    assert calls["n"] == 1, "非瞬时错误重试没有意义，只该试一次"


def test_graph_bubble_up_is_never_retried():
    """回归：控制流异常一旦被重试逻辑吞掉，HITL 会静默失效（实测踩过）。

    `interrupt()` 抛的 `GraphInterrupt` 是 `GraphBubbleUp` 的子类，而它也继承自
    `Exception` —— 所以只要分类写得不小心，就会被当成"可重试的失败"。
    """
    calls = {"n": 0}

    def interrupted():
        calls["n"] += 1
        raise GraphBubbleUp()

    with pytest.raises(GraphBubbleUp):
        with_retry(interrupted, attempts=3)
    assert calls["n"] == 1, "GraphBubbleUp 连一次重试都不该有"


def test_retries_when_error_is_wrapped_in_the_result():
    """有些库**不抛异常**，而是把错误包在返回值里（Tavily 就是 `{"error": ...}`）。

    `retry_if` 就是为这种情况准备的 —— 没有它，重试逻辑是**死的**（实测踩到：
    单测全过，但真实调用永远不会重试）。
    """
    calls = {"n": 0}

    def returns_error():
        calls["n"] += 1
        if calls["n"] < 3:
            return {"error": ConnectionError("连不上")}
        return {"results": [{"title": "ok"}]}

    result = with_retry(returns_error, retry_if=lambda r: "error" in r)
    assert result["results"]
    assert calls["n"] == 3


def test_wrapped_error_result_is_returned_after_exhausting():
    calls = {"n": 0}

    def always_error():
        calls["n"] += 1
        return {"error": ConnectionError("连不上")}

    result = with_retry(always_error, attempts=2, retry_if=lambda r: "error" in r)
    assert "error" in result, "重试用尽应把（带错误的）结果交出去，由模型判断，而不是抛异常"
    assert calls["n"] == 2


def test_retry_leaves_a_log(capsys):
    """重试必须留日志，否则线上失败时看不出它重试过。"""
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.ReadError("读失败")
        return "ok"

    with_retry(flaky)
    assert "[retry]" in capsys.readouterr().out


@pytest.mark.parametrize(
    "exc, expected",
    [
        (httpx.TimeoutException("x"), True),
        (httpx.ConnectError("x"), True),
        (httpx.RemoteProtocolError("x"), True),
        (ConnectionError("x"), True),
        (TimeoutError("x"), True),
        # 网络类 OSError 也必须算瞬时 —— 很多库（requests 等）的错误继承自 OSError，
        # 却**不是**内置 ConnectionError 的子类
        (ConnectionResetError("x"), True),
        (BrokenPipeError("x"), True),
        # 这些重试没意义
        (ValueError("x"), False),
        (FileNotFoundError("x"), False),
        (PermissionError("x"), False),
        (NotADirectoryError("x"), False),
    ],
)
def test_is_transient_classification(exc, expected):
    assert is_transient(exc) is expected


def test_requests_style_connection_error_is_transient():
    """回归：Tavily 把网络故障包成 `requests.exceptions.ConnectionError`，
    它的 MRO 是 `ConnectionError → RequestException → OSError` ——
    只认内置 ConnectionError 会让真实网络故障永不重试（实测踩到）。
    """
    import requests.exceptions

    error = requests.exceptions.ConnectionError(
        "HTTPSConnectionPool(host='127.0.0.1', port=9): Max retries exceeded"
    )
    assert isinstance(error, OSError)
    assert not isinstance(error, ConnectionError)  # 关键是它「不是」内置 ConnectionError
    assert is_transient(error) is True


@pytest.mark.parametrize("status, expected", [(429, True), (503, True), (401, False), (404, False)])
def test_http_status_classification(status, expected):
    request = httpx.Request("GET", "https://example.invalid")
    error = httpx.HTTPStatusError(
        str(status), request=request, response=httpx.Response(status, request=request)
    )
    assert is_transient(error) is expected
