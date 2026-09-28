"""
Agent → sqlite_tool 适配层

职责：把「只读 SQL」收成 ReAct 可调用的函数。
执行前按调用者身份做表级 ACL 校验，再委托 sqlite_tool.query。
"""
from __future__ import annotations

import json


def _sql_tables(sql: str) -> set[str]:
    """只解析表名，不执行 SQL；用于执行前 ACL 校验。"""
    import sqlglot
    from sqlglot import exp

    tree = sqlglot.parse_one(sql, read="sqlite")
    return {table.name for table in tree.find_all(exp.Table) if table.name}


def sqlite_Query(sql: str, principal=None) -> str:
    """
    对业务库 query.db 执行只读 SQL 查询（受表级 ACL 约束）。

    参数:
        sql: 单条 SELECT（禁止写库/多语句；系统会自动加 LIMIT）
        principal: 调用者身份（可选）；传入时按人校验可读表

    返回:
        JSON 字符串：成功为行列表；失败为 {"error": "..."}
    """
    try:
        from app.acl import table_access_allowed

        tables = _sql_tables(sql or "")
        if not table_access_allowed(tables, principal):
            return json.dumps(
                {
                    "error": (
                        "当前身份无权读取 SQL 引用的表: "
                        + ", ".join(sorted(tables))
                    )
                },
                ensure_ascii=False,
            )
    except ValueError as e:
        return json.dumps({"error": f"SQL 无法解析，未执行查询: {e}"}, ensure_ascii=False)

    from sqlite_tool import query as _sqlite_query

    result = _sqlite_query(sql)
    return json.dumps(result, ensure_ascii=False, default=str)
