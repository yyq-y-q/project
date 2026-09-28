"""带工作目录 + 超时 + 简易黑名单的终端工具。"""
from __future__ import annotations

import re
import subprocess

from .sandbox import PathSandbox

# 明显危险；生产可再收紧
_BLOCK = re.compile(
    r"(rm\s+-rf\s+/)|(format\s+c:)|(mkfs\.)|"
    r"(curl\s+[^\n]*\$\{)|(\bshutdown\b)|(\breboot\b)",
    re.IGNORECASE,
)


def make_terminal_tool(work_root: str, timeout_s: int = 60):
    box = PathSandbox(work_root)

    def run_Terminal_Command(command: str) -> str:
        """
        在工作目录下执行 shell 命令（有超时）。
        禁止明显危险命令；stdout/stderr 截断返回。
        """
        cmd = (command or "").strip()
        if not cmd:
            return "空命令"
        if _BLOCK.search(cmd):
            return f"命令被安全策略拒绝: {cmd}"

        try:
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                cwd=str(box.root),
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            return f"命令超时（>{timeout_s}s）: {cmd}"
        except Exception as e:
            return f"执行异常: {e}"

        out = (result.stdout or "")[-8000:]
        err = (result.stderr or "")[-4000:]
        if result.returncode == 0:
            return out or "执行成功"
        return f"exit={result.returncode}\nstdout:\n{out}\nstderr:\n{err}"

    run_Terminal_Command.__name__ = "run_Terminal_Command"
    return run_Terminal_Command
