"""带工作目录沙箱的文件工具工厂。"""
from __future__ import annotations

from .sandbox import PathSandbox


def make_file_tools(work_root: str):
    box = PathSandbox(work_root)

    def file_Read(file_path: str) -> str:
        """
        读取工作目录内文件内容（utf-8）。
        路径必须在 Agent 工作目录下。
        """
        path = box.resolve(file_path)
        if not path.is_file():
            return f"文件不存在: {path}"
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                return path.read_text(encoding="gbk")
            except Exception as e:
                return f"读取失败: {e}"
        except Exception as e:
            return f"读取失败: {e}"

    def file_Write(file_path: str, content: str) -> str:
        """
        写入工作目录内文件。
        路径必须在 Agent 工作目录下；自动创建父目录。
        """
        path = box.resolve(file_path)
        box.ensure_parent(path)
        text = content.replace("\\n", "\n")
        path.write_text(text, encoding="utf-8")
        return f"写入成功: {path} ({len(text)} chars)"

    file_Read.__name__ = "file_Read"
    file_Write.__name__ = "file_Write"
    return file_Read, file_Write
