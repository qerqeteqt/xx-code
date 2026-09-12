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
   → `RepoRoot.resolve()` 现在把**无盘符的前导斜杠**按"仓库根相对"解释；自研搜索直接返回干净
   的 POSIX 相对路径，两边都对齐了。
3. `safe` 装饰器是硬性要求：`create_agent` 默认只吞参数校验错误，工具体里的其他异常会
   **冒泡崩掉整个 run**。所以每个工具都必须保证不抛异常。
4. `RepoRoot` 的安全性依赖 `Path.resolve()` 会展开 `..` 并跟随符号链接，因此指向仓库外的
   符号链接也会被正确拒绝。

---

## 下一步

**M2 — 三个 worker 子图**

- `code_agent/state.py`：`OverallState`（`MessagesState` + `attempts`）
- `code_agent/workers.py`：用 `create_agent` 构建 Explorer / Coder / Verifier
  - 各自独立 `system_prompt` + **裁剪过的工具集**（注意参数是 `system_prompt=`，不是 `prompt=`）
  - **子图不装 checkpointer**（只有根图装）
- 中间件：`ContextEditingMiddleware`（清旧工具结果）、`ToolRetryMiddleware(on_failure="continue")`、`ToolCallLimitMiddleware`
- 验证：单个 worker 能独立跑通一轮"读文件 → 回答"的工具调用

> 设计文档（含完整架构、工具清单、编排方式、压缩策略）保存在
> `C:\Users\x_x\.claude\plans\crispy-forging-patterson.md`
