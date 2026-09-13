# xx-code

基于 **LangGraph** 的多 Agent 编码助手（CLI）。

Supervisor 调度三个 Agent —— **Explorer**（定位代码）/ **Coder**（改代码）/ **Verifier**（跑测试核验）—— 对代码仓库完成「定位 → 修改 → 验证」的闭环；高风险命令会**暂停等你确认**。

> 从零构建的学习型项目，重点是**能看清每一步数据如何在 Agent 之间流动**。

## 状态

可多轮对话（短期记忆）、回答流式输出、会话持久化在 PostgreSQL、支持联网检索。
**108 passed, 1 skipped** —— 含 supervisor 路由 / 图接线 / HITL / 并发编辑 / 剪枝的回归测试。

编排已升级到**方案 B**：共享黑板里**只保留人话**（任务 / 指令 / 各成员汇报），
worker 内部的工具调用与结果会在 supervisor 下一轮开始时被剪掉（`supervisor.prune_scratchpad`）。
实测同任务的持久化状态从 44 条消息（含 24 条工具消息）降到 **8 条消息、0 条工具消息**。
剩余待办见 [DEVLOG.md](DEVLOG.md) 末尾的「v1 收尾状态」。

## 环境

- Python **3.13**（conda env `langgraph`）
- **PostgreSQL**（会话持久化 = "短期记忆"的本体）
- **DeepSeek API**（Anthropic 兼容端点）

## 开始

**首次准备（只做一次）**

```powershell
conda activate langgraph
pip install -r requirements.txt
pip install -e .                  # 装上 xx-code 命令（依赖为空，不会动已有环境）
cp .env.example .env              # 填 AGENT_AUTH_TOKEN、AGENT_PG_DSN
```

**日常使用（像 Claude Code）**

```powershell
cd <你的项目>
xx-code
```

不带参数 = 直接进对话；**当前目录就是工作目录**（启动时打印确认）。再敲一次 `xx-code`
会自动**续接本目录上次的会话**。

```
>>> 运行测试，把失败的修好
[supervisor] 下一步 → verifier（需要先跑测试定位失败）
verifier  → run_command({"command": "python -m pytest -q"})
[supervisor] 下一步 → coder（…）
coder     → edit_file({"path": "calc.py", …})
已把 calc.py 的加法改对，测试 2 passed。      ← 回答逐字流式打出
本轮：模型调用 5 次 · 输入 10,694 · 输出 1,459 · 合计 12,153 tokens
>>> 你刚才改了哪个文件？                      ← 凭记忆回答，不用重述背景
>>> :q
```

> 那行 token 统计里：**输出含 thinking 的 token**，所以数字偏大属正常；
> **输入是各次调用累加（计费口径）**，不是上下文大小。

## 其他命令

```powershell
xx-code "运行测试，把失败的修好"             # 一次性跑完即退出
xx-code --repo <别的目录>                   # 指定别的仓库（默认当前目录）
xx-code --new                              # 强制开新会话
xx-code --thread-id <id>                   # 精确指定要续接的会话
xx-code --agent explorer "这项目怎么跑测试"  # 只跑单个 worker
xx-code --check                            # 环境自检（模型连通 + tool calling）
xx-code --history                          # 查看历史：列出会话
xx-code --history --thread-id <id>         # 查看某次会话的完整记录（含被剪掉的工具调用与结果）
```

记录存在本机 PostgreSQL（`langgraph_db`，连接串在 `.env`）。**别想着直接查表** ——
消息是 msgpack 二进制，SQL 出来是乱码；用 `--history` 看。

找不到 `xx-code` 就说明没激活环境（先 `conda activate langgraph`），
或用等价的 `python -m code_agent.cli`。

## ⚠️ 安全

**只在 git 干净、你不在乎搞坏的仓库上用。**

- agent 能**改文件**、能**执行命令（含删除文件）**
- 危险命令的拦截是**正则匹配，不是沙箱** —— 绕过方式存在
- shell 命令**不受仓库目录限制**，能在你有权限的任何位置操作

练习可以拿自带的靶子：`cd data/sample_repo && xx-code`（故意有 2 个失败用例，`git checkout -- data/sample_repo` 可反复还原）。

## 结构

```
code_agent/
├── cli.py         # 入口：交互模式 / 流式输出 / HITL 确认
├── graph.py       # 组装 StateGraph + PostgreSQL checkpointer
├── supervisor.py  # 中心调度（JSON 路由 + 指令注入 + 生成回答）
├── workers.py     # Explorer / Coder / Verifier 三个子图（最小权限）
├── state.py       # 父图共享 State
├── paths.py       # 仓库路径围栏
├── messages.py    # 处理 thinking 模型的 block 形态
├── config.py      # 配置 + 模型构建
└── tools/         # filesystem / search / command / web
data/sample_repo/  # 练手靶子
tests/             # 105 passed
```

## 设计要点

- **最小权限**：只有 Verifier 能执行命令、只有 Coder 能改代码 —— 所以 HITL 只需挂一处。
- **调度**：Supervisor 用 `Command(goto=...)` 直接路由，不写 `conditional_edges`；worker 干完回到它。
- **隔离（方案 B）**：黑板只留人话，工具草稿成对剪掉 —— 止住上下文膨胀，也避免成员互相模仿。
- **上下文压缩**：工具内源头截断 → 剪掉旧草稿 → supervisor 只看摘要（带长度上限）。
- **工具失败**：工具内部捕获并回传错误给模型，但必须放行 `GraphBubbleUp`（否则 `interrupt` 失效）。
- **并发**：同一轮多个工具调用会**并行执行**，因此文件读-改-写必须串行化。
- **持久化**：`PostgresSaver` + `thread_id`；会话跨进程续跑，这就是"短期记忆"。

踩坑记录（思考模型的种种约束、Windows 编码、丢失更新等）见 [DEVLOG.md](DEVLOG.md)。
