"""跨会话长期记忆：embedding + Milvus 语义检索。

**作用域**：按项目分 —— 每个目标仓库一份独立 collection（"这个项目用 conda env
langgraph" 只对那个项目有意义）。

**为什么直连 `pymilvus` 而不用 `langchain-milvus`**：实测不兼容。它把
`MilvusClient._using` 当成 alias 去调 ORM 的 `Collection(using=...)`，而
`MilvusClient` **不会注册 named connection** → 必然抛
`ConnectionNotExistException: should create connection first`。
（服务端 Milvus 3.0.0 + pymilvus 2.6.12 + langchain-milvus 0.3.3）

**两层存储抽象**：`MilvusStore`（真后端）与 `InMemoryStore`（纯 Python 余弦，
离线测试用），接口一致 —— 靠 `build_store()` 选。
"""

from __future__ import annotations

import abc
import hashlib
import math
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm(text: str) -> str:
    """归一化：压掉多余空白。去重前先归一，避免"只差空格"被当成两条。"""
    return " ".join((text or "").split())


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


@dataclass(frozen=True)
class Memory:
    """一条长期记忆。"""

    id: str
    text: str
    kind: str = "fact"      # preference / fact / convention …
    created: str = ""
    source: str = ""        # 来源会话 id，便于追溯
    score: float = 0.0      # 检索时的相似度（写入时为 0）


class Embedder:
    """文本向量化：DashScope 的 text-embedding-v4（走 OpenAI 兼容端点）。"""

    DIM = 1024

    def __init__(self, api_key: str, model: str = "text-embedding-v4",
                 base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1") -> None:
        from langchain_openai import OpenAIEmbeddings

        self.model = model
        # check_embedding_ctx_length=False：否则 langchain 会用 tiktoken 先把文本切成
        # token 数组再发出去，而非 OpenAI 原生端点会不认
        self._client = OpenAIEmbeddings(
            model=model, api_key=api_key, base_url=base_url,
            check_embedding_ctx_length=False,
        )

    def embed_documents(self, texts: Iterable[str]) -> list[list[float]]:
        return self._client.embed_documents(list(texts))

    def embed_query(self, text: str) -> list[float]:
        return self._client.embed_query(text)


class MemoryStore(abc.ABC):
    """长期记忆存储的公共接口 + **写入去重**逻辑。

    去重只设两道闸（成本递增）：

    1. **相似度阈值**（主力）：把新条目向量化，取最近的一条；相似度 ≥ `threshold`
       就判为同一条 → **update**（保留 id、覆盖内容与向量），而不是新增。
       *完全相同的文本相似度就是 1.0，所以不需要单独的"内容 hash"闸 —— 它被这道闸覆盖了。*
    2. **LLM 裁决**（在 `extract` 那一步做，不在这一层）：语义相近但可能是"冲突/过时"
       （"用 pytest" → "改用 unittest"）—— 这类只有语义理解能判，
       相似度算法只能说"很像"，说不了"哪个对"。
    """

    def __init__(self, embedder: Embedder, *, threshold: float = 0.92) -> None:
        self.embedder = embedder
        self.threshold = threshold

    # ---------- 对外接口 ----------

    def add(self, text: str, *, kind: str = "fact", source: str = "") -> str:
        """写入一条记忆，自动去重。返回值：命中的记忆 id（新增或更新的都是它）。"""
        clean = _norm(text)
        if not clean:
            raise ValueError("记忆内容不能为空")

        vector = self.embedder.embed_query(clean)
        nearest = self._nearest(vector)
        if nearest is not None and nearest.score >= self.threshold:
            # 判为同一条 → 更新（保留 id，历史可追溯是同一条记忆被改写）
            self._write(nearest.id, clean, kind or nearest.kind, source, vector, _now())
            return nearest.id
        return self._write(uuid.uuid4().hex[:16], clean, kind, source, vector, _now())

    @abc.abstractmethod
    def search(self, query: str, *, k: int = 3, min_score: float = 0.0) -> list[Memory]:
        """语义检索 top-k（按相似度降序，已过滤掉低于 min_score 的）。"""

    @abc.abstractmethod
    def all(self) -> list[Memory]:
        """列出全部记忆（人工查看 / 抽取时提供上下文用）。"""

    def update(self, memory_id: str, text: str, *, kind: str = "") -> None:
        """按 id 覆盖一条记忆（内容变了要**重算向量**，否则检索还是按旧语义）。

        与 `add` 的区别：`add` 自己判断"是不是同一条"，这里已经知道是哪条了 ——
        用于抽取阶段 LLM 明确给出 `update(id)` 的情况。
        """
        clean = _norm(text)
        if not clean:
            raise ValueError("记忆内容不能为空")
        self._write(memory_id, clean, kind or "fact", "",
                    self.embedder.embed_query(clean), _now())

    @abc.abstractmethod
    def delete(self, memory_id: str) -> None: ...

    @abc.abstractmethod
    def count(self) -> int: ...

    # ---------- 子类实现 ----------

    @abc.abstractmethod
    def _nearest(self, vector: Sequence[float]) -> Memory | None:
        """找出与新向量最相近的一条（用于去重判断）。"""

    @abc.abstractmethod
    def _write(self, memory_id: str, text: str, kind: str, source: str,
               vector: Sequence[float], created: str) -> str:
        """写入或覆盖（同 id 覆盖 = 更新）。"""


class InMemoryStore(MemoryStore):
    """纯 Python 余弦相似度。用于离线测试；Milvus 不可用时的降级后端。

    ⚠️ 进程退出即丢失，**不能当正式后端**。
    """

    def __init__(self, embedder: Embedder, *, threshold: float = 0.92) -> None:
        super().__init__(embedder, threshold=threshold)
        self._rows: dict[str, dict] = {}

    def _nearest(self, vector: Sequence[float]) -> Memory | None:
        best: Memory | None = None
        for memory_id, row in self._rows.items():
            score = cosine(vector, row["vector"])
            if best is None or score > best.score:
                best = Memory(id=memory_id, text=row["text"], kind=row["kind"],
                              created=row["created"], source=row["source"], score=score)
        return best

    def _write(self, memory_id, text, kind, source, vector, created) -> str:
        self._rows[memory_id] = {"text": text, "kind": kind, "source": source,
                                "created": created, "vector": list(vector)}
        return memory_id

    def search(self, query: str, *, k: int = 3, min_score: float = 0.0) -> list[Memory]:
        vector = self.embedder.embed_query(query)
        scored = [
            Memory(id=memory_id, text=row["text"], kind=row["kind"],
                   created=row["created"], source=row["source"],
                   score=cosine(vector, row["vector"]))
            for memory_id, row in self._rows.items()
        ]
        hits = [m for m in scored if m.score >= min_score]
        hits.sort(key=lambda m: m.score, reverse=True)
        return hits[:k]

    def all(self) -> list[Memory]:
        return [Memory(id=i, text=r["text"], kind=r["kind"],
                       created=r["created"], source=r["source"])
                for i, r in sorted(self._rows.items(), key=lambda kv: kv[1]["created"])]

    def delete(self, memory_id: str) -> None:
        self._rows.pop(memory_id, None)

    def count(self) -> int:
        return len(self._rows)


class MilvusStore(MemoryStore):
    """Milvus 后端（直连 pymilvus）。

    建表用**显式 schema**（`auto_id=False` + string 主键）—— 这是必须的：
    只有自己管 id 才能 `upsert` 覆盖同一条（"更新记忆"），
    而快捷建表（`create_collection(dimension=...)`）的 id 由 Milvus 生成，做不到。
    """

    TEXT_MAX = 4096

    def __init__(self, uri: str, collection: str, embedder: Embedder, *,
                 threshold: float = 0.92, dim: int | None = None) -> None:
        super().__init__(embedder, threshold=threshold)
        from pymilvus import MilvusClient

        self.client = MilvusClient(uri=uri)
        self.collection = collection
        self.dim = dim or embedder.DIM
        self._ensure()

    def _ensure(self) -> None:
        if self.client.has_collection(self.collection):
            return
        from pymilvus import DataType

        schema = self.client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=self.dim)
        schema.add_field("text", DataType.VARCHAR, max_length=self.TEXT_MAX)
        schema.add_field("kind", DataType.VARCHAR, max_length=64)
        schema.add_field("created", DataType.VARCHAR, max_length=32)
        schema.add_field("source", DataType.VARCHAR, max_length=64)
        index = self.client.prepare_index_params()
        index.add_index(field_name="vector", index_type="AUTOINDEX", metric_type="COSINE")
        self.client.create_collection(
            collection_name=self.collection, schema=schema, index_params=index
        )

    @staticmethod
    def _to_memory(row: dict, score: float = 0.0) -> Memory:
        return Memory(id=row["id"], text=row["text"], kind=row.get("kind", ""),
                      created=row.get("created", ""), source=row.get("source", ""),
                      score=score)

    def _nearest(self, vector: Sequence[float]) -> Memory | None:
        hits = self.client.search(
            self.collection, data=[list(vector)], limit=1,
            # ⚠️ 必须显式请求 "id"：Milvus 的 search 结果 entity 里**不自动带主键**
            #（实测 KeyError: 'id'——离线单测用 InMemoryStore 是发现不了的）
            output_fields=["id", "text", "kind", "created", "source"],
        )
        if not hits or not hits[0]:
            return None
        top = hits[0][0]
        return self._to_memory(top["entity"], score=float(top["distance"]))

    def _write(self, memory_id, text, kind, source, vector, created) -> str:
        self.client.upsert(self.collection, [{
            "id": memory_id, "vector": list(vector), "text": text[: self.TEXT_MAX],
            "kind": (kind or "fact")[:64], "created": created, "source": source[:64],
        }])
        return memory_id

    def search(self, query: str, *, k: int = 3, min_score: float = 0.0) -> list[Memory]:
        vector = self.embedder.embed_query(query)
        hits = self.client.search(
            self.collection, data=[vector], limit=max(k, 1),
            output_fields=["id", "text", "kind", "created", "source"],
        )
        found = [self._to_memory(h["entity"], score=float(h["distance"]))
                 for h in (hits[0] if hits else [])]
        return [m for m in found if m.score >= min_score][:k]

    def all(self) -> list[Memory]:
        # Milvus 默认最终一致：写完立刻查可能看不到，所以显式要 Strong
        rows = self.client.query(
            self.collection, filter='id != ""',
            output_fields=["id", "text", "kind", "created", "source"],
            consistency_level="Strong",
        )
        return [self._to_memory(r) for r in sorted(rows, key=lambda r: r.get("created", ""))]

    def delete(self, memory_id: str) -> None:
        self.client.delete(self.collection, ids=[memory_id])

    def count(self) -> int:
        return len(self.all())


def collection_for_project(root: str) -> str:
    """按项目给 collection 起名 —— 记忆按项目隔离，互不串味。

    ⚠️ **必须先规范化路径再 hash**：同一个目录写成 `D:/a/b` 和 `D:\\a\\b` 必须得到
    同一个名字。否则"写入时"和"读取时"路径写法不同，就会各建一个库、互相看不见
    —— 实测踩到（写记忆用正斜杠、CLI 里用 `RepoRoot` 解析出的反斜杠）。
    `normcase` 在 Windows 上还会统一大小写。
    """
    canonical = os.path.normcase(os.path.abspath(str(root)))
    digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:10]
    return f"cax_mem_{digest}"


_EXTRACT_PROMPT = """你是一个「长期记忆提取器」。读完下面这段编码助手与用户的对话，
抽出**值得跨会话长期记住**的稳定信息。

值得记的（跨会话仍然成立）：
- 用户偏好：语言、回复风格、工具选择、禁忌
- 项目约定：怎么跑测试、用什么环境、目录/命名约定、依赖管理方式
- 长期约束：用户的目标、外部限制

**不要**记：本次任务的具体细节、一次性的报错、临时代码片段、助手自己的做法。

如果已有记忆里已经有同类内容，用 update 修正而不是新增；明显过时的用 delete。

只输出一个 JSON 数组，不要任何其他文字、不要 markdown 代码块：
[{"op": "add"|"update"|"delete", "id": "update/delete 时填已有记忆的 id", "text": "记忆内容", "kind": "preference"|"fact"|"convention"}]

没有值得记的就输出 []。"""


def extract_operations(llm, conversation: str, existing: list[Memory]) -> list[dict]:
    """会话结束时用**独立的一次 LLM 调用**抽取记忆操作。

    解析失败就返回 `[]` —— 少记一条记忆而已，**不能因此让会话收尾失败**（所以这里吞异常）。
    """
    import json
    import re

    known = "\n".join(f"- [{m.id}] ({m.kind}) {m.text}" for m in existing) or "（暂无）"
    prompt = (
        f"{_EXTRACT_PROMPT}\n\n=== 已有记忆 ===\n{known}\n\n=== 对话 ===\n{conversation}"
    )
    try:
        raw = llm.invoke(prompt)
        text = getattr(raw, "content", raw)
        if isinstance(text, list):  # thinking 模型：content 是 block 列表
            text = "".join(b.get("text", "") for b in text
                           if isinstance(b, dict) and b.get("type") == "text")
        match = re.search(r"\[.*\]", str(text), re.DOTALL)
        if not match:
            return []
        parsed = json.loads(match.group(0))
        return [op for op in parsed if isinstance(op, dict) and op.get("op") in
                ("add", "update", "delete")] if isinstance(parsed, list) else []
    except Exception as exc:  # noqa: BLE001 - 抽记忆失败不该影响会话
        print(f"[memory] 抽取记忆失败：{type(exc).__name__}: {str(exc)[:80]}")
        return []


def apply_operations(store: MemoryStore, operations: list[dict], *, source: str = "") -> tuple[int, int, int]:
    """把抽取出的操作写进存储，返回 (新增, 更新, 删除) 的条数。

    `add` 仍走 `store.add()` —— 也就是说**阈值去重依然生效**，抽取阶段给出的 add
    如果其实是重复的，会被自动转成 update。
    """
    added = updated = deleted = 0
    for op in operations:
        try:
            kind, text, memory_id = op.get("op"), (op.get("text") or "").strip(), op.get("id")
            if kind == "add" and text:
                store.add(text, kind=op.get("kind") or "fact", source=source)
                added += 1
            elif kind == "update" and text and memory_id:
                store.update(memory_id, text, kind=op.get("kind") or "")
                updated += 1
            elif kind == "delete" and memory_id:
                store.delete(memory_id)
                deleted += 1
        except Exception as exc:  # noqa: BLE001 - 单条失败不该影响其他条
            print(f"[memory] 跳过一条记忆操作：{type(exc).__name__}: {str(exc)[:60]}")
    return added, updated, deleted


def build_store(settings, root: str) -> MemoryStore | None:
    """按配置构建记忆后端。缺 key / 连不上 Milvus 就返回 None（等于关闭记忆功能）。

    这里**刻意返回 None 而不是抛异常**：Milvus 没起不该让整个 agent 跑不起来 ——
    长期记忆是增强项，不是必需品。但要**打印一句**，免得静默失效。
    """
    if not settings.dashscope_api_key or not settings.milvus_uri:
        return None
    try:
        return MilvusStore(
            uri=settings.milvus_uri,
            collection=collection_for_project(root),
            embedder=Embedder(settings.dashscope_api_key, settings.embedding_model),
            threshold=settings.memory_threshold,
        )
    except Exception as exc:  # noqa: BLE001 - 记忆不可用不该拖垮 agent
        print(f"[memory] 长期记忆已禁用：连不上 Milvus（{type(exc).__name__}: {str(exc)[:80]}）")
        return None
