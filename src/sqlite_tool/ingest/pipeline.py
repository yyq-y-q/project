"""
结构化摄取流水线（编排层）

  原件 raw
    → parse    解析
    → extract  字段提取
    → clean    清洗
    → validate 校验
    → load     归库
    → query    只读查询（sqlite_tool.query，不在本包写 SQL 执行）

本模块只串联；各阶段逻辑在独立文件，避免再度揉成上帝脚本。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import logging

from sqlite_tool.config import Config

from .clean import clean_table
from .extract import extract_fields
from .load import load_sqlite
from .parse import parse_table
from .types import LoadResult, ValidatedTable
from .validate import validate_table

logger = logging.getLogger(__name__)

_TABLE_GLOBS = ("*.xlsx", "*.xlsm", "*.csv")


def run_ingest(
    path: Path | str,
    table_name: str | None = None,
    db_path: str | Path | None = None,
    replace: bool = True,
) -> dict[str, Any]:
    """
    跑完整摄取，返回各阶段摘要（便于教学打印）。
    支持 xlsx / xlsm / csv。
    """
    path = Path(path)

    raw = parse_table(path)
    extracted = extract_fields(raw, table_name=table_name)
    cleaned = clean_table(extracted)
    validated = validate_table(cleaned)
    loaded = load_sqlite(validated, db_path=db_path, replace=replace)

    return {
        "ok": True,
        "source": str(raw.source),
        "sheet": raw.sheet,
        "table": loaded.table_name,
        "fields": [
            {
                "name": f.name,
                "source_header": f.source_header,
                "sql_type": f.sql_type,
                "required": f.required,
            }
            for f in validated.fields
        ],
        "stats": validated.stats,
        "issues": [
            {
                "row_index": i.row_index,
                "field": i.field,
                "message": i.message,
                "value": repr(i.value),
            }
            for i in validated.issues[:20]
        ],
        "load": {
            "db_path": str(loaded.db_path),
            "rowcount": loaded.rowcount,
            "columns": loaded.columns,
        },
    }


def ingest_to_validated(path: Path | str, table_name: str | None = None) -> ValidatedTable:
    """只到校验（不写库），方便单阶段调试。"""
    raw = parse_table(path)
    extracted = extract_fields(raw, table_name=table_name)
    cleaned = clean_table(extracted)
    return validate_table(cleaned)


def ingest_dir(
    directory: Path | str | None = None,
    db_path: str | Path | None = None,
    replace: bool = True,
) -> dict[str, Any]:
    """
    一键：扫描目录下 xlsx/csv，逐个 run_ingest。
    默认 Config.RAW_DIR（与 RAG 共用 raw，按后缀分流）。
    """
    root = Path(directory) if directory is not None else Path(Config.RAW_DIR)
    if not root.is_dir():
        return {"ok": False, "error": f"目录不存在: {root}", "results": []}

    files: list[Path] = []
    for g in _TABLE_GLOBS:
        files.extend(root.glob(g))
    files = sorted({p.resolve() for p in files}, key=lambda p: p.name.lower())

    results: list[dict[str, Any]] = []
    for fp in files:
        try:
            summary = run_ingest(fp, db_path=db_path, replace=replace)
            results.append(summary)
            logger.info(
                "ingest ok %s → %s rows=%s",
                fp.name,
                summary.get("table"),
                (summary.get("load") or {}).get("rowcount"),
            )
        except Exception as e:
            logger.exception("ingest fail %s: %s", fp, e)
            results.append({"ok": False, "source": str(fp), "error": str(e)})

    ok_n = sum(1 for r in results if r.get("ok"))
    return {
        "ok": ok_n == len(results) and len(results) > 0,
        "directory": str(root.resolve()),
        "total": len(results),
        "success": ok_n,
        "failed": len(results) - ok_n,
        "results": results,
    }
