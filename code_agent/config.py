"""配置加载与模型构建。

所有敏感信息（API key、数据库密码）只从环境变量 / 项目根目录的 .env 读取，
.env 已被 .gitignore 排除，代码里不出现任何真实密钥。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录（本文件位于 <root>/code_agent/config.py）
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    """配置缺失或不合法。"""


def load_env() -> None:
    """加载项目根目录的 .env。

    override=False：已存在的真实环境变量优先，不会被 .env 覆盖。
    """
    load_dotenv(PROJECT_ROOT / ".env", override=False)


@dataclass(frozen=True)
class Settings:
    """运行期配置。"""

    base_url: str
    auth_token: str
    model: str
    pg_dsn: str | None
    tavily_api_key: str | None

    @classmethod
    def from_env(cls) -> "Settings":
        load_env()
        required = ("AGENT_BASE_URL", "AGENT_AUTH_TOKEN", "AGENT_MODEL")
        missing = [key for key in required if not os.getenv(key)]
        if missing:
            raise ConfigError(
                "缺少环境变量: "
                + ", ".join(missing)
                + f"\n请复制 {PROJECT_ROOT / '.env.example'} 为 .env 并填写。"
            )
        return cls(
            base_url=os.environ["AGENT_BASE_URL"],
            auth_token=os.environ["AGENT_AUTH_TOKEN"],
            model=os.environ["AGENT_MODEL"],
            pg_dsn=os.getenv("AGENT_PG_DSN") or None,
            tavily_api_key=os.getenv("TAVILY_API_KEY") or None,
        )

    def describe(self) -> str:
        """用于日志输出，不泄露 token。"""
        return f"model={self.model} base_url={self.base_url} token=***"


def build_llm(settings: Settings | None = None, *, temperature: float = 0.0,
              max_tokens: int = 4096, timeout: float = 120.0, max_retries: int = 2):
    """构建主模型（DeepSeek 的 Anthropic 兼容端点）。

    懒导入 langchain_anthropic，避免每次导入包都付出加载代价。
    """
    from langchain_anthropic import ChatAnthropic

    settings = settings or Settings.from_env()
    return ChatAnthropic(
        model=settings.model,
        api_key=settings.auth_token,   # 别名，等价于 anthropic_api_key
        base_url=settings.base_url,    # 别名，等价于 anthropic_api_url
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        max_retries=max_retries,
    )
