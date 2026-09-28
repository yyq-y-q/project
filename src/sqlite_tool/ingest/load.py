"""
阶段 5 — 归库 load

职责：ValidatedTable → SQLite 表（可写）
默认策略：全量替换同名表（教学清晰；生产再换 upsert/迁移）

注意：运行时查询仍走 executor 只读连接；只有摄取进程写库。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlite_tool.config import SQLITE_DIR, Config

from .types import LoadResult, ValidatedTable

try:
    from app.locks import sqlite_query_write
except ImportError:

    @contextmanager
    def sqlite_query_write(*, timeout: float | None = 120.0) -> Iterator[None]:
        yield


_TYPE_DDL = {
    "INTEGER": "INTEGER",
    "REAL": "REAL",
    "TEXT": "TEXT",
}


def load_sqlite(
    validated: ValidatedTable,
    db_path: str | Path | None = None,
    replace: bool = True,
) -> LoadResult:
    if not validated.rows and not validated.fields:
        raise ValueError("无字段可建表")

    SQLITE_DIR.mkdir(parents=True, exist_ok=True)
    target = Path(db_path or Config.QUERY_PATH)
    table = validated.table_name
    fields = validated.fields

    # 表名/列名已在 extract 收成标识符；再防一层
    if not table.isidentifier():
        raise ValueError(f"非法表名: {table!r}")
    for f in fields:
        if not f.name.isidentifier():
            raise ValueError(f"非法列名: {f.name!r}")

    col_defs = []
    for f in fields:
        ddl_t = _TYPE_DDL[f.sql_type]
        piece = f'"{f.name}" {ddl_t}'
        if f.name.lower() == "id":
            piece += " PRIMARY KEY"
        elif f.required:
            piece += " NOT NULL"
        col_defs.append(piece)

    col_ddl = ", ".join(col_defs)
    col_list = ", ".join(f'"{f.name}"' for f in fields)
    placeholders = ", ".join("?" for _ in fields)

    # 跨进程写锁：多路 ingest / 多 worker 不并写 query.db
    with sqlite_query_write():
        con = sqlite3.connect(str(target))
        try:
            if replace:  # 旧表结构不要了（列变了也能重建）
                con.execute(f'DROP TABLE IF EXISTS "{table}"')
            con.execute(f'CREATE TABLE IF NOT EXISTS "{table}" ({col_ddl})')
            if replace:
                con.execute(f'DELETE FROM "{table}"')

            rows_as_tuples = [
                tuple(row.get(f.name) for f in fields) for row in validated.rows
            ]
            if rows_as_tuples:
                con.executemany(
                    f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})',
                    rows_as_tuples,
                )
            con.commit()
            n = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        finally:
            con.close()

    return LoadResult(
        db_path=target.resolve(),
        table_name=table,
        rowcount=int(n),
        columns=[f.name for f in fields],
    )
