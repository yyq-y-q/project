"""
文件路径沙箱：所有 file/terminal 操作限制在 work_root 下。
"""
from __future__ import annotations

import os
from pathlib import Path


class PathSandbox:
    def __init__(self, work_root: str | Path):
        self.root = Path(work_root).resolve()
        if not self.root.is_dir():
            raise ValueError(f"工作目录不存在: {self.root}")

    def resolve(self, path: str | Path) -> Path:
        """解析并校验路径必须落在 root 内。"""
        p = Path(path)
        if not p.is_absolute():
            p = self.root / p
        resolved = p.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as e:
            raise PermissionError(
                f"路径越界：{resolved} 不在工作目录 {self.root} 内"
            ) from e
        return resolved

    def ensure_parent(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
