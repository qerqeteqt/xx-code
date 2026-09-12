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

## 下一步

**M1 — 文件与搜索工具（不需要模型即可测试）**

- `code_agent/paths.py`：`RepoRoot` 路径围栏（`resolve()` + 校验是否在根内，防目录穿越）
- `code_agent/tools/filesystem.py`：`read_file` / `write_file` / `edit_file` / `list_dir`
- `code_agent/tools/search.py`：复用 `FilesystemFileSearchMiddleware`（`use_ripgrep=False`）
- `tests/`：路径围栏与工具的单测，`pytest` 验证

> 设计文档（含完整架构、工具清单、编排方式、压缩策略）保存在
> `C:\Users\x_x\.claude\plans\crispy-forging-patterson.md`
