"""三个 worker agent：Explorer / Coder / Verifier。

每个 worker 由 `create_agent` 构建成一张**编译后的子图**，拥有独立的
system_prompt 与裁剪过的工具集（最小权限）。子图**不装 checkpointer**，
持久化统一由根图（M3）负责 —— 子图自己装会冲突。

工具权限（最小权限）：
    Explorer   只读（list / read / glob / grep）
    Coder      只读 + 写（write / edit）
    Verifier   只读 + run_command —— 唯一能执行命令的角色，因此 HITL 只需挂在这一处
"""

from __future__ import annotations

from langchain.agents import create_agent
from langchain.agents.middleware import (
    ClearToolUsesEdit,
    ContextEditingMiddleware,
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
)

from code_agent.config import Settings, build_llm
from code_agent.paths import RepoRoot
from code_agent.tools.command import build_command_tools
from code_agent.tools.filesystem import build_filesystem_tools
from code_agent.tools.search import build_search_tools

# 单个 agent 跑一轮任务的调用上限，防止跑飞
MAX_MODEL_CALLS = 12
MAX_TOOL_CALLS = 25

# 追加到每个 worker 提示词末尾的通用约束。
# 原因：ReAct 循环在模型"只回文字、不调工具"时就会结束。实测 coder 曾只描述计划
# 而没动手，导致 supervisor 白跑一轮重新分派。
_ACT_NOW = """

注意：不要只描述你的计划或打算 —— 必须实际调用工具来推进任务。
只有在任务确实完成时，才输出最终汇报。"""

_EXPLORER = """你是 Explorer，负责在目标仓库中定位与任务相关的代码。

目标仓库根目录：{root}
所有工具的文件路径都相对该根目录。

工作方式：
- 先 list_dir 看目录结构，再用 glob_search 找文件名、grep_search 按内容定位，
  最后用 read_file 只读关键片段。不要一次读入整个大文件。
- 结论要落到具体位置。

完成后用简洁的中文汇报：
1. 相关文件清单（每个文件一句话）
2. 关键函数/类及其位置（文件:行号）
3. 与任务直接相关的代码要点

你不修改任何文件。"""

_CODER = """你是 Coder，负责按任务要求修改目标仓库中的代码。

目标仓库根目录：{root}
所有工具的文件路径都相对该根目录。

工作方式：
- 动手前必须先 read_file 确认要改的原文，不要凭猜测拼 old_string。
- 改已有代码用 edit_file（精确替换，old_string 必须与原文完全一致且唯一）；
  只有新建文件或整体重写才用 write_file。
- 改动要小且聚焦，不要顺手重构无关代码。

完成后用简洁的中文汇报：改了哪些文件、每个文件改了什么、为什么。

你无法执行命令，验证由 Verifier 负责。"""

_VERIFIER = """你是 Verifier，负责独立核验代码是否满足任务要求。

目标仓库根目录：{root}
所有工具的文件路径都相对该根目录。

工作方式：
- 自己重新阅读相关代码做判断，不要轻信别人的结论。
- 用 run_command 运行测试或检查来取得客观证据（例如 python -m pytest -q）。
  命令在系统 shell 中执行（Windows 下是 cmd.exe），工作目录为仓库根目录。
  危险命令会暂停请求用户确认，被你拒绝后请改用非破坏性的做法。
- 重点检查：逻辑是否正确、边界情况、是否真正解决了任务描述中的问题。

结论用简洁的中文给出，**开头必须明确写「通过」或「不通过」**，然后说明理由与证据；
不通过时给出具体问题与修改建议。"""


def _middleware() -> list:
    """三个 agent 共用的健壮性中间件。

    - ContextEditingMiddleware：旧的工具结果替换为占位符，只影响单次调用、不写 checkpoint
    - ToolCallLimit / ModelCallLimit：限制单轮任务的调用次数

    **刻意不用 `ToolRetryMiddleware`**：它的 `wrap_tool_call` 是 `except Exception`，
    且没有放行 LangGraph 控制流异常的机制，会把 `interrupt()` 抛出的 `GraphInterrupt`
    当成可重试的失败捕获、重试耗尽后转成错误 ToolMessage —— 结果是 HITL 暂停永远
    传不到 CLI（实测确认）。我们所有工具都经 `safe` 兜底、从不抛异常，本就不需要它。
    """
    return [
        ContextEditingMiddleware(edits=[ClearToolUsesEdit(trigger=80_000, keep=3)]),
        ToolCallLimitMiddleware(run_limit=MAX_TOOL_CALLS),
        ModelCallLimitMiddleware(run_limit=MAX_MODEL_CALLS, exit_behavior="end"),
    ]


def _build(root: RepoRoot, name: str, prompt: str, *, allow_write: bool,
           allow_command: bool, settings: Settings | None) -> object:
    tools = build_filesystem_tools(root, allow_write=allow_write) + build_search_tools(root)
    if allow_command:
        tools += build_command_tools(root)
    return create_agent(
        build_llm(settings),
        tools=tools,
        system_prompt=prompt.format(root=root) + _ACT_NOW,
        middleware=_middleware(),
        name=name,
    )


def build_explorer(root: RepoRoot, settings: Settings | None = None):
    return _build(root, "explorer", _EXPLORER,
                  allow_write=False, allow_command=False, settings=settings)


def build_coder(root: RepoRoot, settings: Settings | None = None):
    return _build(root, "coder", _CODER,
                  allow_write=True, allow_command=False, settings=settings)


def build_verifier(root: RepoRoot, settings: Settings | None = None):
    return _build(root, "verifier", _VERIFIER,
                  allow_write=False, allow_command=True, settings=settings)


WORKERS = {
    "explorer": build_explorer,
    "coder": build_coder,
    "verifier": build_verifier,
}


def build_worker(role: str, root: RepoRoot, settings: Settings | None = None):
    """按角色名构建 worker，返回编译后的子图。"""
    if role not in WORKERS:
        raise ValueError(f"未知角色: {role}（可选: {', '.join(WORKERS)}）")
    return WORKERS[role](root, settings)
