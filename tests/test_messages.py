"""消息内容解析（纯函数）。

回归重点：**流式累加**出来的 content 是 str/dict 混合形态
（实测 `['', {'type': 'thinking', ...}, 'Python 是一种...']`），
而 `invoke` 出来的是规整的 block 列表。两种都得认，否则流式消息会被读成空字符串。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from code_agent.messages import normalize_content, text_of


def test_text_of_plain_string():
    assert text_of(AIMessage(content="普通字符串")) == "普通字符串"


def test_text_of_block_list():
    message = AIMessage(content=[
        {"type": "thinking", "thinking": "内心活动", "signature": "sig"},
        {"type": "text", "text": "正文"},
    ])
    assert text_of(message) == "正文"


def test_text_of_streamed_mixed_content():
    """流式累加形态：正文是裸字符串，非正文才是 dict。"""
    message = AIMessage(content=[
        "",
        {"type": "thinking", "thinking": "内心活动", "signature": "sig"},
        "Python 是一种编程语言。",
    ])
    assert text_of(message) == "Python 是一种编程语言。"


def test_normalize_content_converts_bare_strings():
    assert normalize_content(["", "正文"]) == [{"type": "text", "text": "正文"}]


def test_normalize_content_keeps_blocks_intact():
    thinking = {"type": "thinking", "thinking": "内心活动", "signature": "sig"}
    assert normalize_content([thinking, "正文"]) == [thinking, {"type": "text", "text": "正文"}]


def test_normalize_content_handles_plain_string():
    assert normalize_content("正文") == [{"type": "text", "text": "正文"}]
    assert normalize_content("") == []
