"""JSONL 版 checkpointer：把图状态存成本地文件，不再依赖 PostgreSQL。

**为什么子类化 `InMemorySaver` 而不是从零实现 `BaseCheckpointSaver`：**

从零实现要写 `get_tuple` / `list` / `put` / `put_writes` 四个方法（参考：InMemorySaver 603 行、
PostgresSaver 476 行），其中版本号生成、blob 分层、pending writes、list 排序都不平凡。
更要命的是：`put_writes` 一旦写错，**HITL 的暂停/恢复会静默失效**（平时能跑，
一到危险命令要确认就出问题）。所以这里只在经过验证的实现之上加一层"落盘/重放"。

**文件格式**：一个会话一个 `<thread_id>.jsonl`，append-only，一行一条操作：

    {"op":"put",    "ns":…, "id":…, "parent":…, "cp":[…], "meta":[…], "blobs":[[…]]}
    {"op":"writes", "ns":…, "id":…, "task_id":…, "items":[[…]]}

所有值都是 LangGraph 自己的 `JsonPlusSerializer` 序列化过的 `(type, bytes)`，
bytes 用 base64 转成文本。线程 id 取自文件名，所以行内不必重复存。
"""

from __future__ import annotations

import base64
import json
import threading
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver

from code_agent.config import SESSION_DIR

# LangGraph 的并行任务会**多线程**调用 put_writes（已实测工具确实并行执行），
# 因此追加必须加锁 —— 否则两次写入会交错，行被切成半截（实测踩到，文件里出现
# "行首是 base64 碎片、行尾才是完整 JSON" 的坏行）。
_APPEND_LOCK = threading.Lock()


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


class JsonlSaver(InMemorySaver):
    """把 `InMemorySaver` 的内存结构落成本地 JSONL 文件。"""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        super().__init__()
        self.base_dir = Path(base_dir) if base_dir else SESSION_DIR
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._replay_all()

    # ---------- 落盘 ----------

    def _append(self, thread_id: str, record: dict) -> None:
        # 整行（含换行）一次性写入，且整个过程持锁 —— 保证行与行不交错
        line = json.dumps(record, ensure_ascii=False) + "\n"
        path = self.base_dir / f"{thread_id}.jsonl"
        with _APPEND_LOCK:
            with path.open("a", encoding="utf-8", errors="replace") as handle:
                handle.write(line)

    def put(self, config, checkpoint, metadata, new_versions):
        result = super().put(config, checkpoint, metadata, new_versions)
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"]["checkpoint_ns"]
        checkpoint_id = checkpoint["id"]

        (cp_t, cp_b), (meta_t, meta_b), parent = self.storage[thread_id][ns][checkpoint_id]
        blobs = []
        for channel, version in new_versions.items():
            blob = self.blobs.get((thread_id, ns, channel, version), ("empty", b""))
            blobs.append([channel, version, blob[0], _b64(blob[1])])

        self._append(thread_id, {
            "op": "put", "ns": ns, "id": checkpoint_id, "parent": parent,
            "cp": [cp_t, _b64(cp_b)], "meta": [meta_t, _b64(meta_b)], "blobs": blobs,
        })
        return result

    def put_writes(self, config, writes, task_id, task_path=""):
        super().put_writes(config, writes, task_id, task_path)
        thread_id = config["configurable"]["thread_id"]
        ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"]["checkpoint_id"]

        # 直接把这个检查点下的全部 writes 重记一遍（数量很少，重放时后者覆盖前者）
        stored = self.writes.get((thread_id, ns, checkpoint_id)) or {}
        items = [
            [inner_task_id, idx, channel, blob[0], _b64(blob[1]), path]
            for (inner_task_id, idx), (_task, channel, blob, path) in stored.items()
        ]
        self._append(thread_id, {
            "op": "writes", "ns": ns, "id": checkpoint_id, "task_id": task_id,
            "task_path": task_path, "items": items,
        })

    # ---------- 加载 ----------

    def _replay_all(self) -> None:
        for path in sorted(self.base_dir.glob("*.jsonl")):
            self._replay(path)

    def _replay(self, path: Path) -> None:
        thread_id = path.stem
        broken = 0
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                self._apply(thread_id, json.loads(line))
            except json.JSONDecodeError:
                # 进程被杀时最后一行可能只写了一半（或历史版本留下的坏行）——
                # 跳过它，不要因为一行坏掉整个会话
                broken += 1
        if broken:
            print(f"[JsonlSaver] {path.name}：跳过 {broken} 行损坏记录（会话仍可正常读取）")

    def _apply(self, thread_id: str, record: dict) -> None:
        ns = record.get("ns", "")
        if record.get("op") == "put":
            cp_t, cp_b = record["cp"]
            meta_t, meta_b = record["meta"]
            self.storage[thread_id][ns][record["id"]] = (
                (cp_t, _unb64(cp_b)), (meta_t, _unb64(meta_b)), record.get("parent"),
            )
            for channel, version, blob_t, blob_b in record.get("blobs", []):
                self.blobs[(thread_id, ns, channel, version)] = (blob_t, _unb64(blob_b))
        elif record.get("op") == "writes":
            outer = (thread_id, ns, record["id"])
            for inner_task_id, idx, channel, blob_t, blob_b, path in record.get("items", []):
                self.writes[outer][(inner_task_id, idx)] = (
                    inner_task_id, channel, (blob_t, _unb64(blob_b)), path,
                )


def open_jsonl_checkpointer(base_dir: str | Path | None = None) -> JsonlSaver:
    """打开 JSONL checkpointer（可直接 `with` 使用，InMemorySaver 已实现上下文协议）。"""
    return JsonlSaver(base_dir)
