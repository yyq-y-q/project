"""
阶段 3 — 清洗 clean

职责：
- 丢掉整行空
- 字符串 strip；空串 → None
- 按 FieldSpec.sql_type 做温和类型转换（失败先原样留下，交给 validate 报）
- 键改为标准字段名

不做：判定「业务是否合法」（那是 validate）
"""
from __future__ import annotations

import re
from typing import Any

from .extract import row_values_by_field
from .types import CleanedTable, ExtractedTable, FieldSpec, SqlType


def _to_none_if_blank(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return None if s == "" else s
    return v


def _coerce(v: Any, sql_type: SqlType) -> Any:
    """尽力转类型；转不动则原样返回（validate 再杀）。"""
    v = _to_none_if_blank(v)
    if v is None:
        return None

    if sql_type == "TEXT":
        return str(v).strip() if not isinstance(v, str) else v.strip()

    if sql_type == "INTEGER":
        if isinstance(v, bool):
            return v
        if isinstance(v, int):
            return v
        if isinstance(v, float) and v == int(v):
            return int(v)
        s = str(v).strip().replace(",", "").replace("¥", "").replace("$", "")
        # 22000.0 / 22 000
        s = re.sub(r"\s+", "", s)
        if re.fullmatch(r"[+-]?\d+", s):
            return int(s)
        if re.fullmatch(r"[+-]?\d+\.0+", s):
            return int(float(s))
        return v  # 留给 validate

    if sql_type == "REAL":
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
        s = str(v).strip().replace(",", "").replace("%", "")
        s = re.sub(r"\s+", "", s)
        try:
            return float(s)
        except ValueError:
            return v

    return v


def clean_table(extracted: ExtractedTable) -> CleanedTable:
    fields = extracted.fields
    kept: list[dict[str, Any]] = []
    dropped_empty = 0

    for row in extracted.rows: #待洗的行
        raw_map = row_values_by_field(row, fields)
        # 标准化
        cleaned = {f.name: _coerce(raw_map.get(f.name), f.sql_type) for f in fields}

        if all(v is None for v in cleaned.values()):
            dropped_empty += 1
            continue
        kept.append(cleaned)

    return CleanedTable(
        source=extracted.source,
        table_name=extracted.table_name,
        fields=fields,
        rows=kept,
        stats={
            "input_rows": len(extracted.rows),
            "dropped_empty": dropped_empty,
            "output_rows": len(kept),
        },
    )
