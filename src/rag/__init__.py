"""
rag 包门面：对外导出常用入口。

说明：
- 包内多数文件仍是裸 import（from config import ...）；
  门面在导入前把本目录放入 sys.path，兼容旧写法。
"""
from __future__ import annotations

import sys
from pathlib import Path

_RAG_DIR = Path(__file__).resolve().parent
if str(_RAG_DIR) not in sys.path:
    sys.path.insert(0, str(_RAG_DIR))

from .RAG import RAGPipeline  # noqa: E402
from .loader import load_doc_paths, load_docs_dir  # noqa: E402

__all__ = [
    "RAGPipeline",
    "load_docs_dir",
    "load_doc_paths",
]
