"""把会话写成人可读的 Markdown 记录。

**为什么要单独一份**：状态文件（`.jsonl`）存的是 LangGraph 的 msgpack 快照，
base64 之后看着就是乱码 —— 那是给机器用的，不能动。
人要读对话，得另写一份纯文本，所以有了这个。

文件与状态文件**同名**、扩展名换成 `.md`，放在同一个日期目录下：

    .code_agent_sessions/2026/09/13/161728-还记得我之前问了你啥吗-e23b40.jsonl   ← 状态
    .code_agent_sessions/2026/09/13/161728-还记得我之前问了你啥吗-e23b40.md      ← 记录（人看）
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

_LOCK = threading.Lock()


def _short(args) -> str:
    """把工具参数压成一行短的。"""
    if not isinstance(args, dict):
        return str(args)[:80]
    text = json.dumps(args, ensure_ascii=False)
    return text if len(text) <= 120 else text[:117] + "…"


class Transcript:
    """往 Markdown 记录文件里追加条目。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, text: str) -> None:
        with _LOCK:
            with self.path.open("a", encoding="utf-8", errors="replace") as handle:
                handle.write(text)

    def header(self, repo: str, thread_id: str) -> None:
        if self.path.exists():
            return
        self._write(
            f"# 会话 {datetime.now():%Y-%m-%d %H:%M:%S}\n\n"
            f"- 仓库：`{repo}`\n- 会话 id：`{thread_id}`\n\n---\n"
        )

    def user(self, text: str) -> None:
        self._write(f"\n## {datetime.now():%H:%M:%S}　你\n\n{text}\n")

    def decision(self, next_: str, reason: str) -> None:
        self._write(f"\n## {datetime.now():%H:%M:%S}　supervisor\n\n→ **{next_}**（{reason}）\n")

    def tools(self, node: str, calls: list) -> None:
        lines = "\n".join(f"- `{c.get('name')}({_short(c.get('args'))})`" for c in calls)
        self._write(f"\n## {datetime.now():%H:%M:%S}　{node}\n\n{lines}\n")

    def report(self, node: str, text: str) -> None:
        self._write(f"\n## {datetime.now():%H:%M:%S}　{node}　汇报\n\n{text}\n")

    def answer(self, text: str) -> None:
        self._write(f"\n## {datetime.now():%H:%M:%S}　回答\n\n{text}\n\n---\n")
