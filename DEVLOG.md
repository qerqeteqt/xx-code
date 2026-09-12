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

# 工具演示：写入 → 读取 → 精确替换 → 列目录 → glob → grep → 越界拦截
# 全程在临时目录，不会修改任何真实文件
"C:\Users\x_x\.conda\envs\langgraph\python.exe" -m code_agent.cli --demo-tools
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

## 下一步

**M3 — Supervisor 调度 + 危险命令 HITL**

- `code_agent/state.py`：`OverallState`（`MessagesState` + `attempts`）
- `code_agent/tools/command.py`：`run_command`（危险命令匹配 + 工具内 `interrupt()`）
- `code_agent/supervisor.py`：结构化输出路由（含代理不支持强制 tool_choice 时的降级链）
- `code_agent/graph.py`：父 `StateGraph` + 子图接线 + `PostgresSaver` + `thread_id`
- CLI：`--repo` + 任务 → 跑整图；捕获 `__interrupt__` 交互确认

> 设计文档（含完整架构、工具清单、编排方式、压缩策略）保存在
> `C:\Users\x_x\.claude\plans\crispy-forging-patterson.md`
