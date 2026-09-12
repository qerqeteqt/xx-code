"""测试共用夹具。

`FakeMessagesListChatModel` 没实现 `bind_tools`（会抛 `NotImplementedError`），
所以子类化成 `ScriptedModel` 把 `bind_tools` 变成 no-op —— 响应是我们脚本写死的、
自带 `tool_calls`，不需要真的绑定工具 schema。

这样就能在**不联网、不花钱**的前提下驱动完整的 `create_agent` 与整张图，
对"改核心逻辑会不会悄悄弄坏调度/接线"提供回归保护。
"""

from __future__ import annotations

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel

from code_agent.config import Settings


class ScriptedModel(FakeMessagesListChatModel):
    """按预设顺序返回响应，不做任何网络调用。"""

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN003
        return self


@pytest.fixture
def scripted():
    """用法：scripted([AIMessage(...), AIMessage(...)])"""

    def make(responses):
        return ScriptedModel(responses=responses)

    return make


@pytest.fixture
def fake_settings() -> Settings:
    """不依赖 .env 的配置；仅用于构建对象，不会发起请求。"""
    return Settings(
        base_url="http://example.invalid",
        auth_token="fake-token",
        model="fake-model",
        pg_dsn=None,
        tavily_api_key=None,
    )
