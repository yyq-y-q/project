"""
统一业务异常

分层约定
--------
  1. 领域/工具层（rag、sqlite_tool、agent）
     - 可继续抛原生异常，或返回 {error: ...}
     - 不强制依赖 app.errors（避免反向耦合）

  2. Service / Hybrid 边界（app.services / app.hybrid）
     - 可预期失败 → 抛 AppError 子类
     - 不可预期的底层异常 → wrap 成对应域错误或 InternalError
       raise XxxError("说明", stage="rag.ask", cause=e, details={...})

  3. 入口（API / CLI）
     - 只做全局捕获与序列化，业务里不再 try/except 包一层
     - API: AppError → 对应 http_status + JSON
            入参校验 → 422
            其它 Exception → 500 internal_error（日志留栈，响应不吐栈）
     - CLI: 同一 to_dict()，exit 1

字段
----
  code        稳定机器码（前端/日志分支）
  message     给人看的说明
  stage       失败环节（如 hybrid.sql / rag.rebuild）
  details     可安全展示的上下文（sql 片段、session、路径…）
  cause       原始异常（__cause__）；to_dict 只给 str(cause)
  http_status API 状态码

用法
----
  raise IndexNotReadyError()
  raise SqlQueryError("语法错误", stage="sqlite.query", details={"sql": sql})
  raise LlmError("调用失败", stage="hybrid.fuse", cause=e) from e
"""
from __future__ import annotations

from typing import Any


class AppError(Exception):
    """项目可预期失败的根异常。"""

    code: str = "app_error"
    http_status: int = 400

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        cause: BaseException | None = None,
        stage: str | None = None,
    ):
        self.message = message or self._default_message()
        self.details = dict(details or {})
        self.cause = cause
        self.stage = stage
        super().__init__(self.message)
        if cause is not None:
            self.__cause__ = cause

    def _default_message(self) -> str:
        return "业务处理失败"

    def with_stage(self, stage: str) -> AppError:
        """链式补 stage（已有则不覆盖）。"""
        if not self.stage:
            self.stage = stage
        return self

    def error_body(self, *, include_cause: bool = True) -> dict[str, Any]:
        """error 对象本体（不含 ok）。"""
        body: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.stage:
            body["stage"] = self.stage
        if self.details:
            body["details"] = self.details
        if include_cause and self.cause is not None:
            body["cause"] = {
                "type": type(self.cause).__name__,
                "message": str(self.cause),
            }
        return body

    def to_dict(self, *, include_cause: bool = True) -> dict[str, Any]:
        return {"ok": False, "error": self.error_body(include_cause=include_cause)}


# ---------- 鉴权 / 权限 ----------


class AuthError(AppError):
    code = "auth_error"
    http_status = 401

    def _default_message(self) -> str:
        return "认证失败"


class PermissionDeniedError(AppError):
    code = "permission_denied"
    http_status = 403

    def _default_message(self) -> str:
        return "权限不足"


class AuditIntegrityError(AppError):
    code = "audit_integrity_error"
    http_status = 500

    def _default_message(self) -> str:
        return "审计链完整性校验失败"


class WorkflowError(AppError):
    code = "change_workflow_error"
    http_status = 409

    def _default_message(self) -> str:
        return "变更工作流状态不允许"


class ChangeNotFoundError(WorkflowError):
    code = "change_not_found"
    http_status = 404

    def _default_message(self) -> str:
        return "变更申请不存在"


class TemporaryFileError(AppError):
    code = "temporary_file_error"
    http_status = 400

    def _default_message(self) -> str:
        return "临时文件处理失败"


# ---------- 入参 ----------


class ValidationError(AppError):
    code = "validation_error"
    http_status = 422

    def _default_message(self) -> str:
        return "参数不合法"


# ---------- RAG ----------


class RagError(AppError):
    code = "rag_error"
    http_status = 500

    def _default_message(self) -> str:
        return "知识库处理失败"


class IndexNotReadyError(RagError):
    code = "rag_index_not_ready"
    http_status = 503

    def _default_message(self) -> str:
        return "RAG 索引未就绪，请先执行 rag-rebuild / POST /v1/rag/rebuild"


class IndexBuildError(RagError):
    code = "rag_index_build_failed"
    http_status = 500

    def _default_message(self) -> str:
        return "RAG 索引构建失败"


class DocumentNotFoundError(RagError):
    code = "rag_no_documents"
    http_status = 404

    def _default_message(self) -> str:
        return "未找到可摄取的叙述文档（检查 data/raw 下 txt/md/docx/pdf）"


class LlmError(RagError):
    code = "llm_error"
    http_status = 502

    def _default_message(self) -> str:
        return "大模型调用失败"


class RetrievalError(RagError):
    code = "rag_retrieval_error"
    http_status = 500

    def _default_message(self) -> str:
        return "检索失败"


# ---------- SQLite ----------


class SqliteError(AppError):
    code = "sqlite_error"
    http_status = 500

    def _default_message(self) -> str:
        return "结构化数据层失败"


class SqlValidationError(SqliteError):
    code = "sql_validation_error"
    http_status = 400

    def _default_message(self) -> str:
        return "SQL 未通过只读校验（仅允许单条 SELECT）"


class SqlQueryError(SqliteError):
    code = "sql_query_error"
    http_status = 400

    def _default_message(self) -> str:
        return "SQL 执行失败"


class IngestError(SqliteError):
    code = "ingest_error"
    http_status = 500

    def _default_message(self) -> str:
        return "表数据摄取失败"


class DatabaseNotReadyError(SqliteError):
    code = "db_not_ready"
    http_status = 503

    def _default_message(self) -> str:
        return "业务库未就绪，请先 sqlite-ingest"


# ---------- Agent / Hybrid ----------


class AgentError(AppError):
    code = "agent_error"
    http_status = 500

    def _default_message(self) -> str:
        return "Agent 执行失败"


class HybridError(AppError):
    code = "hybrid_error"
    http_status = 500

    def _default_message(self) -> str:
        return "混合问答失败"


class ConfigError(AppError):
    code = "config_error"
    http_status = 500

    def _default_message(self) -> str:
        return "配置错误（检查 .env 与路径）"


# ---------- 未预期 ----------


class InternalError(AppError):
    """入口兜底：未分类异常。客户端只看 code/message，栈只进日志。"""

    code = "internal_error"
    http_status = 500

    def _default_message(self) -> str:
        return "服务内部错误"


def wrap_unexpected(
    exc: BaseException,
    *,
    stage: str,
    message: str | None = None,
    details: dict[str, Any] | None = None,
    as_error: type[AppError] = InternalError,
) -> AppError:
    """
    把非 AppError 的底层异常收成业务异常。
    已是 AppError 则补 stage 后原样返回（不二次包装）。
    """
    if isinstance(exc, AppError):
        return exc.with_stage(stage)

    cls = as_error
    msg = message or cls()._default_message()
    return cls(msg, stage=stage, cause=exc, details=details)


def raise_from_sqlite_result(result: Any, *, sql: str | None = None, stage: str = "sqlite.query") -> None:
    """若 sqlite_tool.query 返回 {error: ...} 则抛出对应 Sql*Error。"""
    if not (isinstance(result, dict) and result.get("error")):
        return

    details: dict[str, Any] = {"upstream": result["error"]}
    if sql:
        details["sql"] = sql[:500]
    msg = str(result["error"])
    low = msg.lower()
    # 只读校验失败 → SqlValidationError；其余执行失败 → SqlQueryError
    if any(
        k in msg for k in ("不合法", "非法", "数量错误", "仅允许", "只读")
    ) or "validat" in low:
        raise SqlValidationError(msg, stage=stage, details=details)
    raise SqlQueryError(msg, stage=stage, details=details)


def internal_from_exception(
    exc: BaseException,
    *,
    stage: str | None = None,
    request_id: str | None = None,
) -> InternalError:
    """API/CLI 全局兜底构造 InternalError（响应不含栈）。"""
    details: dict[str, Any] = {"exception_type": type(exc).__name__}
    if request_id:
        details["request_id"] = request_id
    return InternalError(
        "服务内部错误，请查看服务端日志",
        stage=stage or "unhandled",
        cause=exc,
        details=details,
    )
