# xx-code

基于 **LangGraph** 的多 Agent 编码助手（CLI）。

用 **Supervisor 中心调度** 组织三个 Agent —— **Explorer**（定位代码）/ **Coder**（修改代码）/ **Verifier**（跑测试验证）—— 对指定代码仓库完成「定位 → 修改 → 验证」的闭环。高风险命令（`rm -rf`、`git push` 等）会**暂停并等你确认**。

> 这是一个从零构建的学习型项目，目标是能看清每一步的数据如何在 Agent 之间流动。

## 当前状态

可以对话（多轮 + 短期记忆）、回答流式输出、会话由 PostgreSQL 持久化、
支持联网检索。**105 个测试通过**（含 supervisor 路由 / 图接线 / HITL / 并发编辑的回归测试）。

> 已知未完成项与后续方向（编排升级到"方案 B"等）见 [DEVLOG.md](DEVLOG.md) 末尾的「v1 收尾状态」。

## 环境要求

- Python **3.13**（本项目在 conda env `langgraph` 中开发）
- **PostgreSQL**：用作 LangGraph checkpointer，提供跨进程的会话持久化
- **DeepSeek API**：走 Anthropic 兼容端点

## 快速开始

### 1. 首次准备（只做一次）

```powershell
conda activate langgraph          # 项目跑在这个环境里；别用系统默认 python（3.10，没装依赖）
pip install -r requirements.txt
pip install -e .                  # 装上 xx-code 命令（依赖为空，不会改动环境里已有的包）

cp .env.example .env              # 然后编辑 .env，填 AGENT_AUTH_TOKEN 与 AGENT_PG_DSN
```

### 2. 日常使用 —— 像 Claude Code 一样

```powershell
cd <你的项目>
xx-code
```

**就这两条。** 不带参数 = 直接进入对话；**当前目录就是工作目录**（启动时会打印出来给你确认）。
再敲一次 `xx-code` 会自动**续接本目录上次的会话**，接着聊。

```
仓库 D:\your-project
会话 80243508…  （已续接上次会话）
直接输入任务即可；:new 开新会话，:q 退出

>>> 运行测试，把失败的修好
[supervisor] 下一步 → verifier（…）
verifier  → run_command({"command": "python -m pytest -q"})
[supervisor] 下一步 → coder（…）
coder     → edit_file({"path": "calc.py", …})
…
已把 calc.py 的加法改对，测试 2 passed。      ← 回答逐字流式打出
>>> 你刚才改了哪个文件？                      ← 凭记忆回答，不用重述背景
>>> :q
```

### 其他用法

```powershell
xx-code "运行测试，把失败的修好"            # 一次性跑完即退出（任务作为参数）
xx-code --repo <别的目录>                   # 指定别的仓库（默认当前目录）
xx-code --new                              # 强制开新会话，不续接上次
xx-code --thread-id <id>                   # 精确指定要续接的会话
xx-code --agent explorer "这项目怎么跑测试"  # 只跑单个 worker（explorer/coder/verifier）
xx-code --check                            # 环境自检（模型连通 + tool calling）
```

若提示找不到 `xx-code`，说明当前没激活环境 —— 先 `conda activate langgraph`，
或改用 `python -m code_agent.cli`（两种写法完全等价）。

> ⚠️ **只在 git 干净、你不在乎搞坏**的仓库上跑修改类任务。
> agent 能改文件、能执行命令（**含删除文件**），危险命令的拦截是**正则匹配而非沙箱**，
> 而且 shell 命令**不受仓库目录限制**。别拿重要的、没版本控制的目录试。

## 目录结构

```
xx-code/
├── code_agent/          # 主包
│   ├── config.py        # 配置加载 + 模型构建
│   ├── cli.py           # 命令行入口：交互模式 / 流式输出 / HITL 交互
│   ├── paths.py         # 目标仓库路径围栏
│   ├── messages.py      # text_of() / normalize_content()：处理 thinking 模型的 block 形态
│   ├── state.py         # 父图共享 State
│   ├── tools/           # 工具集
│   │   ├── _util.py        # safe：工具异常兜底（放行 interrupt）
│   │   ├── filesystem.py   # read_file / write_file / edit_file / list_dir（写入串行化）
│   │   ├── search.py       # glob_search / grep_search
│   │   ├── command.py      # run_command + 危险命令 HITL
│   │   └── web.py          # web_search 联网检索（无 key 自动禁用）
│   ├── workers.py       # Explorer / Coder / Verifier 三个 worker 子图
│   ├── supervisor.py    # 中心调度（JSON 路由 + 指令注入 + 生成回答）
│   └── graph.py         # 组装 StateGraph + PostgreSQL checkpointer
├── data/sample_repo/    # 故意有 2 个失败用例的练手靶子（安全试跑用）
├── models/              # 本地模型（暂空）
├── tests/               # 测试（105 passed, 1 skipped）
├── pyproject.toml       # xx-code 命令入口
├── DEVLOG.md            # 开发日志：每步实际干了什么
└── requirements.txt
```

## 路线图

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M0 | 项目骨架 + 模型自检 | ✅ |
| M1 | 文件/搜索工具 + 单元测试（无需模型） | ✅ |
| M2 | Explorer / Coder / Verifier 三个 worker | ✅ |
| M3 | Supervisor 调度 + 危险命令 HITL 确认 | ✅ |
| M4 | 联网检索 + 输出美化 + 完整文档 | ✅ |
| 后续 | 交互模式 / 短期记忆 / 流式输出 | ✅ |
| 待办 | 编排升级到「方案 B」（各 worker 独立上下文） | ⏳ |

## 设计要点

- **模型**：`deepseek-flash`（DeepSeek 官方 Anthropic 兼容端点）。它是**带 thinking 的模型** ——
  回复内容是 block 列表而非纯字符串，且**不能伪造 assistant 消息**（否则 API 报 400）。
- **持久化**：`PostgresSaver` + `thread_id`，会话跨进程续跑；这就是"短期记忆"的本体。
- **上下文压缩**：工具内源头截断 → 清理旧工具结果 → supervisor 只看摘要（带长度上限）。
- **工具失败**：工具内部捕获并回传错误给模型，避免整图崩溃；但工具不可抛 `GraphBubbleUp`（否则 interrupt 失效）。
- **最小权限**：只有 Verifier 能执行命令、只有 Coder 能改代码。
- **并发**：同一轮的多个工具调用会**并行执行**，因此文件写入必须串行化。

详细的设计决策与踩坑记录见 [DEVLOG.md](DEVLOG.md)。
