"""HITL：危险命令暂停-确认-恢复（不联网）。

覆盖一条完整的真实链路：`create_agent` + 真实中间件栈 + 真实 `run_command` 工具，
用脚本化模型让它调用危险命令，然后断言：
- 暂停确实发生（且携带命令与原因）
- 用户拒绝 → 命令**没有执行**
- 用户批准 → 命令**确实执行**
"""

from __future__ import annotations

import subprocess

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from code_agent.paths import RepoRoot
from code_agent.tools.command import build_command_tools
from code_agent.workers import _middleware

DANGEROUS = "git clean -fd"


@pytest.fixture
def repo(tmp_path):
    """一个真实的小 git 仓库，含一个未跟踪文件。"""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "untracked.txt").write_text("junk\n", encoding="utf-8")
    return RepoRoot(tmp_path)


def _agent(scripted, root):
    return create_agent(
        scripted([
            AIMessage(content="", tool_calls=[
                {"name": "run_command", "args": {"command": DANGEROUS}, "id": "c1"}
            ]),
            AIMessage(content="已处理"),
        ]),
        tools=build_command_tools(root),
        system_prompt="测试用",
        middleware=_middleware(),
        checkpointer=InMemorySaver(),
    )


def _run_until_interrupt(agent, config):
    """跑到暂停为止，返回 interrupt 载荷。"""
    for chunk in agent.stream(
        {"messages": [HumanMessage("执行 git clean -fd")]}, config, stream_mode="updates"
    ):
        if "__interrupt__" in chunk:
            return chunk["__interrupt__"][0].value
    return None


def test_dangerous_command_pauses_with_reason(scripted, repo):
    pending = _run_until_interrupt(_agent(scripted, repo), {"configurable": {"thread_id": "t1"}})
    assert pending is not None, "危险命令没有触发暂停"
    assert pending["command"] == DANGEROUS
    assert pending["reason"]


def test_reject_keeps_files_intact(scripted, repo):
    agent = _agent(scripted, repo)
    config = {"configurable": {"thread_id": "t2"}}
    assert _run_until_interrupt(agent, config) is not None

    for _ in agent.stream(Command(resume="reject"), config, stream_mode="updates"):
        pass

    assert repo.resolve("untracked.txt").exists(), "用户拒绝了，命令却真的执行了"


def test_approve_actually_executes(scripted, repo):
    agent = _agent(scripted, repo)
    config = {"configurable": {"thread_id": "t3"}}
    assert _run_until_interrupt(agent, config) is not None

    for _ in agent.stream(Command(resume="approve"), config, stream_mode="updates"):
        pass

    assert not repo.resolve("untracked.txt").exists(), "用户批准了，命令却没执行"


def test_safe_command_never_pauses(scripted, repo):
    """安全命令不该弹确认。"""
    agent = create_agent(
        scripted([
            AIMessage(content="", tool_calls=[
                {"name": "run_command", "args": {"command": "git status"}, "id": "c1"}
            ]),
            AIMessage(content="完成"),
        ]),
        tools=build_command_tools(repo),
        system_prompt="测试用",
        middleware=_middleware(),
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "t4"}}
    interrupted = False
    for chunk in agent.stream(
        {"messages": [HumanMessage("看看状态")]}, config, stream_mode="updates"
    ):
        if "__interrupt__" in chunk:
            interrupted = True
    assert not interrupted
