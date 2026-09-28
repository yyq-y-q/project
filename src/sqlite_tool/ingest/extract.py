"""
阶段 2 — 字段提取 extract

职责：
- 脏表头 → 标准字段名（可入库的标识符）
- 猜/定 sql 类型（第一版：按列名启发式 + 采样）
- 定表名（默认用文件名 stem）
- 行变成 dict（键 = 原始表头，方便对照）

不做：strip 单元格、丢空行、写库
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .types import ExtractedTable, FieldSpec, RawTable, SqlType

# 列名里出现这些词 → 倾向整数
_INT_HINTS = ("id", "year", "age", "count", "qty", "num", "salary", "price", "code")
_REAL_HINTS = ("rate", "ratio", "score", "float", "avg", "pct", "percent")

# 别名：常见中文/脏表头 → 稳定英文列名（字段提取的核心价值之一）
_HEADER_ALIASES: dict[str, str] = {
    "id": "id",
    "ID": "id",
    "编号": "id",
    "姓名": "name",
    "名字": "name",
    "名称": "name",
    "部门": "dept",
    "职位": "title",
    "岗位": "title",
    "城市": "city",
    "薪资": "salary",
    "工资": "salary",
    "入职年": "hire_year",
    "年份": "year",
}


def _alias_key(header: str) -> str:
    return header.strip()


def _slug_field(header: str, used: set[str]) -> str:
    """
    字段提取：
    1) 先查别名表（中文表头 → 英文标准名）
    2) 否则 slug：空白/特殊字符 → _
    3) 重名加 _2 _3
    """
    raw = header.strip()
    if raw in _HEADER_ALIASES:
        h = _HEADER_ALIASES[raw]
    else:
        # 再试：去空格后的中文键
        compact = re.sub(r"\s+", "", raw)
        if compact in _HEADER_ALIASES:
            h = _HEADER_ALIASES[compact]
        else:#自动清洗生成合法标识符
            ## 如果compact是空字符串，直接赋值为"col"
            h = compact if compact else "col"
            ## 所有空白（空格、tab、换行）替换成下划线_
            h = re.sub(r"\s+", "_", h)
            ## 符号如 # @ - 全部变成 _
            h = re.sub(r"[^\w]", "_", h, flags=re.UNICODE)
            h = re.sub(r"_+", "_", h).strip("_") or "col"
            # 多个连续下划线合并成单个下划线；
            # 去掉字符串首尾下划线；
            # 如果处理完变成空字符串，兜底为 "col" 否则 h 就是合法标识符
            if h[0].isdigit():
                #处理【数字开头】的情况
                h = f"f_{h}"
            if not h.isidentifier():
                #判断这个字符串能不能当作合法 Python 变量名
                # 仍非标识符（少见）→ 占位
                h = f"col_{len(used)+1}"

    base = h
    n = 2
    #第一个：user，加入 used；第二个冲突 → user_2
    while h in used:
        h = f"{base}_{n}"
        n += 1
    used.add(h)
    return h


def _guess_type(name: str, source_header: str, values: list[Any]) -> SqlType:
    key = f"{name} {source_header}".lower()
    if any(h in key for h in _REAL_HINTS):
        return "REAL"
    if any(h in key for h in _INT_HINTS):
        return "INTEGER"

    # 采样非空值：按「能解析成数」的多数派定类型（脏单元格留给 validate 丢行）
    samples = [v for v in values if v is not None and str(v).strip() != ""][:40]
    if not samples:
        return "TEXT"

    def as_number(v: Any):
        if isinstance(v, bool):
            return None
        if isinstance(v, int):
            return v
        if isinstance(v, float):
            return v
        s = str(v).strip().replace(",", "").replace("¥", "").replace("$", "")
        s = re.sub(r"\s+", "", s)
        if re.fullmatch(r"[+-]?\d+", s):
            return int(s)
        if re.fullmatch(r"[+-]?\d+\.\d+", s):
            return float(s)
        return None

    nums = [as_number(v) for v in samples]
    parsed = [n for n in nums if n is not None]
    # 至少一半能当数字，才认数值列（避免一列纯文本被误判）
    if len(parsed) < max(1, len(samples) // 2):
        return "TEXT"
    if all(isinstance(n, int) or (isinstance(n, float) and n == int(n)) for n in parsed):
        return "INTEGER"
    return "REAL"


def _default_required(name: str) -> bool:
    # 主键味道的列：必填
    return name.lower() in {"id", "pk"} or name.lower().endswith("_id")


def extract_fields(
    raw: RawTable,
    table_name: str | None = None,
) -> ExtractedTable:
    used: set[str] = set()
    fields: list[FieldSpec] = []#列定义

    # 按列收集值，供类型猜测
    ncols = len(raw.headers)
    col_values: list[list[Any]] = [[] for _ in range(ncols)]
    for row in raw.rows:
        for i, v in enumerate(row):
            if i < ncols:
                col_values[i].append(v)

    for i, header in enumerate(raw.headers):
        # 空表头列：占位名
        src = header if header else f"空白列{i+1}"
        name = _slug_field(src if header else f"col_{i+1}", used)
        sql_type = _guess_type(name, src, col_values[i])
        fields.append(
            FieldSpec(
                name=name,
                source_header=header,  # 保留真·原表头（可能为空串）
                sql_type=sql_type,
                required=_default_required(name),
            )
        )

    dict_rows: list[dict[str, Any]] = []
    for row in raw.rows:
        # 键用「位置稳定」的 source：空表头用 fields 里 name 回退不对；
        # 统一用 enumerate 对齐：键 = 原始 headers[i]（可空），后面 clean 用 fields 顺序取
        item: dict[str, Any] = {}
        for i, header in enumerate(raw.headers):
            # 用「列下标命名空间」避免空表头/重名表头撞键
            key = f"__col_{i}__"
            item[key] = row[i] if i < len(row) else None
            item[f"__header_{i}__"] = header
        dict_rows.append(item)
#表名
    tname = table_name or _slug_field(raw.source.stem, set())
    return ExtractedTable(
        source=raw.source,
        table_name=tname,
        fields=fields,
        rows=dict_rows,
    )


def row_values_by_field(row: dict[str, Any], fields: list[FieldSpec]) -> dict[str, Any]:
    """给clean做：把 extract 行（__col_i__）映成 {标准字段名: 值}。"""
    out: dict[str, Any] = {}
    for i, f in enumerate(fields):
        out[f.name] = row.get(f"__col_{i}__")
    return out
