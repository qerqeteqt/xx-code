"""回归：同一条 AIMessage 里的多个工具调用会被**并行执行**（已实测确认）。

实测踩到过：coder 在一次回复里同时发起两个 `edit_file`（改 add、改 div），
两个都返回"已修改"，但其中一个的改动被另一个覆盖掉了 ——
`edit_file` / `write_file` 是"读-改-写"，并行就会互相覆盖（丢失更新）。
后果是模型得重做一遍、白花若干次调用；更糟的情况是文件只被改了一半。

这里通过放大"读与写之间"的窗口，把这个竞态变成**确定性可复现**的测试。
"""

from __future__ import annotations

import time
from pathlib import Path

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage

from code_agent.paths import RepoRoot
from code_agent.tools.filesystem import build_filesystem_tools


def _edit(path: str, old: str, new: str, call_id: str) -> dict:
    return {
        "name": "edit_file",
        "args": {"path": path, "old_string": old, "new_string": new},
        "id": call_id,
    }


def test_parallel_edits_do_not_lose_changes(scripted, tmp_path, monkeypatch):
    # 放大竞态窗口：让"读"之后、真正落盘之前有一段延迟，
    # 这样并行的第二个编辑一定会读到旧内容。
    original_write = Path.write_text

    def slow_write(self, *args, **kwargs):
        time.sleep(0.05)
        return original_write(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", slow_write)

    (tmp_path / "calc.py").write_text("A = 1\nB = 2\n", encoding="utf-8")
    tools = build_filesystem_tools(RepoRoot(tmp_path), allow_write=True)

    agent = create_agent(
        scripted([
            AIMessage(content="", tool_calls=[
                _edit("calc.py", "A = 1", "A = 100", "e1"),
                _edit("calc.py", "B = 2", "B = 200", "e2"),
            ]),
            AIMessage(content="完成"),
        ]),
        tools=tools,
        system_prompt="测试用",
    )
    agent.invoke({"messages": [HumanMessage("改这两处")]})

    text = (tmp_path / "calc.py").read_text(encoding="utf-8")
    assert "A = 100" in text, "第一处修改被并行执行覆盖丢失了"
    assert "B = 200" in text, "第二处修改被并行执行覆盖丢失了"
