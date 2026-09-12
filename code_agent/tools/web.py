"""联网检索工具（Tavily）。

两个设计决定：
1. 未配置 `TAVILY_API_KEY` 时返回**空列表**，`web_search` 自动禁用（不报错、不影响其余功能）。
2. 不用裸的 `TavilySearch`，而是包一层 `@tool` + `safe`：
   - 保证"工具绝不抛异常"这条硬性约束（网络失败会返回错误字符串，而不是崩掉整个 run）
   - 对结果做**源头截断**（Tavily 返回的正文很长，直接进上下文会撑爆）
"""

from __future__ import annotations

from langchain_core.tools import tool

from code_agent.config import Settings
from code_agent.tools._util import safe

MAX_RESULTS = 3
MAX_CHARS = 2000
MAX_CONTENT_CHARS = 400


def _format(result) -> str:
    """把 Tavily 的返回压成紧凑文本。"""
    if isinstance(result, str):
        return result[:MAX_CHARS]

    lines: list[str] = []
    answer = result.get("answer")
    if answer:
        lines.append(f"摘要: {answer}")
    for item in (result.get("results") or [])[:MAX_RESULTS]:
        title = str(item.get("title", ""))[:120]
        url = str(item.get("url", ""))
        content = str(item.get("content", "")).replace("\n", " ")[:MAX_CONTENT_CHARS]
        lines.append(f"- {title}\n  {url}\n  {content}")

    return ("\n".join(lines) or "（无结果）")[:MAX_CHARS]


def build_web_tools(settings: Settings) -> list:
    """构建联网检索工具；没有 key 时返回空列表。"""
    if not settings.tavily_api_key:
        return []

    from langchain_tavily import TavilySearch

    tavily = TavilySearch(max_results=MAX_RESULTS, tavily_api_key=settings.tavily_api_key)

    @tool
    @safe
    def web_search(query: str) -> str:
        """联网检索外部资料。

        用于查询仓库里查不到的信息：第三方库用法、报错含义、API 文档等。
        优先用仓库内检索，确认本地没有答案后再联网。

        Args:
            query: 检索关键词。
        """
        return _format(tavily.invoke({"query": query}))

    return [web_search]
