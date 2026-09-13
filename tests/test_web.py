"""联网检索工具的启用条件与输出截断（不需要网络）。"""

from __future__ import annotations

from code_agent.config import Settings
from code_agent.tools.web import MAX_CHARS, _format, _retry_reason, build_web_tools


def _settings(tavily_key: str | None) -> Settings:
    return Settings(
        base_url="http://example.invalid",
        auth_token="fake",
        model="fake",
        tavily_api_key=tavily_key,
    )


def test_no_key_disables_web_search():
    """没配 TAVILY_API_KEY 时不该暴露 web_search（也不该报错）。"""
    assert build_web_tools(_settings(None)) == []


def test_with_key_exposes_web_search():
    tools = build_web_tools(_settings("tvly-fake"))
    assert [tool.name for tool in tools] == ["web_search"]


def test_format_truncates_long_content():
    """Tavily 返回的正文很长，必须截断，否则会撑爆上下文。"""
    result = {"results": [{"title": "t", "url": "u", "content": "x" * 5000}]}
    assert len(_format(result)) <= MAX_CHARS


def test_format_handles_plain_string():
    assert _format("plain") == "plain"


def test_format_handles_empty_results():
    assert _format({}) == "（无结果）"


def test_retry_reason_reads_the_wrapped_exception():
    """Tavily 不抛异常，把错误包在返回值里 —— 要取出里面的异常再分类。

    返回的是"原因字符串"（会进重试日志），空字符串表示不该重试。
    """
    assert _retry_reason({"error": ConnectionResetError("连不上")}) == "ConnectionResetError"
    assert _retry_reason({"error": ValueError("401 Unauthorized")}) == ""
    assert _retry_reason({"results": []}) == ""
    assert _retry_reason("普通字符串") == ""


def test_format_returns_wrapped_error_to_the_model():
    """重试用尽后，错误要如实回传给模型（而不是变成"（无结果）"让它瞎猜）。"""
    out = _format({"error": ValueError("Error 401: Unauthorized")})
    assert out.startswith("Error:")
    assert "401" in out
