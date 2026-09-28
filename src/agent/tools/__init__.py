from .file_tool import make_file_tools
from .terminal_tool import make_terminal_tool
from .sqlite_query_tool import sqlite_Query
from .rag_tool import rag_Query

# 兼容：无沙箱时的旧符号（main 应改用 make_file_tools）
def file_Read(file_path: str) -> str:
    """已弃用：请通过 make_file_tools(work_path) 创建。"""
    return f"请使用绑定工作目录的 file_Read；收到路径 {file_path}"


def file_Write(file_path: str, content: str) -> str:
    return f"请使用绑定工作目录的 file_Write；收到路径 {file_path}"


def run_Terminal_Command(command: str) -> str:
    return f"请使用绑定工作目录的 run_Terminal_Command；收到命令 {command}"


__all__ = [
    "make_file_tools",
    "make_terminal_tool",
    "file_Read",
    "file_Write",
    "run_Terminal_Command",
    "sqlite_Query",
    "rag_Query",
]
