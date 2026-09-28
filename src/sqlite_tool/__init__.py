"""
sqlite_tool 包门面：对外稳定入口。

- query: 只读 SQL 网关
- run_ingest / ingest_to_validated: 结构化摄取（再导出自 ingest 子包）
"""
from __future__ import annotations

from .ingest import ingest_dir, ingest_to_validated, run_ingest
from .query import query

__all__ = [
    "query",
    "run_ingest",
    "ingest_to_validated",
    "ingest_dir",
]
