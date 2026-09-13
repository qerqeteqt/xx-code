"""记忆抽取：LLM 输出的解析与落地（离线，用假 LLM + InMemoryStore）。"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from code_agent.memory import InMemoryStore, apply_operations, extract_operations
from test_memory import FakeEmbedder  # noqa: E402 - pytest 会把 tests/ 加进 sys.path


class FakeLLM:
    """按脚本返回；也支持直接抛异常，用来测"抽取失败不能影响收尾"。"""

    def __init__(self, reply: str = "", *, boom: bool = False) -> None:
        self.reply, self.boom = reply, boom

    def invoke(self, _prompt):
        if self.boom:
            raise RuntimeError("模型挂了")
        return AIMessage(content=self.reply)


def store() -> InMemoryStore:
    return InMemoryStore(FakeEmbedder())


def test_parses_json_array():
    ops = extract_operations(
        FakeLLM('[{"op": "add", "text": "用户偏好中文", "kind": "preference"}]'), "", []
    )
    assert ops == [{"op": "add", "text": "用户偏好中文", "kind": "preference"}]


def test_parses_json_wrapped_in_prose_and_fences():
    """模型常把 JSON 包在废话或 markdown 代码块里 —— 要能抠出来。"""
    reply = '好的，我提取到如下：\n```json\n[{"op": "add", "text": "项目用 pytest"}]\n```\n完毕'
    assert extract_operations(FakeLLM(reply), "", []) == [{"op": "add", "text": "项目用 pytest"}]


def test_returns_empty_when_model_fails():
    assert extract_operations(FakeLLM(boom=True), "", []) == []


def test_returns_empty_when_no_json():
    assert extract_operations(FakeLLM("我觉得没什么好记的"), "", []) == []


def test_filters_unknown_ops():
    ops = extract_operations(FakeLLM('[{"op": "explode", "text": "x"}]'), "", [])
    assert ops == []


def test_apply_add_uses_dedup():
    """抽取给的 add 若其实是重复，仍会被存储层的阈值去重转成 update。"""
    s = store()
    s.add("用户偏好中文回复", kind="preference")

    added, updated, deleted = apply_operations(
        s, [{"op": "add", "text": "用户偏好中文回复", "kind": "preference"}]
    )
    assert (added, updated, deleted) == (1, 0, 0)
    assert s.count() == 1, "不该产生第二条重复记忆"


def test_apply_update_rewrites_same_memory():
    s = store()
    memory_id = s.add("测试用 pytest", kind="convention")

    added, updated, deleted = apply_operations(
        s, [{"op": "update", "id": memory_id, "text": "测试改用 unittest", "kind": "convention"}]
    )
    assert (added, updated, deleted) == (0, 1, 0)
    assert s.count() == 1
    assert s.all()[0].text == "测试改用 unittest"


def test_apply_delete_removes_memory():
    s = store()
    memory_id = s.add("这条过时了")
    added, updated, deleted = apply_operations(s, [{"op": "delete", "id": memory_id}])
    assert (added, updated, deleted) == (0, 0, 1)
    assert s.count() == 0


def test_bad_operation_is_skipped_without_breaking_others():
    s = store()
    added, updated, deleted = apply_operations(s, [
        {"op": "update", "text": "缺 id 的更新"},          # 该被跳过
        {"op": "add", "text": "这条是好的"},
        {"op": "delete", "id": "不存在的 id"},             # 删不存在的也不该炸
    ])
    assert added == 1
    assert s.count() == 1
