"""消息处理辅助。"""

from __future__ import annotations


def text_of(message) -> str:
    """取出消息中的纯文本。

    本模型是思考模型，`AIMessage.content` **不是字符串**，而是 block 列表，
    形如 `[{"type": "thinking", ...}, {"type": "text", "text": "..."}]`。

    但**流式累加**出来的消息格式又不一样：正文是**裸字符串**、非正文才是 dict
    （实测 `['', {'type': 'thinking', ...}, 'Python 是一种...']`）。
    所以两种形态都要认，否则流式消息会被读成空字符串。
    """
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def normalize_content(content) -> list:
    """把流式累加出来的 content 规范成标准 block 列表。

    流式累加会混进裸字符串（且含空串），而发回 API 时期望的是
    `[{"type": "text", "text": ...}, ...]` 这种规整结构 —— 不规范化下一轮可能报错。
    """
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    normalized: list = []
    for block in content or []:
        if isinstance(block, dict):
            normalized.append(block)
        elif isinstance(block, str) and block:
            normalized.append({"type": "text", "text": block})
    return normalized
