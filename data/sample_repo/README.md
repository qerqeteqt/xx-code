# sample_repo —— 给 agent 练手的靶子

一个**故意写错**的小 Python 包，用来安全地试跑 agent，不必拿重要项目冒险。

初始状态：`pytest` 有 **2 个用例失败**
- `add(2, 3)` 返回 `-1`（实现里错写成减法）
- `div(1, 0)` 抛 `ZeroDivisionError`（没处理除零）

试跑：

```bash
cd D:\pycharm\Mutil-Agent
"C:\Users\x_x\.conda\envs\langgraph\python.exe" -m code_agent.cli --repo data/sample_repo "运行测试，把失败的修好"
```

跑完看 agent 改了什么：

```bash
git diff data/sample_repo
```

想恢复原状（前提是还没把 agent 的修改提交）：

```bash
git checkout -- data/sample_repo
```

> 注：本目录不在项目自身的 `pytest` 收集范围内（`pytest.ini` 里 `testpaths = tests`），
> 所以它的失败用例不会影响项目自己的测试。
