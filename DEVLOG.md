# 开发日志 (DEVLOG)

记录每一步**实际执行的操作**，方便回溯"后面具体干了什么"。

## 记录约定

每推进一个里程碑或做一次有意义的环境变更，追加一节：

```
## M<n> — 标题（YYYY-MM-DD）

### 目标    这一节要达成什么
### 改动    新增/修改了哪些文件、为什么
### 验证    用什么命令验证、结果如何
### 备注与坑 踩到的坑、重要发现、对后续的影响
```

---

## M0 — 项目骨架与模型自检（2026-09-12）

### 目标

建立项目骨架，并验证最大的未知数：**DeepSeek 端点是否连通、是否支持 tool calling**。

### 改动

| 文件 | 说明 | 是否进仓库 |
|---|---|---|
| `.gitignore` | 排除 `.idea/`、`.env`、`__pycache__`、`.pytest_cache` 等 | ✅ |
| `.env.example` | 环境变量模板，无真实值 | ✅ |
| `.env` | **本地私有**：DeepSeek token + PostgreSQL 密码 | ❌（gitignore） |
| `requirements.txt` | 锁定直接依赖版本 | ✅ |
| `code_agent/__init__.py` | 包声明与结构说明 | ✅ |
| `code_agent/config.py` | `Settings.from_env()` + `build_llm()` | ✅ |
| `code_agent/cli.py` | CLI 入口 + `--check` 自检（M0 唯一功能） | ✅ |
| `data/.gitkeep`、`models/.gitkeep` | 空目录占位，否则 git 不跟踪 | ✅ |

同时完成：git 仓库初始化（分支 `main`）、`.idea/` 移出版本控制、推送到私有仓库 `github.com/qerqeteqt/xx-code`。

### 验证

```bash
cd D:\pycharm\Mutil-Agent
"C:\Users\x_x\.conda\envs\langgraph\python.exe" -m code_agent.cli --check
```

结果：三步全过

```
[1/3] 配置加载成功  (model=deepseek-flash base_url=https://api.deepseek.com/anthropic token=***)
[2/3] 文本调用成功  -> '正常'
[3/3] tool calling 正常  -> [{"name": "echo", "args": {"text": "tool-ok"}, ...}]
```

### 备注与坑

1. **⚠️ `deepseek-flash` 是带思考(thinking)的模型。** `AIMessage.content` **不是字符串**，而是 block 列表：
   ```python
   [{'type': 'thinking', 'thinking': '...', 'signature': '...'},
    {'type': 'text',     'text': '正常'}]
   ```
   - 影响：后续做 CLI 流式输出、日志、以及任何读取 `content` 的地方，**必须处理 block 列表，不能假设是 `str`**。
   - 影响：Anthropic 的 extended thinking 在**多轮工具调用**中要求把 thinking block 原样回传；langchain 已代为处理，但**不要手动裁剪消息内容**，否则可能报签名校验错误。
2. 端点确认为 **DeepSeek 官方 Anthropic 兼容端点** `https://api.deepseek.com/anthropic`（不是第三方中转），因此 `ChatAnthropic` + 自定义 base_url 的方案成立。
3. 运行时会有一条无害告警 `RequestsDependencyWarning: ... chardet or charset_normalizer`（某个依赖导入 `requests` 触发），不影响功能，暂不处理。
4. **必须用 conda env `langgraph` 的 python（3.13）**。PATH 上的默认 python 是 3.10 且未装 langgraph。
5. Windows 控制台是 cp936，`cli.py` 里已强制 `stdout/stderr` 为 UTF-8，否则中文输出会抛 `UnicodeEncodeError`。

---

## M1 — 文件与搜索工具（2026-09-12）

### 目标

落地所有文件/搜索工具，并**在不依赖模型的情况下**用单元测试验证。核心是安全（路径围栏）与稳定（工具不抛异常、输出有截断）。

### 改动

| 文件 | 说明 |
|---|---|
| `code_agent/paths.py` | `RepoRoot` 路径围栏：`resolve()` 越界抛 `PathEscapeError` |
| `code_agent/tools/_util.py` | `safe` 装饰器：工具任何异常都转成 `Error: ...` 字符串返回 |
| `code_agent/tools/filesystem.py` | `list_dir` / `read_file` / `write_file` / `edit_file` |
| `code_agent/tools/search.py` | `glob_search` / `grep_search`（**自研**，见坑 1） |
| `code_agent/tools/__init__.py` | 工具层说明 |
| `tests/test_paths.py` | 围栏单测（含 `..` 逃逸、绝对路径越界、虚拟路径） |
| `tests/test_filesystem.py` | 文件工具单测（含截断、`old_string` 不唯一、越界拦截） |
| `tests/test_search.py` | 搜索工具单测（含中文文件回归） |
| `pytest.ini` | `testpaths=tests`、`pythonpath=.`、`-q` |

设计要点：
- **最小权限**：`build_filesystem_tools(root, allow_write=False)` 给 Explorer 只读工具集；写类工具只给 Coder。
- **源头截断**（第一层上下文压缩）：`read_file` 最多 400 行、`list_dir` 最多 200 项、`grep_search` 最多 100 条。
- **搜索结果路径可直接回传**：`grep_search` 返回 `code_agent/config.py:65:...`，其中路径可直接喂给 `read_file`（已实测）。
- `grep_search` 跳过 `.git` / `__pycache__` / `node_modules` 等目录与二进制文件。

### 验证

```bash
"C:\Users\x_x\.conda\envs\langgraph\python.exe" -m pytest
# => 40 passed

# 注：当时还加过一个 `--demo-tools` 命令（在临时目录里演示写入/读取/替换/搜索/越界拦截）
# 用于手动验证。M2 时按「不为测试加脚手架、代码尽量简洁」的要求**已删除**该命令，
# 上述场景现由 tests/ 下的单测覆盖。
```

真实项目自测（仓库里全是中文注释，正好是回归场景）：

```
[glob] **/*.py        -> code_agent/__init__.py | code_agent/cli.py | ... (11 个)
[grep] def build_llm  -> code_agent/config.py:65:def build_llm(settings: Settings | None = None, ...
[grep] 上下文压缩      -> code_agent/tools/filesystem.py:17 ... / README.md:69 ...   ← 中文检索正常
[read] 用 grep 结果路径读取 -> 成功
```

### 备注与坑

1. **⚠️ 内置 `FilesystemFileSearchMiddleware` 在 Windows 上有编码 bug，已弃用。**
   它的纯 Python 回退（本机无 ripgrep，必走这条）用 `file_path.read_text()` **不带 encoding**，
   Windows 默认 cp936，遇到含中文的 UTF-8 文件抛 `UnicodeDecodeError` 后被
   `except (UnicodeDecodeError, PermissionError): continue` **静默跳过**。
   实测：纯 ASCII 文件能搜到，含中文的一律搜不到 —— 对中文代码库等于搜索报废。
   → 因此 `tools/search.py` 改为**自研**，显式 `encoding="utf-8", errors="replace"`，
   并顺带解决下面第 2 条、加上截断与忽略目录。
2. 内置中间件返回的是 `"/code_agent\cli.py"` 这种**带前导斜杠、夹杂反斜杠的虚拟路径**，
   直接回传给 `read_file` 会被当成绝对路径 → 判定越界。
   → 自研搜索工具直接返回干净的 POSIX 相对路径，问题从源头消除。
   （M1 当时还额外让 `resolve()` 兼容"无盘符前导斜杠"；**M2 发现那是死代码且有跨平台语义问题，已移除**，见 M2 节坑 1。）
3. `safe` 装饰器是硬性要求：`create_agent` 默认只吞参数校验错误，工具体里的其他异常会
   **冒泡崩掉整个 run**。所以每个工具都必须保证不抛异常。
4. `RepoRoot` 的安全性依赖 `Path.resolve()` 会展开 `..` 并跟随符号链接，因此指向仓库外的
   符号链接也会被正确拒绝。

---

## M2 — 三个 worker 子图（2026-09-12）

### 目标

用 `create_agent` 构建 Explorer / Coder / Verifier 三个 worker 子图，让**单个 worker 能独立跑通完整 ReAct 循环**（真实调模型，不是 mock）。

### 改动

| 文件 | 说明 |
|---|---|
| `code_agent/workers.py` | 三个 worker：独立 system_prompt + 裁剪工具集 + 共用中间件 |
| `code_agent/cli.py` | 重写：删除 `--demo-tools`（测试脚手架）；新增 `--agent` 单 agent 模式 + `task` 位置参数；新增 `_text_of()` |
| `code_agent/paths.py` | **移除**"虚拟路径"处理（死代码，见坑 1） |
| `tests/test_paths.py` | 删虚拟路径用例；改为断言前导 `/` 被拒；**新增符号链接越界用例** |
| `tests/test_filesystem.py` | 同步替换为"前导 `/` 被拒" |

设计要点：

- **最小权限**：Explorer 只读；Coder 只读 + `write_file`/`edit_file`；Verifier 只读（M3 加 `run_command`）。
- 三个 worker 共用中间件：`ContextEditingMiddleware`（清旧工具结果）+ `ToolRetryMiddleware(on_failure="continue")` + `ToolCallLimitMiddleware(run_limit=25)` + `ModelCallLimitMiddleware(run_limit=12)`。
  （`ToolRetryMiddleware` 在 **M3 已移除** —— 它会吞掉 `interrupt()` 导致 HITL 失效，见 M3 节坑 4。）
- **worker 子图不装 checkpointer**，持久化统一留给根图（M3）。
- 每个 worker 是 `create_agent(...)` 编译出的独立子图，可直接 `stream()` 运行。
- **主动跳过计划里的 `state.py`**：单 worker 用不到，等 M3 组图时再加，不留无人使用的代码。

### 验证

真实调用模型跑三个角色：

```
# Explorer
python -m code_agent.cli --repo . --agent explorer "简要说明这个项目的模块划分..."
  → 16 次工具调用（list_dir / read_file 逐步深入），产出准确的模块职责报告

# Coder（在临时仓库验证写权限）
  → list_dir → glob_search → read_file → edit_file → read_file 复验
  → 文件确实被修好：`return a - b` → `return a + b`
  → 且正确选择了 edit_file 而非 write_file

# Verifier
  → 12 次工具调用做静态审查，给出结论并附理由

python -m pytest
  → 39 passed, 1 skipped
```

### 备注与坑

1. **Verifier 发现了设计问题（这正是多 Agent 的价值）**。M1 为兼容内置中间件加了
   "无盘符前导斜杠按仓库根相对解释"，它指出：
   (a) 该逻辑使 `test_escape_via_absolute_outside` 变成 Windows 专用，在 POSIX 上会失败；
   (b) POSIX 下 `read_file("/etc/passwd")` 会被静默解释成 `<repo>/etc/passwd`（无安全漏洞，但语义错误）。
   核查后确认：M1 已弃用内置中间件、自研搜索返回纯相对路径 → **这段兼容逻辑是死代码**。
   → 直接移除：代码更简单，两个问题一并消失。
2. `thinking` block 已处理：`cli.py` 的 `_text_of()` 只取 `type == "text"` 的块，跳过 thinking。
3. 符号链接越界用例在本机被 `skip`（Windows 创建符号链接需权限），逻辑仍由 `Path.resolve()` 保证。
4. 单 agent 模式用 `stream_mode="updates"` 实时打印工具调用轨迹，足以看清 ReAct 循环。

---

## M3 — Supervisor 调度 + 危险命令 HITL（2026-09-12）

### 目标

把三个 worker 组装成完整的 Supervisor 图，接上 PostgreSQL 持久化，并实现
「**仅高风险命令**需人工确认」的 HITL。至此一条命令即可交给它自动完成
「定位 → 修改 → 验证」。

### 改动

| 文件 | 说明 |
|---|---|
| `code_agent/state.py` | `OverallState`（`messages` + `attempts`） |
| `code_agent/messages.py` | `text_of()`：只取 `text` block，跳过 `thinking` |
| `code_agent/tools/command.py` | `run_command` + 危险命令规则表 + 工具内 `interrupt()` |
| `code_agent/supervisor.py` | JSON 路由 + 确定性兜底 + **指令注入** |
| `code_agent/graph.py` | 父图组装 + `open_checkpointer()`（PostgresSaver） |
| `code_agent/workers.py` | Verifier 加 `run_command`；**移除 `ToolRetryMiddleware`**；加通用约束 |
| `code_agent/cli.py` | 完整模式 `cmd_run` + interrupt 交互 + 消息归属去重 |
| `code_agent/tools/_util.py` | `safe` 放行 `GraphBubbleUp` |
| `tests/test_command.py` | 危险命令识别（21 个用例，含 dry-run 豁免） |

设计要点：
- supervisor 用 `Command(goto=..., update=...)` 直接路由（不用 `conditional_edges`）；
  worker 干完 `add_edge(worker, "supervisor")` 回到调度。
- **checkpointer 只装在根图**，worker 子图不装。
- HITL：`run_command` 命中危险规则 → 工具内 `interrupt()` → 穿透子图到根图 → CLI 询问 → `Command(resume=...)`。
- 只有 Verifier 有 `run_command`（最小权限），所以 HITL 只需挂一处。

### 验证

```bash
"C:\Users\x_x\.conda\envs\langgraph\python.exe" -m pytest
# => 70 passed, 1 skipped
```

**端到端（真实调模型，自动完成修复）**：

```bash
python -m code_agent.cli --repo D:/tmp_m3_final2 "运行测试，把失败的修好"
```

```
[supervisor] 下一步 → verifier（任务需要先执行测试获得失败信息，只有 verifier 能执行命令）
[verifier] → list_dir / glob_search / read_file ×2 / run_command ×2
[supervisor] 下一步 → coder（需修改代码，verifier 不能改代码）
[coder] → read_file / edit_file / read_file ×2          ← 一次到位，无空转
[supervisor] 下一步 → verifier（coder 已修复，需重新执行 pytest 验证）
[verifier] → run_command(pytest) / run_command(行为复核)
[supervisor] 下一步 → finish（1 passed，任务全部完成）
```

失败测试被修好，流程**自然收敛**（没有撞轮次上限）。

**HITL 在真实 CLI 中触发**（管道喂 `n` 模拟拒绝）：

```
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
需要你确认一条危险命令：
  命令: git clean -fdn
  原因: 清理未跟踪文件
  执行吗？[y/N]
```

确认命令被拒绝后，`scratch.tmp` 仍在磁盘上 —— **安全兜底有效，命令确实没有执行**。

### 备注与坑

这一节的坑特别密集，且**大部分是"思考模型 + 中文化"带来的**：

1. **`with_structured_output` 在本项目不可用**。`deepseek-flash` 是思考模型，DeepSeek 的
   Anthropic 兼容端点在思考模式下**拒绝强制 tool_choice**：
   `400 Thinking mode does not support this tool_choice`。
   → supervisor 改为「提示只输出 JSON → 正则抠 `{...}` → Pydantic 校验 → 规则兜底」。

2. **不能往共享 `messages` 里写人工构造的 `AIMessage`**。思考模型要求 assistant 消息
   必须原样回传 `thinking` 块，任何手工构造的 assistant 消息都会报
   `400 The content[].thinking in the thinking mode must be passed back to the API`。
   → supervisor 的路由结果只 `print`，不进入 state；需要给成员传话时用 **`HumanMessage`**。

3. **`safe` 装饰器会吞掉 `interrupt()`**。`GraphInterrupt` 继承自 `Exception`，
   被 `except Exception` 捕获后 HITL 永远不触发。
   → `safe` 增加 `except GraphBubbleUp: raise` 放行。

4. **`ToolRetryMiddleware` 同样会吞掉 `interrupt()`，且无法配置规避**。
   它的 `wrap_tool_call` 是 `except Exception`（`tool_retry.py:317`），没有放行
   LangGraph 控制流异常的机制：`GraphInterrupt` 被当成可重试失败，重试 3 次后转成错误
   ToolMessage。症状是——命令**没有执行**（安全没破），但用户**收不到确认提示**。
   → 直接移除该中间件；我们的工具都经 `safe` 兜底、从不抛异常，本就不需要它。

5. **子图返回的状态包含它继承的整段共享历史**，导致 CLI 把上一位 agent 的动作
   误标成当前节点（如 `[verifier] → edit_file`）。因为 `add_messages` 按消息 id 去重，
   状态本身**没有重复**（已核对：27 条消息、1 条 HumanMessage）。
   → CLI 打印时按消息 id 去重；`seen` 集合需**跨 resume 保留**，否则中断恢复后会把
   中断前的消息重复打印。

6. **`git clean -fdn`（dry-run）被误报为危险命令**。dry-run 不改动任何文件。
   → 规则表增加"豁免正则"机制，`git clean` 带 `-n` / `--dry-run` 时不要求确认。

7. **worker 会"照抄不动手"**（方案 A 共享历史的固有代价）：再次被调用时看到上一位的
   结论，于是复述而不行动，空转烧掉轮次预算（实测同一份"不通过"结论被产出 4 次，
   最终撞上 `MAX_ATTEMPTS`）。
   → supervisor 路由时**附带一条具体的祈使句指令**（以 `HumanMessage` 注入）。
   实测同一任务从"4 次空转 + 撞上限"变为"一次到位 + 自然收敛"。

8. `run_command` 会**把当前解释器目录前置到子进程 PATH**，这样 `python`/`pytest`
   解析到 conda env `langgraph`（否则会落到 PATH 上的 3.10）。命令在 `cmd.exe` 中执行。

9. Windows 上 `subprocess` 的 `start_new_session` 是**无效参数**，超时杀进程必须用
   `taskkill /F /T /PID` 带走整棵进程树。

10. **supervisor 起初不知道成员的能力边界，会把任务派给没有相应工具的成员**。
    实测它把「执行命令」派给了 coder（coder 确实没有 `run_command`），coder 如实拒绝后
    supervisor 就直接 `finish`，HITL 根本没机会触发。
    → 在 supervisor 系统提示里写明**能力矩阵**（只有 verifier 能执行命令、只有 coder 能改代码），
    并要求「结束前确认没有把任务派给不具备相应能力的成员」。
    - 附带发现：期间模型还**幻觉过自己的工具集** —— verifier 声称自己只有 6 个文件工具、
      没有 `run_command`，而它在同一轮明明调用过 `run_command`。
      说明"能力边界"不能指望模型自省，必须在提示里显式声明。

11. **worker 需要一条通用约束**：ReAct 循环在模型「只回文字、不调工具」时就会结束，
    实测 coder 曾只描述计划而不动手。
    → `workers._build()` 给三个 worker 的 system_prompt 统一追加 `_ACT_NOW`：
    不要只描述计划、必须实际调用工具推进任务、只有任务确实完成才输出最终汇报。

---

## M4 — 联网检索 + 输出美化 + 文档（2026-09-12）

### 目标

收尾：接上联网检索、美化 CLI 输出、评估上下文压缩是否需要加摘要层。

### 改动

| 文件 | 说明 |
|---|---|
| `code_agent/tools/web.py` | 新增 `web_search`（Tavily），未配 key 时自动禁用 |
| `code_agent/workers.py` | Explorer 接入 `web_search`；`_build` 支持 `checkpointer` |
| `code_agent/cli.py` | `rich` 美化（角色配色 + 面板）；抽出 `_stream_once`/`_drive`；`--agent` 补 checkpointer |
| `tests/test_web.py` | 新增：启用条件 + 输出截断 |
| `README.md` | 补完整模式用法、目录结构、路线图收尾 |

设计要点：

- **不用裸的 `TavilySearch`**，而是包一层 `@tool` + `safe`：既保证"工具绝不抛异常"
  （网络失败返回错误字符串而不是崩掉整个 run），又对结果做**源头截断**
  （Tavily 返回的正文很长，`MAX_CHARS=2000`）。
- 未配置 `TAVILY_API_KEY` → `build_web_tools()` 返回空列表，`web_search` 自动禁用、不影响其余功能。
- `--agent` 模式改用 `InMemorySaver`：修掉 M3 遗留的崩溃点（见坑 1）。
- rich 输出：角色配色 + 最终结论面板；所有来自模型/工具的动态文本都 `escape()`（见坑 2）。

### 验证

```bash
"C:\Users\x_x\.conda\envs\langgraph\python.exe" -m pytest
# => 75 passed, 1 skipped
```

**`web_search` 实测**（显式要求联网，确认接线正确）：

```
python -m code_agent.cli --repo D:/tmp_m4_web --agent explorer "请用 web_search 查一下 Command(goto=) 的用途"
explorer  → web_search({"query": "LangGraph Command goto 用途"})
explorer  → web_search({"query": "LangGraph Command(goto=...) usage documentation"})
→ 返回官方博客 / API 参考等结果
```

**端到端回归**（角色配色 + 结论面板）：

```
[supervisor] 下一步 → verifier（尚未运行过测试…）
verifier  → list_dir / glob_search / read_file ×2 / run_command ×4
[supervisor] 下一步 → coder（已跑出失败：mathx.py 的 double 返回 n+n+1）
coder     → edit_file / read_file
[supervisor] 下一步 → verifier（已修复，待验证）
verifier  → run_command(pytest) / run_command(边界抽查) / read_file ×2
[supervisor] 下一步 → finish（1 passed，任务完成）
┌──────── 最终结论 ────────┐   ← 绿色面板，内容为 verifier 的「## 通过」报告
```

### 上下文压缩评估（基于实测数据，不是拍脑袋）

单次小型任务（改 1 个文件 + 跑测试）：

| 指标 | 数值 |
|---|---|
| 消息总数 | 31（Human 4 / AI 12 / Tool 15） |
| 消息文本总字符 | ~14,600（粗估 ≈5.8k tokens） |
| **supervisor 实际看到的** | 3,040 字符（只喂摘要，不喂原始消息） |

结论：**暂不加 `SummarizationMiddleware`**。已有机制已能支撑中小任务：
① 工具内源头截断 ② `ContextEditingMiddleware` 只留最近 3 条工具结果（且不写 checkpoint）
③ supervisor 只吃摘要。
摘要层要多一次 LLM 调用、且失败会被静默吞掉，收益不明显。
**重新评估的触发条件**：单次运行消息数 > 60 条，或消息文本总字符 > 60k。

### 备注与坑

1. **`--agent` 模式下 worker 调用危险命令会崩**。`interrupt()` 需要 checkpointer，
   而单 agent 模式原本没有 → 抛异常。→ 该模式改用 `InMemorySaver`，现在也能正常暂停确认。
2. **rich 会把内容里的 `[...]` 当成标记解析**（我们的 trace 本身就用 `[角色]` 做标记，
   模型输出里也常有方括号）。→ 所有动态文本经 `rich.markup.escape()`，`Console(highlight=False)`。
3. `--agent` 模式顶层就是 worker 自身，节点名是内部的 `model`/`tools` 而非角色名
   → 打印时用 `label` 覆盖成角色名。
4. `TavilySearch` **缺 key 时构造不报错、调用才报错**，所以必须自己判断 key 是否存在。

---

## 补测 — 集成测试安全网（2026-09-12）

### 目标

在动「方案 B」重构之前，先给最容易改坏的部分（supervisor 路由、图接线、HITL 暂停/恢复）
上自动化保护。在此之前这 75 个单测**全是纯函数**，核心调度只有手工验证记录。

### 改动

| 文件 | 说明 |
|---|---|
| `tests/conftest.py` | `ScriptedModel`（脚本化模型，不联网）+ `fake_settings` 夹具 |
| `tests/test_supervisor.py` | 9 个用例：JSON 路由 / `finish`→`END` / 坏 JSON 兜底 / 模型抛异常兜底 / 轮次上限 / 指令注入 |
| `tests/test_hitl.py` | 4 个用例：危险命令暂停（含原因）/ 拒绝后文件不动 / 批准后真执行 / 安全命令不暂停 |
| `tests/test_graph.py` | 2 个用例：图接线（节点与边）、子图消息不重复累加 |
| `data/sample_repo/` | 故意含 2 个失败用例的**练手靶子** |

**使能技巧**：`FakeMessagesListChatModel` 没实现 `bind_tools`（抛 `NotImplementedError`），
子类化成 `ScriptedModel` 把 `bind_tools` 变成 no-op，就能驱动完整的 `create_agent`
与整张图 —— **不联网、秒级、不花钱**。

### 验证

```bash
"C:\Users\x_x\.conda\envs\langgraph\python.exe" -m pytest
# => 90 passed, 1 skipped（新增 15 个）
```

### 备注与坑

1. 集成测试最大的价值是**把"静默失败"变成"断言失败"**：方案 B 的通道声明若漏了一边，
   字段会被静默丢弃、程序照常跑不报错；有测试后 1 秒就暴露。
2. HITL 的「拒绝后文件仍在 / 批准后文件真没了」现在是**自动化断言**，
   不再依赖手工验证 —— 这是安全属性的回归保护。
3. `data/sample_repo` 不在 `pytest.ini` 的 `testpaths = tests` 收集范围内，
   它的失败用例不会污染项目自身测试（已实测确认）。
4. **`sample_repo` 嵌在项目里会继承项目根的 `pytest.ini`**：首次真实演示时，
   在 `sample_repo` 里跑 `python -m pytest` 的 `rootdir` 变成了项目根
   （`configfile: pytest.ini`、`testpaths=tests`），收集范围错乱，
   Verifier 多花了约 8 次工具调用去排查"测试到底在哪"。
   → 给 `sample_repo` 加了它自己的 `pytest.ini`，使其成为**自包含**的仓库。
   （对真实仓库同理：把 `--repo` 指向大项目的子目录时，可能遇到同类的配置继承问题。）
5. **文档里的命令是按 bash 写的，但用户实际用 PowerShell** —— PowerShell 里
   `"路径" 参数` 会被解析成表达式，报 `表达式或语句中包含意外的标记 "-m"`，
   必须写成 `& "路径" 参数`（调用运算符）。
   → README「快速开始」补了 PowerShell / cmd / git bash 三种写法，
   并推荐 `conda activate langgraph`（用户的 PowerShell 已初始化 conda，实测可用）。
   **教训：写命令示例前先确认用户用的是什么 shell。**

---

## 修复 — 并行工具调用导致编辑丢失（2026-09-12）

### 现象

用户**第一次真实运行**（`--repo data/sample_repo "运行测试，把失败的修好"`）的日志里有可疑痕迹：
coder 先后发起三个 `edit_file`，其中**第三个的 `old_string` 与第一个完全相同**，却也返回了"已修改"。

### 定位

从持久化状态里按 `tool_call_id` 严格配对后确认：msg#21 的**一条 `AIMessage` 携带了两个
`edit_file` 调用**（改 add、改 div），两个都报成功；而第三个编辑能成功，说明**第一次的改动已被回退**。

实测确认根因（`tests/test_tool_concurrency.py` 的思路验证）：

```
X start +0.00 / Y start +0.00 / X end +0.60 / Y end +0.60   总耗时 0.61s（而非 1.2s）
```

→ **LangGraph 的 ToolNode 会并行执行同一条 AIMessage 里的多个工具调用**。
而 `edit_file` / `write_file` 是"读-改-写"，两个并行就互相覆盖 —— **丢失更新**。

### 复现

`tests/test_tool_concurrency.py`：人为放大"读"与"写"之间的窗口后**确定性复现**：

```
修复前：文件最终为 'A = 1\nB = 200\n'  ← 第一处修改整块丢失
```

### 修复

`code_agent/tools/filesystem.py` 增加进程内 `_WRITE_LOCK`，
把 `edit_file` / `write_file` 的读-改-写整体串行化。

### 验证

```bash
"C:\Users\x_x\.conda\envs\langgraph\python.exe" -m pytest tests/test_tool_concurrency.py
# 修复前：FAILED（A = 100 丢失）；修复后：PASSED
"C:\Users\x_x\.conda\envs\langgraph\python.exe" -m pytest
# => 91 passed, 1 skipped
```

### 备注与坑

1. **这个 bug 是靠"用户第一次真实运行"的日志才发现的** —— 此前的单测与两次演示都没撞上，
   因为文件读写太快、竞态窗口极小；用假模型做并发测试时也一次就"通过"了（纯属侥幸）。
   → 说明**让真实使用者尽早跑起来**不可替代。
2. 影响面：不修的话，agent 的并行编辑会**静默丢改动**，表现为"改了好几处却只生效一处"，
   模型被迫重做（白花调用）；严重时文件只被改了一半。
3. 排查手法值得留存：日志里"**两个内容相同的编辑都成功了**"就是丢失更新的典型指纹。
4. `_WRITE_LOCK` 只保护**本进程内**的并发 —— 对当前"单进程 CLI"够用；
   若将来有多个进程同时改同一仓库，需要换成文件锁。

---

## 新增 — 交互模式与短期记忆（2026-09-13）

### 目标

把 `xx-code` 从"一次性批处理"变成**可以对话的 agent**，并让它记住上文 ——
原先只能 `python -m code_agent.cli --repo X "任务"` 跑完即退出，不能追问。

### 改动

| 文件 | 说明 |
|---|---|
| `code_agent/cli.py` | 新增 `--chat` 交互模式、`--new`；会话记录（每个仓库最近一次会话）；`_clean()`；stdin 也强制 UTF-8 |
| `code_agent/supervisor.py` | **收尾时产出面向用户的回答**；摘要加 4000 字符上限 |
| `pyproject.toml` | 新增 `xx-code` 命令入口（需 `pip install -e .`） |
| `.gitignore` | 忽略 `.code_agent_sessions.json` |
| `tests/test_chat_session.py` | 新增 5 例（会话记录的存取/覆盖/损坏恢复） |
| `tests/test_supervisor.py` | 更新 finish 断言 + 摘要上限 + `attempts` 逐轮语义 |

设计要点：

- **短期记忆 = 同一个 `thread_id` 贯穿会话**，消息由 `PostgresSaver` 持久化；
  三个 worker 通过共享 `messages` 黑板自然看到上文。
- **跨次记忆**：记录"每个仓库最近一次会话"，`--chat` 下次自动续接（`:new` 开新会话）。
- **`attempts` 逐轮重置**：每轮输入传 `attempts=0`。它是"本轮任务的调度次数上限"，
  不是整个会话的上限 —— 不重置的话续聊时新一轮会被立刻结束。

### 验证

```bash
"C:\Users\x_x\.conda\envs\langgraph\python.exe" -m pytest
# => 98 passed, 1 skipped
```

**多轮对话（同一会话内追问）**：

```
>>> 运行测试，把失败的修好
[supervisor] → verifier → coder → verifier → finish      ← 修好 calc.py 的 2 个 bug
>>> 你刚才改了哪个文件？改了什么？凭记忆直接回答。
[supervisor] 下一步 → finish（用户最后是在提问，而非继续干活）
>>> 改的是 data/sample_repo/calc.py，只动了这一个文件…add 改成加法、div 加了除零判断
```

**跨次记忆（新进程、不加 `--new`）**：

```
会话 48a03a09…（已续接上次会话）
>>> 我之前让你改的是哪个文件？改了什么？
→ 改的是 data/sample_repo/calc.py，只动了这一个文件…（凭记忆作答，未重新调查）
```

### 备注与坑

1. **stdin 没强制 UTF-8 → 中文变成代理字符 → API 直接报错**。
   `UnicodeEncodeError: 'utf-8' codec can't encode character '\udca1' ... surrogates not allowed`。
   一次性模式的任务来自 `sys.argv`（Windows 宽字符 API 解码，中文正常），所以一直没暴露；
   交互模式从 **stdin** 读，管道/重定向时会按 cp936 解码 → 产生代理字符。
   → 三个标准流都强制 UTF-8，并在输入边界加 `_clean()` 清掉代理字符。
2. **这个图原本不会"回答问题"**。三个 worker 都是工具驱动的，supervisor 只会派活；
   用户问"你刚才改了什么"时它直接 `finish`，CLI 于是把**上一轮的旧报告**当作答案显示 ——
   表现成"答非所问、内容是旧的"。
   → 让 supervisor 在收尾时**自己生成回答**（用模型生成的真实 `AIMessage`，
   不是手工构造，因此不违反思考模型的约束），并用**确定性 id** 防 resume 时重复追加。
   同时在路由提示里写明"用户是在提问而非派活时应选 finish 并直接回答"。
3. **`attempts` 原本跨轮累积**：续聊时状态里残留的计数会让新一轮立刻被上限结束。
   → 每轮输入传 `attempts=0`，并用测试把该语义固定下来
   （`test_attempts_is_a_per_turn_counter`）。
4. **共享历史的交叉污染（方案 A 的实证）**：实测 **coder 模仿 verifier 调用了 `run_command`**
   —— 一个它根本没有的工具。ToolNode 返回
   `Error: run_command is not a valid tool, try one of [list_dir, read_file, write_file, edit_file, glob_search, grep_search]`。
   **安全没被突破**（工具 schema + ToolNode 拦住了），但白费一次模型调用。
   → 先在 worker 提示词里写明"你不能执行命令/不能改文件"，缓解这个问题；
   **根本解法仍是方案 B**（各 worker 独立通道，不共享原始对话）。
5. supervisor 的摘要加了 4000 字符上限（保留开头任务 + 结尾进展）。
   交互模式下会话会越来越长，不设上限的话每轮都变慢变贵。
6. `xx-code` 命令入口写在 `pyproject.toml` 里，但**没有自动安装** ——
   需要时自己跑一次 `pip install -e .`（依赖刻意声明为空，不会改动现有环境）。

---

## 修复 — 交互模式"复读旧回答"（2026-09-13）

### 现象

用户反馈 agent "很智障"：问「你还记得我刚刚做了什么吗？」，回答与上一轮**一字不差**。

### 定位（从持久化状态里捞出来的证据）

```
# 1 AIMessage id=supervisor-answer-1  '记得。你刚刚做了这几件事：…'   ← 其实是第二轮生成的对答案！
#35 AIMessage id=supervisor-answer-4  '可以，直接跟我说需求就行。…'   ← 上一轮的旧回答
#36 HumanMessage                     '你还记得我刚刚做了什么吗？'
```

第二轮的回答**其实生成正确**，但它被写到了**对话开头（#1）**，并且**覆盖掉了那条同 id 的旧消息**。

### 根因

为了让节点在 resume 重跑时幂等，我给回答用了确定性 id `supervisor-answer-{attempts}`。
而 `attempts` **每轮都会重置**（见上一节）→ 第二轮的 id 与第一轮**相同** →
`add_messages` 按 id 去重 → **新回答覆盖到旧位置**。
CLI 取"最后一条 AI 消息"时拿到的仍是上一轮的旧回答 → 表现为复读；同时**历史被污染**。

### 修复（6 处）

1. **回答 id 改用"当前最后一条消息的 id"作锚** —— 既逐轮唯一，又在同一状态重跑时保持幂等。
2. `_final_answer(messages, since=…)` **只看本轮**的消息，杜绝把上一轮的回答当结果展示；
   本轮没产出时明确显示 `(本轮没有产生回答)`。
3. 回答生成失败**不再静默吞掉**（打印错误）—— 之前正是因为静默，用户才只看到旧内容而无从察觉。
4. 回答改用**真实对话**（最近 2 轮的原始消息），不再用 `_digest` 那种压缩日志。
   之前模型只看到【动作】【汇报】日志，于是把"你还记得吗"答成一份工作汇报，甚至照抄旧回答。
5. 末尾指令明确标注「调度器内部指令，非用户发言」并要求不要提及 ——
   否则模型会把它当成用户说的话，在回答里点评这条指令（实测踩到）。
6. 回答加简洁约束（3～6 行），并强化"回顾类问题照实回答"的指引。

### 验证

```bash
"C:\Users\x_x\.conda\envs\langgraph\python.exe" -m pytest
# => 99 passed, 1 skipped（新增一条 id 撞车的回归用例）
```

**原场景复现（先派活，再问"你还记得我刚刚做了什么吗？"）** → 回答正确回顾了整段对话，
还精确指出"你只发了一句任务，另外 3 条带 `[Supervisor 指令]` 前缀的是调度器注入的、不是你发的"。

**提问轮**（"这个项目是干什么的？"）→ 6 行以内的准确回答，无复读、无内部指令泄漏。

### 追加改进（同日）

- **退出交互模式时直接打印续接命令**（含 `thread_id`）——回答"我下次怎么接着聊"。
  之前 `:q` 之后只说"会话结束"，用户不知道会话其实还在、更不知道怎么回去。
- 用 `--thread-id` 续接旧会话时，标签从"（新会话）"改为"（指定会话）"。
- 澄清一个容易误解的点：**`:q` 不删除会话**，内容都在 PostgreSQL 里；
  "自动续接"依赖 `.code_agent_sessions.json` 这个映射文件，删掉它只是失去自动续接。

### 备注与坑

- **教训：不要拿"每轮会重置的计数器"当确定性 id 的锚** —— 跨轮必然撞车，
  而 `add_messages` 按 id 去重是**静默**的（只覆盖、不报错），排查时极具迷惑性。
- 排查手法值得留存：把持久化状态按顺序连 **id** 一起打出来，一眼就看出"新回答跑到开头去了"。

---

## 新增 — 回答流式输出（2026-09-13）

### 目标

CLI 之前是"节点级 trace + 最后整块面板"，回答一次性蹦出来，没有对话感。改成流式。

### 为什么不能直接用 `stream_mode="messages"`

因为**最终回答是 supervisor 在节点内部用 `llm.invoke()` 生成的** ——
节点内部的模型调用**不会**出现在 `stream_mode="messages"` 里。所以：

1. supervisor 改用 `llm.stream()`，把**正文增量**通过 `get_stream_writer()` 推成 custom 块；
2. CLI 用 `stream_mode=["updates", "custom"]`：
   - `updates` → 工具轨迹，**interrupt 只在这个模式里出现**（两者必须并用）
   - `custom` → 回答增量，逐字打印
3. 只推**正文**增量，跳过 thinking 块（否则满屏内心活动）。

### 顺带修掉一个潜伏 bug

**流式累加出来的 content 形态和 `invoke` 不一样**：

```python
# invoke 出来（规整 block 列表）
[{'type': 'thinking', ...}, {'type': 'text', 'text': 'Python 是一种…'}]
# 流式累加出来（str/dict 混合！）
['', {'type': 'thinking', ...}, 'Python 是一种…']
```

`text_of()` 原本只认 dict 块 → 会把**流式消息读成空字符串**。
→ 已修：两种形态都认；另加 `normalize_content()`，存回 state 前把裸字符串
规范成 `{"type": "text", ...}` 块，避免下一轮发回 API 时报错。

### 验证

- **thinking 块的 signature 在流式累加后保留**（实测 `b4f948f1-…`），所以回传不会 400。
- 两轮对话实测：回答逐字流式显示；**第二轮正常**（证明流式消息回传兼容）。
- `pytest` → **105 passed, 1 skipped**（新增 `tests/test_messages.py` 6 例）。

### 说明

- **worker 的工具轨迹仍是"节点级"**（不是 token 级）—— 这是有意的：
  trace 要的是每行一次工具调用，而不是 token 汤。流式的只有"给用户的回答"。
- 回答流式输出后就不再渲染面板（`_drive` 返回 `started` 标记），避免重复显示。

---

## 新增 — 短命令入口与"当前目录即仓库"（2026-09-13）

### 目标

用户问："我只能用 `python -m code_agent.cli --chat --repo D:\...\sample_repo` 吗？也太长了，
而且这个目录是什么鬼？能不能像 Claude 那样一句话就开始对话？"

两个问题都是我此前的疏忽：**入口命令没装**、**仓库路径要手写**。

### 改动

1. **装上 `xx-code` 入口**（`pyproject.toml` 里早就写好，但一直没执行 `pip install -e .`）。
2. **`--chat` 不再追问仓库路径** —— 不传 `--repo` 就用**当前目录**（去掉了我原先加的那个
   "仓库路径 [.] :" 提示）。启动时会打印实际使用的目录，避免搞错。
3. **不带任何参数直接进对话**：`xx-code` 即开始聊天（原先无任务时是打印帮助）。
4. 退出提示改为短命令形式：
   ```
   会话已保存。下次继续：
     xx-code  （当前目录就是该仓库，会自动续接本次会话）
   ```

### 验证

```powershell
cd D:\pycharm\Mutil-Agent\data\sample_repo
xx-code
# => 仓库 D:\pycharm\Mutil-Agent\data\sample_repo
#    会话 80243508…（已续接上次会话）
#    >>>   （直接开始对话）
```

`pytest` → 105 passed, 1 skipped。

### 备注与坑

- `pip install -e .` 的依赖字段刻意为空，**不会改动环境里已有的包**；
  卸载用 `pip uninstall xx-code`。
- pip 会警告 "script xx-code.exe is installed in ... which is not on PATH" ——
  这是因为我从 bash（未激活环境）执行安装；**在 PowerShell 里 `conda activate langgraph`
  之后该目录就在 PATH 上**，`xx-code` 可直接使用。
- 顺带明确一条安全语义：**当前目录即仓库**意味着在错误的目录下启动，agent 就会操作那个目录。
  启动时打印仓库路径就是为了让这件事可见。

---

## 新增 — 每轮 token 用量统计（2026-09-13）

### 目标

用户希望每轮对话结束后能看到"这次用了多少 token"。

### 结论：可行，且**不需要额外模型调用**

LangChain 的 `AIMessage.usage_metadata` 就带 `input_tokens` / `output_tokens`。
实测本端点两条路都有：
- `invoke` → `usage_metadata` 与 `response_metadata['usage']` 都有
- `stream` → 累加后 `usage_metadata` 有（**只有最后一个 chunk 带**）

### 改动

| 文件 | 说明 |
|---|---|
| `supervisor.py` | `_answer_to_user` 重建消息时把 `usage_metadata` 带上（之前只复制 content，把用量丢了） |
| `cli.py` | `_print_update` 顺带累加用量（复用已有的按 id 去重，避免重复计）；新增 `_usage_totals()` / `_print_usage()`；每轮打印一行，`:q` 时打印会话累计；`_drive` 返回本轮统计 |

### 实测

```
本轮：模型调用 5 次 · 输入 10,694 · 输出 1,459 · 合计 12,153 tokens
...
本会话累计：模型调用 5 次 · 输入 10,694 · 输出 1,459 · 合计 12,153 tokens
```

`pytest` → 105 passed, 1 skipped。

### 备注与坑

1. **只有最后一个流式 chunk 带 usage**（实测确认）→ 累加不会重复计数。
   排查时一度以为"累加把用量翻倍了"，实际是两次独立调用的 thinking 长度不同。
2. **`output_tokens` 包含 thinking 的 token** —— 所以"只回答了一句话却花掉上千输出 token"
   是正常的，thinking 越长数字越大。这点要在 README 里说明，免得用户以为算错了。
3. **"输入"是各次调用累加后的值（计费口径），不是上下文大小** ——
   一次 ReAct 循环里每调一次模型都要重发上下文，所以 5 次调用的输入合计
   会数倍于单次上下文。想表达"上下文多大"是另一回事。
4. **旧会话（本次改动之前存的）没有 `usage_metadata`** → 累计会偏小；
   新会话才准。没有去回填历史数据。

---

## 方案 B — 各成员不再共享工具草稿（2026-09-13）

### 目标

解决方案 A 的三个代价：上下文膨胀、**交叉污染**（实测 coder 照着 verifier 的
`run_command` 去调一个它没有的工具）、worker「照抄不动手」。

### 实现方式的选择（重要）

**没有**采用"新增 `findings`/`edits`/`verdict` 字段"，而是**剪掉工具草稿、只留人话**。

理由：新增字段需要 worker 主动调用一个"提交报告"的工具，而**模型可能忘记调用** →
字段为空 → 比不改更糟（静默失败，比现状还差）。剪枝是**自动的、不依赖模型配合**，
隔离效果相同：

```
剪前：任务 + [A 的指令 + A 的工具调用 + A 的工具结果 + A 的汇报] + [B 的指令 + B 的…]
剪后：任务 + A 的指令 + A 的汇报 + B 的指令 + B 的汇报 + …          ← 只剩人话
```

### 改动

| 文件 | 说明 |
|---|---|
| `state.py` | 新增 `made_edits` —— 草稿被剪后，"是否改过代码"就翻不到了 |
| `supervisor.py` | 新增 `prune_scratchpad()`：**成对**删除带 `tool_calls` 的 AIMessage 及其 ToolMessage；`_rule_based` 改用 `made_edits` |
| `cli.py` | payload 传 `made_edits`；`_final_answer` 改用「本轮之前的消息 id 集合」过滤 |

### 验证

**持久化状态实测**（同一个"跑测试、修 bug"任务）：

| | 方案 A | 方案 B |
|---|---|---|
| 消息总数 | 44 | **8** |
| 其中工具消息 | 24 | **0** |
| 消息文本总量 | ~14,600 字符 | ~6,700 字符 |

剩下的黑板内容正是设计目标：`用户任务` + 3 条 `[Supervisor 指令]` + 4 条汇报 + 1 条回答。
`made_edits: True` 也如实记录了。`pytest` → **108 passed, 1 skipped**。

### 备注与坑

1. **必须成对删除**：只删 ToolMessage 而留下带 `tool_calls` 的 AIMessage，
   下次发回 API 会因 tool_use ⇄ tool_result 不配对而报错。剪枝函数里两遍扫描就是为了这个。
2. **剪枝会让"是否改过代码"失效** → 必须补 `made_edits`，由 supervisor 在**剪之前**观察一次。
   这是"隔离"带来的直接副作用，不补的话兜底路由会永远派 coder。
3. **`_final_answer` 不能用下标**：剪枝会让消息列表变短、下标漂移 →
   会拿到上一轮的回答（正是之前踩过的"复读"）。改用 id 集合过滤本轮新消息。
4. **用量统计被剪枝破坏**（这次实测出来的）：原先的累计是"回头读库求和"，
   而带用量的消息已被剪掉 → 累计比单轮之和还小。
   → 改为**进程内累加**，并如实标注"**本次运行累计**"（不包含本会话此前运行的部分）。
5. **路由调用的用量会漏计**：supervisor 的路由调用结果不进 state（只落一条指令），
   CLI 在状态里看不到它 → 每轮少算一次调用。改为通过 custom 流主动上报 `{"type": "usage"}`。
6. 这一版把「方案 A / B」的取舍真正落地了；方案 A 的代码路径（共享历史做决策）只保留在
   `_digest` 的摘要里 —— supervisor 决策仍基于摘要，但摘要的来源已变成"只有人话"的历史。

---

## 新增 — `--history` 查看会话记录（2026-09-13）

### 背景

用户问"聊天记录存在哪里，我去看看"。先说结论：**直接看数据库不可行** ——
消息存在 `checkpoint_blobs.blob` 里，类型是 **msgpack 二进制**，SQL 查出来是乱码，
必须反序列化才能读。

存储位置（本机 PostgreSQL `langgraph_db`，连接串在 `.env` 的 `AGENT_PG_DSN`）：

| 表 | 存什么 |
|---|---|
| `checkpoints` | 每前进一步一个快照的元信息（jsonb，几百字节） |
| `checkpoint_blobs` | **消息正文**（msgpack 二进制） |
| `checkpoint_writes` | 节点中间写入（也是 blob） |

### 改动

| 文件 | 说明 |
|---|---|
| `cli.py` | 新增 `--history`：不带 `--thread-id` 列出会话；带上则打印**完整记录** |
| `cli.py` | 新增 `merge_history()`：把多个历史快照合并成完整消息序列（按 id 去重），并标出"已不在上下文里"的 |
| `cli.py` | invoke 时带 `metadata={"repo": ...}`，以后可**按仓库**筛会话（老会话没标记） |
| `tests/test_cli_history.py` | 新增 2 例 |

### 验证

```bash
xx-code --history                                  # 列会话
xx-code --history --thread-id eee627c4…            # 打印完整记录
```

```
9 个快照，合并后 33 条消息；标 ·已剪 的表示当前不在模型上下文里（记录本身仍在库里）
HumanMessage  运行测试，把失败的修好
AIMessage     ·已剪 I'll start by exploring the repository structure…
             → 调用 ['list_dir', 'glob_search']
ToolMessage   ·已剪 calc.py (144 B) test_calc.py (122 B)
ToolMessage   ·已剪 # calc.py (共 8 行) 1  def add(a, b): …      ← 连文件内容都在
```

**最新状态只有 8 条，从历史里合并出 33 条** —— 被剪掉的 24 条全部可读。
`pytest` → 110 passed, 1 skipped。

### 备注与坑

1. **剪枝不销毁记录**：LangGraph 每前进一步存一个快照（`checkpoints` 是 append-only），
   被剪的消息仍留在**更早的快照**里。所以"不喂给模型" ≠ "删掉记录"。
   —— 之前跟用户解释时我只说了"删掉"，措辞不准确，容易让人以为记录被销毁。
2. 旧会话没记 repo 元信息 → 列表里归为"其它仓库/未标记"，用 `--thread-id` 仍可查看。

---

## v1 收尾状态（M0–M4 全部完成）

计划中的 5 个里程碑已全部落地，`xx-code` 现在能：

```bash
python -m code_agent.cli --repo <仓库> "运行测试，把失败的修好"
```

→ Supervisor 自动调度 Explorer（定位）/ Coder（改码）/ Verifier（跑测试核验），
高风险命令暂停等确认，会话由 PostgreSQL 持久化，可跨进程续接。

### 已知未完成项（诚实清单，按优先级）

1. **编排仍是方案 A（共享 messages）**。worker 偶尔会"照抄不动手"（M3 坑 7 已用指令注入
   缓解，但没根治），上下文也会随任务变大而膨胀。→ 升级到方案 B（各 worker 独立
   `findings`/`edits`/`verdict` 通道）是**下一步最大的收益点**。
   升级时务必注意：**父子两侧 `state_schema` 必须同时声明通道**，否则静默丢弃。
2. ~~图与调度的逻辑没有自动化测试~~ → **已补**（见上节「补测」）：supervisor 路由、
   图接线、HITL 暂停-恢复现在都有断言覆盖，总计 90 个测试。剩余未覆盖的是
   `run_command` 的超时/进程树清理、以及真实模型的端到端行为（后者靠手工验证）。
3. **`attempts` 上限（8）与 `recursion_limit`（250）是硬编码**，没有按任务规模自适应。
4. ~~PG 连接失败没有友好报错~~ → **已修**：CLI 加了 `_check_pg()` 预检，`main()` 统一捕获
   `RuntimeError` 并打印人话提示（同时覆盖"缺少 `AGENT_PG_DSN`"）。原先会甩出一长串 psycopg
   堆栈，其中 PG 返回的中文报错还会因控制台编码变成乱码。
5. **只支持 Windows + 中文环境验证过**（cp936、`taskkill`、`cmd.exe` 等假设）。
6. **没有 `--resume` 之类的续接入口**：`--thread-id` 能续，但需要手动记住 ID。

> 设计文档（含完整架构、工具清单、编排方式、压缩策略）保存在
> `C:\Users\x_x\.claude\plans\crispy-forging-patterson.md`
