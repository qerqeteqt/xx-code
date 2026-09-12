# xx-code

基于 **LangGraph** 的多 Agent 编码助手（CLI）。

用 **Supervisor 中心调度** 组织三个 Agent —— **Explorer**（定位代码）/ **Coder**（修改代码）/ **Verifier**（跑测试验证）—— 对指定代码仓库完成「定位 → 修改 → 验证」的闭环。高风险命令（`rm -rf`、`git push` 等）会**暂停并等你确认**。

> 这是一个从零构建的学习型项目，目标是能看清每一步的数据如何在 Agent 之间流动。

## 当前状态

**M3 完成**：整条链路已打通 —— 给一句话任务，Supervisor 自动调度 Explorer / Coder / Verifier
完成「定位 → 修改 → 验证」，高风险命令会暂停等你确认，会话由 PostgreSQL 持久化。
70 个单测通过。

## 环境要求

- Python **3.13**（本项目在 conda env `langgraph` 中开发）
- **PostgreSQL**：用作 LangGraph checkpointer，提供跨进程的会话持久化
- **DeepSeek API**：走 Anthropic 兼容端点

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置环境变量（.env 已被 gitignore，不会进仓库）
cp .env.example .env
#    然后编辑 .env，至少填写：
#      AGENT_AUTH_TOKEN  DeepSeek API key
#      AGENT_PG_DSN      PostgreSQL 连接串

# 3. 自检：验证模型连通与 tool calling
python -m code_agent.cli --check

# 4. 运行单元测试
python -m pytest

# 5. 完整模式：Supervisor 调度三个 worker 自动完成任务
python -m code_agent.cli --repo <目标仓库路径> "运行测试，把失败的修好"
#    过程会打印调度决策与各 agent 的工具调用；危险命令会暂停等你输入 y/N

# 6. 单 agent 模式：只跑一个 worker
python -m code_agent.cli --repo . --agent explorer "简要说明这个项目的模块划分"
#    --agent 可选 explorer / coder / verifier；coder 会真实修改文件，注意目标仓库
```

## 目录结构

```
xx-code/
├── code_agent/          # 主包
│   ├── config.py        # 配置加载 + 模型构建
│   ├── cli.py           # 命令行入口 + HITL 交互
│   ├── paths.py         # 目标仓库路径围栏
│   ├── messages.py      # text_of()：从 thinking 模型的消息里取正文
│   ├── state.py         # 父图共享 State
│   ├── tools/           # 工具集
│   │   ├── _util.py        # safe：工具异常兜底（放行 interrupt）
│   │   ├── filesystem.py   # read_file / write_file / edit_file / list_dir
│   │   ├── search.py       # glob_search / grep_search
│   │   ├── command.py      # run_command + 危险命令 HITL
│   │   └── web.py          # 联网检索                     (M4)
│   ├── workers.py       # Explorer / Coder / Verifier 三个 worker 子图
│   ├── supervisor.py    # 中心调度（JSON 路由 + 指令注入）
│   └── graph.py         # 组装 StateGraph + PostgreSQL checkpointer
├── data/                # 样例仓库等数据
├── models/              # 本地模型（暂空）
├── tests/               # 单元测试（70 passed, 1 skipped）
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
| M4 | 联网检索 + 输出美化 + 完整文档 | ⏳ |

## 设计要点

- **模型**：`deepseek-flash`（DeepSeek 官方 Anthropic 兼容端点）。注意它是**带 thinking 的模型**，回复内容是 block 列表而非纯字符串。
- **持久化**：`PostgresSaver` + 固定 `thread_id`，会话可跨进程续跑。
- **上下文压缩**：工具内源头截断 → 清理旧工具结果 → 摘要兜底（三层，按需开启）。
- **工具失败**：工具内部捕获并回传错误给模型，配合重试与调用限流，避免整图崩溃。
- **最小权限**：只有 Verifier 能执行命令，因此人工确认只需挂在一处。

详细的设计决策与踩坑记录见 [DEVLOG.md](DEVLOG.md)。
