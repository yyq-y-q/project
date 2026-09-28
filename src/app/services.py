"""
Service 层：编排 rag / sqlite / agent，供 API 与 CLI 复用。
索引与 pipeline 进程内单例（API / Agent 共用一份）；记忆按 session 隔离。

约定：可预期失败抛 app.errors.AppError 子类，不再用 {"ok": False}。
"""
from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path
from typing import Any

from app.errors import (
    AgentError,
    AppError,
    DocumentNotFoundError,
    IndexBuildError,
    IndexNotReadyError,
    IngestError,
    LlmError,
    PermissionDeniedError,
    RagError,
    SqlQueryError,
    ValidationError,
    raise_from_sqlite_result,
    wrap_unexpected,
)
from app.auth import Principal
from app.logging_setup import get_logger

logger = get_logger(__name__)

# 进程内唯一 RAGPipeline；rebuild 后原地换血，agent.rag_tool 也走这里
_rag_lock = threading.Lock()
_rag = None


def get_rag():
    """懒加载全局 RAG 单例（load 失败仍返回实例，由调用方判 ready）。"""
    global _rag
    with _rag_lock:
        if _rag is not None:
            return _rag
        from rag import RAGPipeline

        pipe = RAGPipeline()
        if not pipe.load_index():
            logger.warning("RAG 索引未加载，需先 rebuild")
        _rag = pipe
        return _rag


def reset_rag_singleton() -> None:
    global _rag
    with _rag_lock:
        _rag = None


def set_rag_singleton(pipe) -> None:
    """rebuild 成功后挂回单例，避免 agent/api 各持一份。"""
    global _rag
    with _rag_lock:
        _rag = pipe


class KnowledgeService:
    """叙述知识库：重建 / 问答。"""

    def rebuild(self, raw_dir: str | Path | None = None, owner_key_id: str | None = None) -> dict[str, Any]:
        """
        重建索引。owner_key_id 用于标记文档所有者（可选）。
        全量重建时，未传 owner_key_id 的文档默认 visibility=public。
        """
        from rag import load_docs_dir
        from rag.config import Config

        root = Path(raw_dir) if raw_dir else Config.RAW_DOCS_DIR
        try:
            docs, metas = load_docs_dir(root)
            if not docs:
                raise DocumentNotFoundError(
                    f"无 RAG 文档: {root}",
                    stage="rag.rebuild",
                    details={"raw_dir": str(root)},
                )
            # 补充 ACL metadata
            if owner_key_id:
                for meta in metas:
                    meta.setdefault("owner_key_id", owner_key_id)
                    meta.setdefault("visibility", "private")
            else:
                for meta in metas:
                    meta.setdefault("visibility", "public")
            
            # 复用单例：build_index 内部已 rag_index_write
            pipe = get_rag()
            ok = pipe.build_index(docs, metas, rebuild=True)
            if not ok:
                # 失败时标记未就绪，但不丢实例（会话记忆仍在）
                pipe._is_built = False
                raise IndexBuildError(
                    stage="rag.rebuild",
                    details={"raw_dir": str(root), "documents": len(docs)},
                )
            set_rag_singleton(pipe)
            return {
                "ok": True,
                "documents": len(docs),
                "sources": [m.get("source") for m in metas],
                "chroma": pipe.vector_index.collection.count(),
            }
        except AppError:
            raise
        except Exception as e:
            logger.exception("rag rebuild failed")
            raise wrap_unexpected(
                e,
                stage="rag.rebuild",
                message="RAG 索引构建异常",
                details={"raw_dir": str(root)},
                as_error=IndexBuildError,
            ) from e

    def ask(
        self,
        question: str,
        session_id: str | None = None,
        use_memory: bool = True,
        principal: Any | None = None,
    ) -> dict[str, Any]:
        """
        RAG 查询。ACL 在两路召回、RRF 融合和重排之前执行，
        因而受限块不会进入摘要、Prompt 或模型调用。
        """
        q = (question or "").strip()
        if not q:
            raise ValidationError("question 不能为空", stage="rag.ask")
        pipe = get_rag()
        if not pipe._is_built and not pipe.load_index():
            raise IndexNotReadyError(stage="rag.ask")
        sid = session_id or "default"
        t0 = time.time()
        try:
            result = pipe.query(
                q,
                use_memory=use_memory,
                session_id=sid,
                principal=principal,
            )
            result["acl_filtered"] = True
        except AppError:
            raise
        except RuntimeError as e:
            # 管道内「索引未构建」等
            msg = str(e)
            if "索引" in msg:
                raise IndexNotReadyError(msg, stage="rag.ask", cause=e) from e
            raise wrap_unexpected(
                e, stage="rag.ask", message=msg or "RAG 查询失败", as_error=RagError
            ) from e
        except ValueError as e:
            # 常见：缺 API Key
            raise LlmError(
                str(e) or "大模型配置/调用失败",
                stage="rag.ask.llm",
                cause=e,
                details={"session_id": sid},
            ) from e
        except Exception as e:
            logger.exception("rag ask failed session=%s", sid)
            raise wrap_unexpected(
                e,
                stage="rag.ask",
                message="RAG 问答失败",
                details={"session_id": sid},
                as_error=RagError,
            ) from e

        return {
            "ok": True,
            "answer": result.get("answer"),
            "sources": result.get("sources"),
            "cite_ids": result.get("cite_ids"),
            "session_id": result.get("session_id"),
            "history_length": result.get("history_length"),
            "timing": result.get("timing"),
            "service_timing": time.time() - t0,
            "debug": result.get("debug"),
            "acl_filtered": result.get("acl_filtered", False),
        }


def _sql_tables(sql: str) -> set[str]:
    """只解析表名，不执行 SQL；用于执行前 ACL 校验。"""
    import sqlglot
    from sqlglot import exp

    tree = sqlglot.parse_one(sql, read="sqlite")
    return {table.name for table in tree.find_all(exp.Table) if table.name}


class TableService:
    """结构化表：ingest / 只读 SQL。"""

    def ingest_all(self, raw_dir: str | Path | None = None) -> dict[str, Any]:
        from sqlite_tool import ingest_dir

        try:
            out = ingest_dir(raw_dir)
        except AppError:
            raise
        except Exception as e:
            logger.exception("sqlite ingest failed")
            raise wrap_unexpected(
                e,
                stage="sqlite.ingest",
                message="表数据摄取异常",
                details={"raw_dir": str(raw_dir) if raw_dir else None},
                as_error=IngestError,
            ) from e

        if isinstance(out, dict) and out.get("ok") is False:
            raise IngestError(
                out.get("error") or "表数据摄取失败",
                stage="sqlite.ingest",
                details={"result": out},
            )
        if isinstance(out, dict):
            out = {**out, "ok": True}
        return out

    def query(
        self,
        sql: str,
        principal: Principal | None = None,
    ) -> dict[str, Any]:
        from app.acl import table_access_allowed
        from sqlite_tool import query as sqlite_query

        text = (sql or "").strip()
        if not text:
            raise ValidationError("sql 不能为空", stage="sqlite.query")
        try:
            tables = _sql_tables(text)
            if not table_access_allowed(tables, principal):
                raise PermissionDeniedError(
                    "当前 Principal 无权读取 SQL 引用的表",
                    stage="sqlite.acl",
                    details={"tables": sorted(tables)},
                )
        except PermissionDeniedError:
            raise
        except ValueError as e:
            raise SqlQueryError(
                "SQL 无法解析，未执行查询",
                stage="sqlite.acl",
                cause=e,
            ) from e
        t0 = time.time()
        try:
            result = sqlite_query(text)
        except AppError:
            raise
        except Exception as e:
            logger.exception("sqlite query crashed")
            raise wrap_unexpected(
                e,
                stage="sqlite.query",
                message="SQL 执行异常",
                details={"sql": text[:500]},
                as_error=SqlQueryError,
            ) from e

        if isinstance(result, dict) and result.get("error"):
            raise_from_sqlite_result(result, sql=text, stage="sqlite.query")
        return {"ok": True, "rows": result, "timing": time.time() - t0}


class AgentService:
    """ReAct Agent 单次任务。"""

    def run(
        self,
        task: str,
        work_dir: str | Path | None = None,
        session_id: str | None = None,
        principal: Any | None = None,
    ) -> dict[str, Any]:
        from functools import partial

        from agent.config import Config as AgentConfig
        from agent.react_manager import ReActManager
        from agent.tools import (
            make_file_tools,
            make_terminal_tool,
            rag_Query,
            sqlite_Query,
        )
        from app.auth import scoped_session_id

        text = (task or "").strip()
        if not text:
            raise ValidationError("task 不能为空", stage="agent.run")

        if work_dir is None:
            AgentConfig.DEFAULT_WORK_DIR.mkdir(parents=True, exist_ok=True)
            work = str(AgentConfig.DEFAULT_WORK_DIR.resolve())
        else:
            work = str(Path(work_dir).resolve())

        sid = scoped_session_id(principal, session_id) or f"api-{uuid.uuid4().hex[:8]}"
        fr, fw = make_file_tools(work)
        term = make_terminal_tool(work)
        # 把调用者身份绑定进工具：Agent 内部查知识库/查表同样受 ACL 约束。
        rag = partial(rag_Query, principal=principal)
        sq = partial(sqlite_Query, principal=principal)
        agent = ReActManager(
            work,
            [fr, fw, term, sq, rag],
            model=AgentConfig.MODEL,
            session_id=sid,
        )
        t0 = time.time()
        try:
            answer = agent.run(text)
            return {
                "ok": True,
                "answer": answer,
                "session_id": sid,
                "work_dir": work,
                "timing": time.time() - t0,
            }
        except AppError as e:
            raise e.with_stage("agent.run")
        except Exception as e:
            logger.exception("agent failed")
            raise AgentError(
                "Agent 执行失败",
                stage="agent.run",
                cause=e,
                details={"session_id": sid, "work_dir": work},
            ) from e


class HealthService:
    def status(self, *, deep: bool = True) -> dict[str, Any]:
        """
        存活 + 就绪信息。
        deep=False：只做轻量检查（给 k8s liveness，不碰模型/索引加载）。
        """
        import os
        from pathlib import Path

        from app.auth import auth_configured
        from app.settings import get_settings
        from rag.config import Config as RagConfig
        from sqlite_tool.config import Config as SqlConfig

        settings = get_settings()
        chroma = Path(RagConfig.CHROMA_PERSIST_DIR)
        bm25 = Path(RagConfig.BM25_PATH)
        qdb = Path(SqlConfig.QUERY_PATH)
        lock_dir = settings.project_root / "data" / "locks"
        log_dir = settings.project_root / "data" / "logs"

        llm_key = bool(os.getenv(RagConfig.LLM_API_KEY_ENV, "").strip())
        checks: dict[str, Any] = {
            "env": settings.env,
            "require_api_key": settings.require_api_key,
            "auth_configured": auth_configured(),
            "llm_key_configured": llm_key,
            "bm25_exists": bm25.exists(),
            "chroma_dir_exists": chroma.exists(),
            "query_db_exists": qdb.exists(),
            "lock_dir_writable": False,
            "log_dir_writable": False,
        }

        for label, path in (("lock_dir_writable", lock_dir), ("log_dir_writable", log_dir)):
            try:
                path.mkdir(parents=True, exist_ok=True)
                probe = path / ".write_probe"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink(missing_ok=True)
                checks[label] = True
            except Exception as e:
                logger.info("health write probe %s: %s", label, e)
                checks[label] = False

        rag_ok = False
        if deep:
            try:
                pipe = get_rag()
                rag_ok = bool(pipe._is_built or pipe.load_index())
            except Exception as e:
                logger.info("health rag: %s", e)
        else:
            rag_ok = checks["bm25_exists"] and checks["chroma_dir_exists"]

        checks["rag_index_ready"] = rag_ok

        # 生产就绪：鉴权已配、LLM key、锁目录可写；索引可后补但仍标 ready_for_traffic
        prod_blockers: list[str] = []
        if settings.is_production or settings.require_api_key:
            if not checks["auth_configured"]:
                prod_blockers.append("auth_keys_missing")
            if not checks["llm_key_configured"]:
                prod_blockers.append("llm_key_missing")
            if not checks["lock_dir_writable"]:
                prod_blockers.append("lock_dir_not_writable")
        ready = len(prod_blockers) == 0

        return {
            "ok": True,
            "ready": ready,
            "prod_blockers": prod_blockers,
            "time": time.time(),
            **checks,
        }
