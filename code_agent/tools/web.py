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
from code_agent.tools._util import is_transient, safe, with_retry

MAX_RESULTS = 3
MAX_CHARS = 2000
MAX_CONTENT_CHARS = 400


def _retry_reason(result) -> str:
    """该重试就返回原因（错误类型名，会进重试日志）；不该重试返回空字符串。

    Tavily **不抛异常**，而是把错误装进返回值：`{"error": Exception(...)}`。
    所以要把里面的异常取出来再判断是不是瞬时的 —— 否则重试永远不会触发（实测踩到）。
    401（key 不对）是 `ValueError` → 不重试；连不上是 `ConnectionError` → 重试。
    """
    if not isinstance(result, dict):
        return ""
    error = result.get("error")
    if isinstance(error, BaseException) and is_transient(error):
        return type(error).__name__
    return ""


def _format(result) -> str:
    """把 Tavily 的返回压成紧凑文本。"""
    if isinstance(result, dict) and result.get("error"):
        # 把错误如实回传给模型，让它自己决定下一步（换关键词 / 说明查不到）
        return f"Error: 联网检索失败：{result['error']}"
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

        网络抖动/限流会自动重试；重试用尽则把错误返回给模型自行判断。

        Args:
            query: 检索关键词。
        """
        # 重试只包住网络调用这一句：检索是幂等的，重试安全。
        # retry_if 用来识别 Tavily"把错误包在返回值里"的情况。
        # 重试用尽后仍把错误当成结果返回，由模型自己判断下一步。
        result = with_retry(tavily.invoke, {"query": query},
                            retry_if=_retry_reason, label="web_search")
        return _format(result)

    return [web_search]
