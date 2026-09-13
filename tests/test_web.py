"""联网检索工具的启用条件与输出截断（不需要网络）。"""

from __future__ import annotations

from code_agent.config import Settings
from code_agent.tools.web import MAX_CHARS, _format, build_web_tools


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
