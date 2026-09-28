"""
阶段 4 — 校验 validate

职责：
- required 字段非空
- 值类型与 FieldSpec.sql_type 一致
- （可选）主键唯一：名为 id 的字段
- 坏行默认丢弃，issues 里留下原因（不静默）

不做：改值（改值是 clean 的事）
"""
from __future__ import annotations

from typing import Any

from .types import CleanedTable, FieldSpec, RowIssue, SqlType, ValidatedTable


def _type_ok(v: Any, sql_type: SqlType) -> bool:
    if v is None:
        return True
    if sql_type == "TEXT":
        return isinstance(v, str)
    if sql_type == "INTEGER":
        #（Python 里 True 是 int 子类）
        return isinstance(v, int) and not isinstance(v, bool)
    if sql_type == "REAL":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    return False


def validate_table(cleaned: CleanedTable) -> ValidatedTable:
    fields = cleaned.fields # 列说明书
    ok_rows: list[dict[str, Any]] = [] # 合格行
    issues: list[RowIssue] = [] # 所有问题明细

    # 主键唯一（仅当存在 name==id） 找到第一个符合条件的就停了
    id_field = next((f for f in fields if f.name.lower() == "id"), None)
    seen_ids: set[Any] = set()

    for idx, row in enumerate(cleaned.rows):
        row_errors: list[RowIssue] = []

        for f in fields:
            val = row.get(f.name)

            if f.required and val is None:
                row_errors.append(
                    RowIssue(idx, f.name, "必填为空", val)
                )
                continue

            if not _type_ok(val, f.sql_type):
                row_errors.append(
                    RowIssue(
                        idx,
                        f.name,
                        f"类型期望 {f.sql_type}，实际 {type(val).__name__}",
                        val,
                    )
                )

        if id_field is not None:
            #如果取到了主键
            iv = row.get(id_field.name)
            if iv is not None:
                if iv in seen_ids:
                    row_errors.append(
                        RowIssue(idx, id_field.name, "主键重复", iv)
                    )
                else:
                    seen_ids.add(iv)

        if row_errors:
            issues.extend(row_errors)
            continue
        ok_rows.append(row)

    return ValidatedTable(
        source=cleaned.source,
        table_name=cleaned.table_name,
        fields=fields,
        rows=ok_rows,
        issues=issues,
        stats={
            **cleaned.stats,
            "invalid_rows": len(cleaned.rows) - len(ok_rows),
            "valid_rows": len(ok_rows),
            "issue_count": len(issues),
        },
    )
