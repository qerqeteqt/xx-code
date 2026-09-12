"""消息处理辅助。"""

from __future__ import annotations


def text_of(message) -> str:
    """取出消息中的纯文本。

    本模型是思考模型，`AIMessage.content` **不是字符串**，而是 block 列表，
    形如 `[{"type": "thinking", ...}, {"type": "text", "text": "..."}]`。
    因此必须筛出 `type == "text"` 的块，不能直接当字符串用。
    """
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    return "".join(
        block.get("text", "")
        for block in (content or [])
        if isinstance(block, dict) and block.get("type") == "text"
    )
