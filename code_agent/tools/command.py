"""run_command：在仓库根目录执行 shell 命令；危险命令需人工确认。

**已知限制（重要）**：危险命令的判定只做**正则模式匹配**，这不是沙箱。
未被模式命中、但实际有破坏性的命令会直接执行；且命令对文件系统的访问范围
不受仓库限制。要真正隔离需要容器/虚拟机。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from langchain_core.tools import tool
from langgraph.types import interrupt

from code_agent.paths import RepoRoot
from code_agent.tools._util import safe

DEFAULT_TIMEOUT = 120
MAX_OUTPUT_CHARS = 6000

# (正则, 原因, 豁免正则) —— 命中且不满足豁免时，暂停并请求用户确认
_RULES: tuple[tuple[str, str, str | None], ...] = (
    (r"\brm\s+(-[a-z]+\s+)*-[a-z]*r", "递归删除文件", None),
    (r"\brm\s+-[a-z]*f", "强制删除文件", None),
    (r"\b(del|erase)\b.*\s/[a-z]*[fsq]", "强制/递归删除文件", None),
    (r"\b(rmdir|rd)\b.*\s/[a-z]*[sq]", "递归删除目录", None),
    (r"\bRemove-Item\b.*-(Recurse|Force)", "递归/强制删除", None),
    (r"\bformat\b\s+[a-z]:", "格式化磁盘", None),
    (r"\bgit\s+push\b", "推送到远端仓库", None),
    (r"\bgit\s+reset\b.*--hard", "硬重置，丢弃本地改动", None),
    # git clean 的 dry-run（-n / --dry-run）不改动任何文件，不算危险
    (r"\bgit\s+clean\b", "清理未跟踪文件",
     r"\bgit\s+clean\b[^\n]*?(?:--dry-run|(?:^|\s)-[a-z]*n[a-z]*(?=\s|$))"),
    (r"\bgit\s+(checkout|restore)\b.*(--\s|\.)", "丢弃工作区改动", None),
    (r"\b(shutdown|reboot)\b", "关机/重启", None),
    (r"\b(taskkill|Stop-Process)\b", "结束进程", None),
    (r"\b(mkfs|dd)\b\s", "磁盘写入操作", None),
    (r"\b(chmod|chown)\b.*-R", "递归修改权限/属主", None),
    (r"\bpip\s+uninstall\b", "卸载软件包", None),
    (r"\bnpm\s+(unpublish|publish)\b", "发布/撤回软件包", None),
    (r"curl[^|]*\|\s*(ba)?sh", "从网络下载并直接执行脚本", None),
    (r"\btruncate\b.*-s\s*0", "清空文件", None),
)

_COMPILED = tuple(
    (re.compile(pattern, re.IGNORECASE), why,
     re.compile(exempt, re.IGNORECASE) if exempt else None)
    for pattern, why, exempt in _RULES
)

_APPROVE = frozenset({"approve", "y", "yes", "true"})


def dangerous_reason(command: str) -> str | None:
    """返回命中的危险原因；不是危险命令（或命中豁免）则返回 None。"""
    for pattern, why, exempt in _COMPILED:
        if pattern.search(command) and not (exempt and exempt.search(command)):
            return why
    return None


def _kill_tree(process: subprocess.Popen) -> None:
    """终止进程及其子进程。

    Windows 上 subprocess 的 start_new_session 是无效参数，超时只能杀掉直接子进程，
    因此必须用 taskkill /T 把整棵进程树带走。
    """
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
            )
        else:
            process.kill()
    except Exception:  # noqa: BLE001 - 清理失败不应掩盖超时本身
        pass


def _execute(root: RepoRoot, command: str, timeout: int) -> str:
    # 让子进程优先使用当前解释器（即 conda env `langgraph`），
    # 这样 `python` / `pytest` 会解析到装好依赖的那个环境。
    env = os.environ.copy()
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")

    process = subprocess.Popen(
        command,
        shell=True,
        cwd=str(root),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(process)
        process.communicate()
        return f"Error: 命令超时（>{timeout}s）已被终止: {command}"

    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS] + f"\n... 输出过长已截断（共 {len(output)} 字符）"
    return f"exit_code={process.returncode}\n{output.strip()}"


def build_command_tools(root: RepoRoot) -> list:
    """构建命令工具集（只有 Verifier 会拿到）。"""

    @tool
    @safe
    def run_command(command: str, timeout: int = DEFAULT_TIMEOUT) -> str:
        """在目标仓库根目录执行一条 shell 命令，返回退出码与输出。

        用于运行测试、静态检查、查看文件等。命令在系统 shell 中执行
        （Windows 上是 cmd.exe），工作目录为仓库根目录。

        危险命令（删除、git push、重置、关机等）会暂停并请求用户确认，
        用户拒绝时会返回说明。请优先使用非破坏性的命令。

        Args:
            command: 要执行的命令，如 "python -m pytest -q"。
            timeout: 超时秒数，默认 120。
        """
        reason = dangerous_reason(command)
        if reason is not None:
            decision = interrupt({"command": command, "reason": reason, "cwd": str(root)})
            if str(decision).strip().lower() not in _APPROVE:
                return f"用户拒绝了该命令（{reason}）。请改用非破坏性的做法。"
        return _execute(root, command, int(timeout))

    return [run_command]
