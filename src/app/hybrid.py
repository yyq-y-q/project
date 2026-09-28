"""
HybridService — RAG + SQLite 确定性融合层

与 Agent 混用的区别：
  Agent：模型自己决定调哪个工具（非确定、可多步）
  Hybrid：一个函数内固定「查表 + 检索 + 汇总生成」，可测、可 API 直出

入口：
  HybridService().ask(question, sql=可选)

  - 提供 sql：执行该只读 SQL + RAG(question) → LLM 汇总
  - 不提供 sql：根据库表 schema 让 LLM 生成 SELECT（或判定无需 SQL）
                + RAG → 汇总
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

from app.errors import (
    AppError,
    DatabaseNotReadyError,
    HybridError,
    IndexNotReadyError,
    LlmError,
    SqlQueryError,
    SqlValidationError,
    ValidationError,
    raise_from_sqlite_result,
)
from app.logging_setup import get_logger
from app.services import KnowledgeService, TableService, get_rag

logger = get_logger(__name__)

_SQL_FENCE = re.compile(
    r"```(?:sql)?\s*(.*?)```",
    re.IGNORECASE | re.DOTALL,
)


def _llm_client() -> OpenAI:
    from rag.config import Config as RagConfig

    load_dotenv(RagConfig.ENV_PATH)
    key = os.getenv(RagConfig.LLM_API_KEY_ENV)
    if not key:
        raise LlmError(
            f"未配置 {RagConfig.LLM_API_KEY_ENV}",
            stage="hybrid.llm",
            details={"env_path": str(RagConfig.ENV_PATH)},
        )
    return OpenAI(
        api_key=key.strip(),
        base_url=RagConfig.LLM_BASE_URL,
        timeout=60,
    )


def _chat(client: OpenAI, prompt: str, *, temperature: float = 0.2) -> str:
    from rag.config import Config as RagConfig

    try:
        resp = client.chat.completions.create(
            model=RagConfig.LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=2048,
        )
        content = resp.choices[0].message.content
        if not content:
            raise LlmError("模型返回空内容", stage="hybrid.llm")
        return content.strip()
    except LlmError:
        raise
    except Exception as e:
        raise LlmError("混合层调用大模型失败", stage="hybrid.llm", cause=e) from e


def _extract_sql(text: str) -> str | None:
    """从模型输出抽出 SELECT；NONE/无 → None。"""
    raw = (text or "").strip()
    if not raw:
        return None
    upper = raw.upper()
    if upper.startswith("NONE") or upper == "N/A":
        return None
    m = _SQL_FENCE.search(raw)
    if m:
        raw = m.group(1).strip()
    if "select" not in raw.lower():
        return None

    lines: list[str] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            if lines:
                break
            continue
        if s.startswith("--"):
            continue
        lines.append(s)
    sql = " ".join(lines).strip().rstrip(";")
    if not sql.lower().startswith("select"):
        idx = raw.lower().find("select")
        if idx < 0:
            return None
        sql = raw[idx:].split(";")[0].strip()
    return sql or None


class HybridService:
    """确定性混合问答：SQL 证据 + RAG 证据 → 一篇答案。"""

    def __init__(
        self,
        knowledge: KnowledgeService | None = None,
        tables: TableService | None = None,
    ):
        self.kb = knowledge or KnowledgeService()
        self.tables = tables or TableService()

    def schema_snapshot(
        self,
        max_tables: int = 8,
        principal: Any | None = None,
    ) -> dict[str, Any]:
        """给自动 SQL 用的轻量 schema，只暴露当前 Principal 可读的表。"""
        from app.acl import table_access_allowed
        from app.auth import local_admin_principal

        try:
            meta = self.tables.query(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name",
                principal=local_admin_principal("schema-reader"),
            )
        except AppError as e:
            if isinstance(e, (SqlQueryError, SqlValidationError)):
                raise DatabaseNotReadyError(
                    e.message or "无法读取表清单",
                    stage="hybrid.schema",
                    cause=e,
                    details=e.details,
                ) from e
            raise e.with_stage("hybrid.schema")

        if not meta.get("ok"):
            raise DatabaseNotReadyError(
                meta.get("error") or "无法读取表清单",
                stage="hybrid.schema",
                details=meta,
            )
        names = [
            r["name"]
            for r in (meta.get("rows") or [])
            if r.get("name")
            and table_access_allowed([r["name"]], principal)
        ]
        tables_info: list[dict[str, Any]] = []
        for name in names[:max_tables]:
            sample = self.tables.query(
                f'SELECT * FROM "{name}" LIMIT 1',
                principal=principal,
            )
            cols: list[str] = []
            if sample.get("ok") and sample.get("rows"):
                cols = list(sample["rows"][0].keys())
            tables_info.append({"table": name, "columns": cols})
        return {"tables": tables_info, "table_names": names}

    def _ensure_rag_ready(self) -> None:
        pipe = get_rag()
        if not pipe._is_built and not pipe.load_index():
            raise IndexNotReadyError(stage="hybrid.rag")

    def _run_sql(self, sql: str, principal: Any | None = None) -> dict[str, Any]:
        sql = (sql or "").strip()
        if not sql:
            raise ValidationError("sql 为空", stage="hybrid.sql")
        out = self.tables.query(sql, principal=principal)
        if not out.get("ok"):
            raise_from_sqlite_result(
                {"error": out.get("error") or "query failed"},
                sql=sql,
                stage="hybrid.sql",
            )
        return {
            "sql": sql,
            "rows": out.get("rows") or [],
            "timing": out.get("timing"),
        }

    def _propose_sql(self, question: str, schema: dict[str, Any]) -> str | None:
        client = _llm_client()
        prompt = f"""你是 SQL 助手。根据用户问题和 SQLite schema，决定是否需要查询业务表。

规则：
1. 只输出一种：要么一行合法的 SELECT 语句，要么单独输出 NONE
2. 只要 SELECT，禁止 INSERT/UPDATE/DELETE/DDL
3. 问题纯属概念/定义/政策解释、与表数据无关 → NONE
4. 需要名单、数量、工资、部门等表内事实 → 写 SELECT
5. 不要 markdown，不要解释

Schema(JSON)：
{json.dumps(schema, ensure_ascii=False)}

用户问题：{question}
"""
        text = _chat(client, prompt, temperature=0)
        return _extract_sql(text)

    def _fuse_answer(
        self,
        question: str,
        *,
        rag_part: dict[str, Any] | None,
        sql_part: dict[str, Any] | None,
    ) -> str:
        client = _llm_client()
        rag_block = "（无知识库结果）"
        if rag_part and rag_part.get("ok"):
            rag_block = (
                f"答案草稿：{rag_part.get('answer')}\n"
                f"来源文件：{rag_part.get('sources')}\n"
                f"引用编号：{rag_part.get('cite_ids')}"
            )
        sql_block = "（无表查询结果）"
        if sql_part:
            rows = sql_part.get("rows") or []
            preview = rows[:20]
            sql_block = (
                f"SQL：{sql_part.get('sql')}\n"
                f"行数：{len(rows)}\n"
                f"数据(JSON)：{json.dumps(preview, ensure_ascii=False, default=str)}"
            )

        prompt = f"""你是企业知识助手。请综合「表查询结果」与「知识库检索」回答用户。

规则：
1. 数字、名单、统计以表查询为准。
2. 概念、流程、解释以知识库为准，可保留引用 [C#] 若草稿中有。
3. 两边都有时合并，不要互相矛盾；缺的一边就明确说未查到。
4. 不要编造表中没有的行。
5. 最后若用了知识库，可保留一行：引用: ...

用户问题：{question}

【表查询】
{sql_block}

【知识库】
{rag_block}
"""
        return _chat(client, prompt, temperature=0.3)

    def ask(
        self,
        question: str,
        *,
        sql: str | None = None,
        session_id: str | None = "hybrid",
        use_rag: bool = True,
        use_sql: bool = True,
        auto_sql: bool = True,
        use_memory: bool = False,
        principal: Any | None = None,
    ) -> dict[str, Any]:
        """
        混合问答主入口。

        参数：
          question   用户问题（必填）
          sql        显式只读 SQL；优先于 auto_sql
          use_rag    是否走知识库
          use_sql    是否走结构化（显式 sql 或自动生成）
          auto_sql   未给 sql 时是否根据 schema 自动生成 SELECT
          use_memory RAG 侧 session 记忆（默认关）
        """
        q = (question or "").strip()
        if not q:
            raise ValidationError("question 不能为空", stage="hybrid.ask")
        if not use_rag and not use_sql:
            raise ValidationError(
                "use_rag 与 use_sql 不能同时为 False",
                stage="hybrid.ask",
            )

        t0 = time.time()
        sql_part: dict[str, Any] | None = None
        rag_part: dict[str, Any] | None = None
        plan: dict[str, Any] = {
            "use_rag": use_rag,
            "use_sql": use_sql,
            "sql_source": None,
            "sql_executed": False,
        }

        # ---- SQL 分支 ----
        if use_sql:
            chosen = (sql or "").strip() or None
            if chosen:
                plan["sql_source"] = "explicit"
            elif auto_sql:
                plan["sql_source"] = "auto"
                try:
                    schema = self.schema_snapshot(principal=principal)
                    plan["schema_tables"] = schema.get("table_names")
                    chosen = self._propose_sql(q, schema)
                except DatabaseNotReadyError:
                    raise
                except (SqlQueryError, SqlValidationError, LlmError) as e:
                    logger.warning("auto sql failed: %s", e)
                    plan["auto_sql_error"] = e.error_body()
                    chosen = None
                except Exception as e:
                    logger.warning("auto sql unexpected: %s", e)
                    plan["auto_sql_error"] = {
                        "code": "auto_sql_unexpected",
                        "message": str(e),
                        "cause": {"type": type(e).__name__, "message": str(e)},
                    }
                    chosen = None
            else:
                plan["sql_source"] = "skipped"

            if chosen:
                try:
                    sql_part = self._run_sql(chosen, principal=principal)
                    plan["sql_executed"] = True
                except (SqlValidationError, SqlQueryError) as e:
                    # 显式 SQL 失败直接抛；自动 SQL 失败则降级仅 RAG
                    if plan.get("sql_source") == "explicit":
                        raise e.with_stage("hybrid.sql")
                    logger.warning("sql exec degraded: %s", e)
                    plan["sql_exec_error"] = e.error_body()
                    plan["sql_executed"] = False
            else:
                plan["sql_executed"] = False

        # ---- RAG 分支 ----
        if use_rag:
            self._ensure_rag_ready()
            try:
                rag_part = self.kb.ask(
                    q,
                    session_id=session_id or "hybrid",
                    use_memory=use_memory,
                    principal=principal,
                )
            except AppError as e:
                raise e.with_stage("hybrid.rag")
            if not rag_part.get("ok"):
                raise HybridError(
                    rag_part.get("error") or "RAG 分支失败",
                    stage="hybrid.rag",
                    details={"rag": rag_part},
                )

        sql_ok = bool(plan.get("sql_executed") and sql_part is not None)

        # 无有效 SQL → 直接返回 RAG（仍包 hybrid 结构）
        if use_rag and not sql_ok and rag_part:
            return {
                "ok": True,
                "mode": "rag_only",
                "answer": rag_part.get("answer"),
                "sources": rag_part.get("sources"),
                "cite_ids": rag_part.get("cite_ids"),
                "sql": None,
                "rows": None,
                "plan": plan,
                "session_id": rag_part.get("session_id"),
                "timing": time.time() - t0,
                "parts": {"rag": rag_part, "sql": None},
            }

        # 仅 SQL
        if use_sql and sql_ok and not use_rag:
            try:
                answer = self._fuse_answer(q, rag_part=None, sql_part=sql_part)
            except LlmError as e:
                raise e.with_stage("hybrid.fuse")
            return {
                "ok": True,
                "mode": "sql_only",
                "answer": answer,
                "sources": [],
                "cite_ids": [],
                "sql": sql_part.get("sql") if sql_part else None,
                "rows": sql_part.get("rows") if sql_part else None,
                "plan": plan,
                "session_id": session_id,
                "timing": time.time() - t0,
                "parts": {"rag": None, "sql": sql_part},
            }

        if not sql_ok and not rag_part:
            raise HybridError(
                "混合问答无可用证据（SQL 与 RAG 均未产出）",
                stage="hybrid.ask",
                details={"plan": plan},
            )

        # 双边融合
        try:
            answer = self._fuse_answer(q, rag_part=rag_part, sql_part=sql_part)
        except LlmError as e:
            raise e.with_stage("hybrid.fuse")
        except Exception as e:
            raise HybridError(
                "汇总生成失败",
                stage="hybrid.fuse",
                cause=e,
            ) from e

        return {
            "ok": True,
            "mode": "hybrid",
            "answer": answer,
            "sources": (rag_part or {}).get("sources") or [],
            "cite_ids": (rag_part or {}).get("cite_ids") or [],
            "sql": (sql_part or {}).get("sql"),
            "rows": (sql_part or {}).get("rows"),
            "plan": plan,
            "session_id": (rag_part or {}).get("session_id") or session_id,
            "timing": time.time() - t0,
            "parts": {
                "rag": {
                    "answer": (rag_part or {}).get("answer"),
                    "sources": (rag_part or {}).get("sources"),
                    "timing": (rag_part or {}).get("timing"),
                }
                if rag_part
                else None,
                "sql": sql_part,
            },
        }
