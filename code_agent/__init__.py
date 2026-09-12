"""code_agent —— 基于 LangGraph 的多 Agent 编码助手。

结构（随里程碑逐步落地）：
    config.py   配置加载与模型构建
    paths.py    目标仓库路径围栏
    state.py    共享 State 定义
    tools/      文件 / 搜索 / 命令 / 联网工具
    workers.py  Explorer / Coder / Verifier 三个 worker
    supervisor.py  中心调度
    graph.py    组装 StateGraph
    cli.py      命令行入口
"""

__version__ = "0.0.1"
