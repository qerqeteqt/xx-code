"""长期记忆：写入去重 / 检索排序 / 项目隔离（全部离线，用假 embedder）。

为什么用假 embedder：真 embedding 走 DashScope、真存储走 Milvus，都依赖外部服务。
这里注入**可控向量**的假 embedder + `InMemoryStore`，把"去重逻辑本身"测清楚 ——
真实链路的验证另外单独做。
"""

from __future__ import annotations

import hashlib

import pytest

from code_agent.memory import (
    InMemoryStore,
    MemoryStore,
    collection_for_project,
    cosine,
)


class FakeEmbedder:
    """按预置表返回向量；没预置的按文本 hash 生成（确定性，同样输入必得同样向量）。"""

    def __init__(self, vectors: dict[str, list[float]] | None = None, dim: int = 4) -> None:
        self.vectors = vectors or {}
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        if text in self.vectors:
            return self.vectors[text]
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [(byte - 128) / 256 for byte in digest[: self.dim]]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)

    def embed_documents(self, texts) -> list[list[float]]:
        return [self._vec(t) for t in texts]


def store_with(vectors: dict[str, list[float]] | None = None, *, threshold: float = 0.92):
    return InMemoryStore(FakeEmbedder(vectors), threshold=threshold)


# ---------- 写入去重 ----------


def test_add_creates_one_memory():
    store = store_with()
    store.add("项目用 pytest 跑测试")
    assert store.count() == 1


def test_identical_text_updates_instead_of_duplicating():
    """完全相同的文本不该产生第二条 —— 相似度是 1.0，必然命中阈值。"""
    store = store_with()
    first = store.add("项目用 pytest 跑测试")
    second = store.add("项目用 pytest 跑测试")

    assert first == second, "同一条记忆应该返回同一个 id"
    assert store.count() == 1, "不该重复写入"


def test_whitespace_is_normalized_before_dedup():
    """只差空格的文本归一化后是同一句，应判为重复。"""
    store = store_with()
    store.add("项目用   pytest 跑测试")
    store.add("项目用 pytest 跑测试")
    assert store.count() == 1


def test_similar_text_above_threshold_updates_same_memory():
    """语义相近（≥ 阈值）→ 更新同一条，**id 保持不变**（历史可追溯是同一条被改写）。"""
    vec_a = [1.0, 0.0, 0.0, 0.0]
    vec_b = [0.99, 0.141, 0.0, 0.0]          # 与 vec_a 的余弦 ≈ 0.99
    store = store_with({"偏好用 pytest": vec_a, "偏好用 pytest -q": vec_b})

    first = store.add("偏好用 pytest", kind="preference")
    second = store.add("偏好用 pytest -q", kind="preference")

    assert first == second, "命中阈值就该更新同一条，而不是新增"
    assert store.count() == 1
    assert store.all()[0].text == "偏好用 pytest -q", "内容应被更新为最新的"


def test_dissimilar_text_adds_a_new_memory():
    vec_a = [1.0, 0.0, 0.0, 0.0]
    vec_b = [0.0, 1.0, 0.0, 0.0]             # 与 vec_a 正交，余弦 0
    store = store_with({"偏好用 pytest": vec_a, "项目跑在 conda 里": vec_b})

    id_a = store.add("偏好用 pytest")
    id_b = store.add("项目跑在 conda 里")

    assert id_a != id_b
    assert store.count() == 2


def test_empty_text_is_rejected():
    store = store_with()
    with pytest.raises(ValueError):
        store.add("   ")
    assert store.count() == 0


# ---------- 检索 ----------


def test_search_returns_most_similar_first():
    vec_a = [1.0, 0.0, 0.0, 0.0]
    vec_b = [0.0, 1.0, 0.0, 0.0]
    vec_c = [0.0, 0.0, 1.0, 0.0]
    store = store_with({"A": vec_a, "B": vec_b, "C": vec_c})

    for text in ("A", "B", "C"):
        store.add(text)

    hits = store.search("A", k=3)
    assert [h.text for h in hits] == ["A", "B", "C"], "A 自己应该排第一（余弦 1.0）"
    assert hits[0].score == pytest.approx(1.0)


def test_search_respects_min_score():
    vec_a = [1.0, 0.0, 0.0, 0.0]
    vec_b = [0.0, 1.0, 0.0, 0.0]
    store = store_with({"A": vec_a, "B": vec_b})
    store.add("A")
    store.add("B")

    hits = store.search("A", k=5, min_score=0.5)
    assert [h.text for h in hits] == ["A"], "正交的那条该被阈值过滤掉"


def test_search_honours_k():
    # 注意：向量必须互不相似，否则会被去重合并成一条（这是上面那些测试的性质）
    vectors = {
        "t0": [1.0, 0.0, 0.0, 0.0],
        "t1": [0.0, 1.0, 0.0, 0.0],
        "t2": [0.0, 0.0, 1.0, 0.0],
        "t3": [0.0, 0.0, 0.0, 1.0],
        "t4": [0.7, 0.7, 0.0, 0.0],   # 与 t0 余弦 0.707 < 0.92，不会被合并
    }
    store = store_with(vectors)
    for text in vectors:
        store.add(text)

    assert store.count() == 5, "这些向量互不相似，应该是 5 条独立记忆"
    assert len(store.search("t0", k=2)) == 2


# ---------- 其他 ----------


def test_delete_and_count():
    store = store_with()
    memory_id = store.add("一条要被删掉的记忆")
    assert store.count() == 1
    store.delete(memory_id)
    assert store.count() == 0


def test_collection_name_is_stable_and_project_scoped():
    a1 = collection_for_project(r"D:\repo\a")
    a2 = collection_for_project(r"D:\repo\a")
    b = collection_for_project(r"D:\repo\b")
    assert a1 == a2, "同一个项目每次要得到同一个 collection"
    assert a1 != b, "不同项目必须隔离到不同 collection"
    assert a1.startswith("cax_mem_")


def test_collection_name_ignores_path_spelling():
    """回归：同一个目录的不同写法必须映射到同一个 collection。

    实测踩到：写记忆时用 "D:/a/b"（正斜杠）、CLI 里用 RepoRoot 解析出的 "D:\\a\\b"
    （反斜杠），hash 不同 → 各建一个库、互相看不见。
    """
    assert collection_for_project("D:/repo/a") == collection_for_project(r"D:\repo\a")


def test_cosine_handles_zero_vector():
    assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_store_is_abstract():
    """存储是抽象层 —— 换后端不该改调用方（MilvusStore / InMemoryStore 接口一致）。"""
    assert issubclass(InMemoryStore, MemoryStore)
    for method in ("add", "search", "all", "delete", "count"):
        assert hasattr(MemoryStore, method)
