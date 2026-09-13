"""JSONL 版 checkpointer：把图状态存成本地文件，不再依赖 PostgreSQL。

**为什么子类化 `InMemorySaver` 而不是从零实现 `BaseCheckpointSaver`：**

从零实现要写 `get_tuple` / `list` / `put` / `put_writes` 四个方法（参考：InMemorySaver 603 行、
PostgresSaver 476 行），其中版本号生成、blob 分层、pending writes、list 排序都不平凡。
更要命的是：`put_writes` 一旦写错，**HITL 的暂停/恢复会静默失效**（平时能跑，
一到危险命令要确认就出问题）。所以这里只在经过验证的实现之上加一层"落盘/重放"。

**文件布局**（一眼能看出是哪个会话）：

    <项目根>/.code_agent_sessions/
    ├── index.json                             仓库 → 最近一次会话
    └── 2026/09/13/143022-修复calc加法-a1b2c3.jsonl

文件名 = `时间-任务摘要-短id`；真正的 `thread_id` 存在**第一行的 meta 字段**里，
所以文件名不必背那串 hex。文件夹按**会话创建日期**分 —— 一个会话跨天续聊时会留在
创建那天的目录里，不会分裂（checkpointer 必须一个会话一个文件，否则记忆就断了）。

**文件内容**：append-only，一行一条操作。所有值都是 LangGraph 的
`JsonPlusSerializer` 序列化过的 `(type, bytes)`，bytes 用 base64 转成文本。
"""

from __future__ import annotations

import base64
import json
import re
import threading
from datetime import datetime
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver

from code_agent.config import SESSION_DIR

# LangGraph 的并行任务会**多线程**调用 put_writes（已实测工具确实并行执行），
# 因此追加必须加锁 —— 否则两次写入会交错，行被切成半截（实测踩到，文件里出现
# "行首是 base64 碎片、行尾才是完整 JSON" 的坏行）。
_APPEND_LOCK = threading.Lock()

_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def slugify(text: str, limit: int = 24) -> str:
    """把一句任务压成能当文件名的短标签。"""
    cleaned = _ILLEGAL.sub(" ", text or "").strip()
    cleaned = re.sub(r"\s+", "-", cleaned)
    return cleaned[:limit].strip("-") or "会话"


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
        self._paths: dict[str, Path] = {}   # thread_id → 文件
        self._labels: dict[str, str] = {}   # thread_id → 文件名里的摘要（供 set_label 用）
        self._meta_written: set[str] = set()  # 已经写过 meta 的会话（避免重复注入）
        self._replay_all()

    # ---------- 会话定位 ----------

    def path_for(self, thread_id: str) -> Path | None:
        """某个会话对应的文件（`--history` 用）。"""
        return self._paths.get(thread_id)

    def has_session(self, thread_id: str) -> bool:
        """这个会话是否已经落过盘（用于决定要不要设摘要标签）。"""
        return thread_id in self._paths

    def sessions(self) -> list[tuple[str, Path]]:
        """所有已知会话：(thread_id, 文件)，按文件修改时间从新到旧。"""
        return sorted(
            self._paths.items(), key=lambda item: item[1].stat().st_mtime, reverse=True
        )

    def set_label(self, thread_id: str, label: str) -> None:
        """给会话设一个可读摘要 —— **必须在第一次落盘之前调用**（文件名要用它）。"""
        self._labels[thread_id] = label

    def transcript_path(self, thread_id: str) -> Path:
        """人可读记录文件的路径（与状态文件同名，扩展名 .md）。

        会在此刻**预定**该会话的文件路径，保证之后再落盘用的是同一个名字
        （否则先写记录、后建状态文件时，两次算出的时间戳可能不同，会分成两个文件）。
        """
        with _APPEND_LOCK:
            path = self._paths.get(thread_id)
            if path is None:
                path = self._new_path(thread_id)
                self._paths[thread_id] = path
        return path.with_suffix(".md")

    def _new_path(self, thread_id: str) -> Path:
        now = datetime.now()
        folder = self.base_dir / f"{now:%Y}" / f"{now:%m}" / f"{now:%d}"
        folder.mkdir(parents=True, exist_ok=True)
        label = slugify(self._labels.get(thread_id, ""))
        short = thread_id[:6]
        path = folder / f"{now:%H%M%S}-{label}-{short}.jsonl"
        # 同一秒内开了两个会话时避免重名
        n = 1
        while path.exists():
            n += 1
            path = folder / f"{now:%H%M%S}-{label}-{short}-{n}.jsonl"
        return path

    # ---------- 落盘 ----------

    def _append(self, thread_id: str, record: dict) -> None:
        # 整段持锁："决定路径 → 序列化 → 写入" 必须原子。
        # 否则并发首次写入时会各自创建出不同文件名（_new_path 的防重名后缀），
        # 把一个会话的日志分散到多个文件里（实测被并发测试抓到）。
        with _APPEND_LOCK:
            path = self._paths.get(thread_id)
            if path is None:
                path = self._new_path(thread_id)
                self._paths[thread_id] = path
            if thread_id not in self._meta_written:
                # 第一行带上元信息：真正的 thread_id、摘要、创建时间
                self._meta_written.add(thread_id)
                record = {
                    **record,
                    "meta": {
                        "t": thread_id,
                        "label": self._labels.get(thread_id, ""),
                        "created": datetime.now().isoformat(timespec="seconds"),
                    },
                }
            line = json.dumps(record, ensure_ascii=False) + "\n"
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
            "cp": [cp_t, _b64(cp_b)], "meta_cp": [meta_t, _b64(meta_b)], "blobs": blobs,
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
        for path in sorted(self.base_dir.rglob("*.jsonl")):
            self._replay(path)

    def _replay(self, path: Path) -> None:
        lines = [
            line.strip()
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip()
        ]
        if not lines:
            return

        broken = 0
        thread_id: str | None = None
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                # 进程被杀时最后一行可能只写了一半（或历史版本留下的坏行）——
                # 跳过它，不要因为一行坏掉整个会话
                broken += 1
                continue
            if thread_id is None:
                thread_id = (record.get("meta") or {}).get("t")
            if thread_id:
                self._apply(thread_id, record)

        if thread_id:
            self._paths[thread_id] = path
            self._meta_written.add(thread_id)
        else:
            print(f"[JsonlSaver] {path.name} 缺少 meta.thread_id，已跳过整个文件")
        if broken:
            print(f"[JsonlSaver] {path.name}：跳过 {broken} 行损坏记录（会话仍可正常读取）")

    def _apply(self, thread_id: str, record: dict) -> None:
        ns = record.get("ns", "")
        if record.get("op") == "put":
            cp_t, cp_b = record["cp"]
            meta_t, meta_b = record["meta_cp"]
            self.storage[thread_id][ns][record["id"]] = (
                (cp_t, _unb64(cp_b)), (meta_t, _unb64(meta_b)), record.get("parent"),
            )
            for channel, version, blob_t, blob_b in record.get("blobs", []):
                self.blobs[(thread_id, ns, channel, version)] = (blob_t, _unb64(blob_b))
        elif record.get("op") == "writes":
            outer = (thread_id, ns, record["id"])
            for inner_task_id, idx, channel, blob_t, blob_b, task_path in record.get("items", []):
                self.writes[outer][(inner_task_id, idx)] = (
                    inner_task_id, channel, (blob_t, _unb64(blob_b)), task_path,
                )


def open_jsonl_checkpointer(base_dir: str | Path | None = None) -> JsonlSaver:
    """打开 JSONL checkpointer（可直接 `with` 使用，InMemorySaver 已实现上下文协议）。"""
    return JsonlSaver(base_dir)
