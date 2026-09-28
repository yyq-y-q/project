"""
结构化摄取线路：各阶段之间传递的「信封」。

原则：
- 用明确的数据结构串阶段，而不是到处传裸 list
- 每阶段只吃上一阶段的输出，方便单测和教学对照
- 每个类调用后输入输出的内容
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

# SQLite 侧我们认的三种类型（第一版够用）
SqlType = Literal["INTEGER", "REAL", "TEXT"]


@dataclass
class RawTable:
    """parse 输出：还没洗过的行列。"""

    source: Path
    # 原始表头（可能含空格、中文、重复）
    headers: list[str]
    # 原始行；单元格保持 Excel 读出来的类型或 str
    rows: list[list[Any]]
    sheet: str = ""
    #表格名称


@dataclass
class FieldSpec:
    """extract 输出：一列的标准定义。"""

    # 入库用的合法列名
    name: str
    # 原始表头（追溯用）
    source_header: str
    sql_type: SqlType = "TEXT"
    # 是否允许 NULL；id/主键类通常 False
    required: bool = False


@dataclass
class ExtractedTable:
    """extract 输出：字段清单 + 按「源表头」对齐的行。"""

    source: Path
    table_name: str
    fields: list[FieldSpec]
    # 每行：{ source_header: value }  — 键仍是原表头，load 前再映到 name
    rows: list[dict[str, Any]]


@dataclass
class CleanedTable:
    """clean 输出：值已规范化，键已是标准字段名。"""

    source: Path
    table_name: str
    fields: list[FieldSpec]
    rows: list[dict[str, Any]]
    # 被丢掉的空行数等（教学/观测用）
    stats: dict[str, int] = field(default_factory=dict)
    #清洗情况统计


@dataclass
class RowIssue:
    """某一行某个字段的问题。"""

    row_index: int  # 清洗后的 0-based 下标
    field: str
    message: str
    value: Any = None


@dataclass
class ValidatedTable:
    """validate 输出：可入库行 + 问题清单（坏行默认丢弃）。"""

    source: Path
    table_name: str
    fields: list[FieldSpec]
    rows: list[dict[str, Any]]
    issues: list[RowIssue] = field(default_factory=list)
    #问题列表（坏行默认丢弃）
    stats: dict[str, int] = field(default_factory=dict)
    #校验情况统计


@dataclass
class LoadResult:
    """load 输出：落库结果摘要。"""

    db_path: Path
    table_name: str
    rowcount: int
    columns: list[str]
