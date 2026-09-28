"""
HTTP API（FastAPI）

  鉴权：Header X-API-Key（见 app.auth）
  错误：app.errors.AppError → 统一 JSON {ok:false, error:{code,message,...}}
  监控：/v1/health 、/v1/ready 、/v1/metrics

启动：
  uv run mykb serve
  # 或
  uv run uvicorn app.api:app --app-dir src --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import logging
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, Header, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel, Field

from app.auth import (
    Principal,
    Role,
    auth_configured,
    require_permission,
    require_role,
    resolve_principal,
    scoped_session_id,
)
from app import change_service, temp_file_service
from app.governance import (
    audit_event,
    create_dynamic_key,
    get_change,
    init_schema,
    list_audit_events,
    list_dynamic_keys,
    verify_audit_chain,
    revoke_dynamic_key,
)
from app.chat_service import ChatService
from app.errors import AppError, ValidationError, internal_from_exception
from app.hybrid import HybridService
from app.logging_setup import get_logger, setup_logging
from app.ratelimit import RateLimiter, key_digest
from app.services import AgentService, HealthService, KnowledgeService, TableService
from app.settings import get_settings, validate_runtime_settings

_settings = get_settings()
logger = get_logger(__name__)

_limiter = RateLimiter(
    rpm=_settings.rate_limit_rpm,
    burst=_settings.rate_limit_burst,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    level = getattr(logging, _settings.log_level)
    setup_logging(level=level)
    init_schema()
    issues = validate_runtime_settings(
        _settings,
        auth_configured=auth_configured() or bool(_settings.oidc),
    )
    if issues:
        message = "; ".join(issues)
        logger.critical("runtime configuration rejected: %s", message)
        raise RuntimeError(f"invalid runtime configuration: {message}")
    yield


app = FastAPI(
    title="Private Knowledge Base API",
    version="0.3.0",
    description="RAG + SQLite + Hybrid + Agent + unified Chat",
    lifespan=lifespan,
)

if _settings.oidc:
    app.add_middleware(
        SessionMiddleware,
        secret_key=_settings.oidc.session_secret,
        session_cookie="private_kb_oidc",
        same_site="lax",
        https_only=_settings.is_production,
        max_age=600,
    )

if _settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(_settings.cors_origins),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

_STATIC = Path(__file__).resolve().parent / "static"
if _STATIC.is_dir():
    app.mount("/ui", StaticFiles(directory=str(_STATIC), html=True), name="ui")


@app.get("/")
def root():
    index = _STATIC / "index.html"
    if index.exists():
        return FileResponse(index)
    return {"service": "private-kb", "docs": "/docs", "ui": "/ui/"}


_metrics = {
    "requests": 0,
    "errors": 0,
    "rag_ask": 0,
    "sql_query": 0,
    "hybrid_ask": 0,
    "agent_run": 0,
    "chat": 0,
    "started_at": time.time(),
}

kb = KnowledgeService()
tables = TableService()
hybrid = HybridService(knowledge=kb, tables=tables)
agents = AgentService()
chat = ChatService(knowledge=kb, tables=tables, hybrid=hybrid, agents=agents)
health = HealthService()


def _audit_request_failure(request: Request, *, action: str, reason: str) -> None:
    principal = getattr(request.state, "principal", None)
    try:
        audit_event(
            request_id=request_id(request),
            action=action,
            status="failed",
            operator=principal.operator if principal else "unknown",
            key_id=principal.key_id if principal else "unknown",
            source_ip=source_ip(request),
            target_id=request.url.path,
            failure_reason=reason[:2000],
        )
    except Exception:
        # 审计故障不能覆盖原始业务错误；完整原因已进入服务日志。
        logger.exception("failed to append request failure audit")


# ---- 全局异常：业务 / 入参 / 未预期 ----


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    _metrics["errors"] += 1
    _audit_request_failure(
        request,
        action="request.error",
        reason=f"{exc.code}: {exc.message}",
    )
    logger.warning(
        "AppError code=%s stage=%s path=%s msg=%s",
        exc.code,
        exc.stage,
        request.url.path,
        exc.message,
    )
    body = exc.to_dict(include_cause=False)
    body["request_id"] = request_id(request)
    response = JSONResponse(status_code=exc.http_status, content=body)
    response.headers["X-Request-ID"] = request_id(request)
    return response


@app.exception_handler(RequestValidationError)
async def request_validation_handler(request: Request, exc: RequestValidationError):
    """FastAPI/Pydantic 体校验 → 与 ValidationError 同形 JSON。"""
    _metrics["errors"] += 1
    err = ValidationError(
        "请求参数校验失败",
        stage="api.validation",
        details={"path": request.url.path, "issues": exc.errors()},
    )
    _audit_request_failure(request, action="request.validation_failed", reason=err.message)
    logger.warning("validation failed path=%s", request.url.path)
    body = err.to_dict()
    body["request_id"] = request_id(request)
    response = JSONResponse(status_code=err.http_status, content=body)
    response.headers["X-Request-ID"] = request_id(request)
    return response


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    """
    最后兜底：任何没转成 AppError 的异常 → 500 internal_error。
    完整栈只写日志，响应不暴露 traceback。
    """
    _metrics["errors"] += 1
    wrapped = internal_from_exception(
        exc,
        stage=f"api.{request.url.path}",
        request_id=request_id(request),
    )
    _audit_request_failure(
        request,
        action="request.internal_error",
        reason=f"{type(exc).__name__}: {exc}",
    )
    logger.exception(
        "unhandled path=%s type=%s",
        request.url.path,
        type(exc).__name__,
    )
    body = wrapped.to_dict(include_cause=False)
    body["request_id"] = request_id(request)
    response = JSONResponse(status_code=wrapped.http_status, content=body)
    response.headers["X-Request-ID"] = request_id(request)
    return response


def get_principal(
    request: Request,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> Principal:
    bearer = None
    if authorization:
        parts = authorization.split(maxsplit=1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            bearer = parts[1]
    principal = resolve_principal(x_api_key, bearer=bearer)
    request.state.principal = principal
    return principal


def request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", "unknown"))


def source_ip(request: Request) -> str:
    from app.settings import get_settings

    # 只有明确声明可信反代时才读取 X-Forwarded-For，避免客户端伪造操作者 IP。
    if get_settings().trust_proxy:
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if forwarded:
            return forwarded
    return request.client.host if request.client else "unknown"


class AskBody(BaseModel):
    question: str
    session_id: str | None = "default"
    use_memory: bool = True


class SqlBody(BaseModel):
    sql: str = Field(..., description="只读 SELECT")


class HybridBody(BaseModel):
    question: str
    sql: str | None = Field(default=None, description="可选显式 SELECT；空则 auto_sql")
    session_id: str | None = "hybrid"
    use_rag: bool = True
    use_sql: bool = True
    auto_sql: bool = True
    use_memory: bool = False


class AgentBody(BaseModel):
    task: str
    work_dir: str | None = None
    session_id: str | None = None


class IngestBody(BaseModel):
    raw_dir: str | None = None


class ChatJsonBody(BaseModel):
    """JSON 形态统一对话（无文件时用）。"""

    message: str = ""
    session_id: str | None = "chat"
    force_mode: str | None = Field(default=None, description="可选强制: sql|rag|hybrid|agent|chat")
    use_memory: bool = True
    unpack_paste: bool = True


class KeyCreateBody(BaseModel):
    operator: str = Field(..., min_length=1, max_length=120)
    role: str
    permissions: list[str] = Field(default_factory=list)
    expires_at: str | None = None


class ChangeSubmitBody(BaseModel):
    operation: str = Field(..., description="upsert 或 delete")
    target_path: str = Field(..., min_length=1, max_length=500)
    after_content: str | None = None


class TempSearchBody(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)


class AuditQuery(BaseModel):
    target_id: str | None = None
    limit: int = Field(default=100, ge=1, le=500)


@app.middleware("http")
async def count_requests(request, call_next):
    _metrics["requests"] += 1
    incoming = request.headers.get("x-request-id", "").strip()
    request.state.request_id = incoming[:128] if incoming else f"req_{uuid.uuid4().hex}"
    try:
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response
    except Exception:
        _metrics["errors"] += 1
        raise


@app.middleware("http")
async def rate_limit_requests(request: Request, call_next):
    """进程内限流：按 API Key（摘要）或客户端 IP 计数，探针豁免。"""
    if not _settings.rate_limit_enabled:
        return await call_next(request)
    path = request.url.path
    if path in ("/v1/health", "/v1/ready"):
        return await call_next(request)
    key = request.headers.get("x-api-key", "").strip() or source_ip(request) or "unknown"
    allowed, remaining, retry = _limiter.allow("api", key_digest(key))
    if not allowed:
        body = {
            "ok": False,
            "error": {
                "code": "rate_limited",
                "message": "请求过于频繁，请稍后再试",
                "request_id": request_id(request),
            },
        }
        response = JSONResponse(status_code=429, content=body)
        response.headers["X-Request-ID"] = request_id(request)
        response.headers["Retry-After"] = str(retry)
        return response
    response = await call_next(request)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    return response


@app.get("/v1/health")
def api_health() -> dict[str, Any]:
    """存活探针：轻量，不加载模型。"""
    return health.status(deep=False)


@app.get("/v1/ready")
def api_ready() -> JSONResponse:
    """就绪探针：生产缺 key/锁目录 → 503。"""
    st = health.status(deep=False)
    code = 200 if st.get("ready") else 503
    return JSONResponse(status_code=code, content=st)


@app.get("/v1/metrics")
def api_metrics(p: Principal = Depends(get_principal)) -> dict[str, Any]:
    require_permission(p, "knowledge.read")
    return {
        **_metrics,
        "uptime_s": time.time() - _metrics["started_at"],
        "principal": p.key_id,
        "role": p.role.value,
        "env": _settings.env,
    }


# ---- OIDC 登录路由（可选；未启用时返回 404）----


@app.get("/auth/login")
def oidc_login(request: Request):
    from app.errors import AuthError
    from app.oidc import build_auth_url, generate_pkce_pair

    try:
        verifier, code_challenge = generate_pkce_pair()
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        request.session.update(
            {"oidc_state": state, "oidc_verifier": verifier, "oidc_nonce": nonce}
        )
        auth_url = build_auth_url(state, nonce, code_challenge)
        return {"auth_url": auth_url}
    except AuthError as e:
        raise e
    except Exception as e:
        logger.exception("OIDC login 失败")
        raise AuthError("OIDC 登录不可用", stage="oidc.login", cause=e) from e


@app.get("/auth/callback")
def oidc_callback(code: str, state: str, request: Request):
    from app.errors import AuthError
    from app.oidc import claims_to_principal, create_session_token, exchange_code, verify_id_token

    try:
        session = request.session
        saved_state = session.pop("oidc_state", None)
        verifier = session.pop("oidc_verifier", None)
        nonce = session.pop("oidc_nonce", None)
        if not isinstance(saved_state, str) or not secrets.compare_digest(saved_state, state):
            raise AuthError("state 不匹配", stage="oidc.callback")
        if not isinstance(verifier, str) or not verifier:
            raise AuthError("缺少 PKCE verifier", stage="oidc.callback")
        if not isinstance(nonce, str) or not nonce:
            raise AuthError("缺少 OIDC nonce", stage="oidc.callback")
        tokens = exchange_code(code, verifier)
        id_token = tokens.get("id_token")
        if not id_token:
            raise AuthError("缺少 id_token", stage="oidc.callback")
        claims = verify_id_token(id_token, nonce)
        principal = claims_to_principal(claims)
        session_token = create_session_token(principal)
        audit_event(
            request_id=request_id(request),
            action="auth.oidc.login",
            status="success",
            operator=principal.operator,
            key_id=principal.key_id,
            source_ip=source_ip(request),
            target_id="oidc_callback",
        )
        return {"ok": True, "session_token": session_token, "operator": principal.operator, "role": principal.role.value}
    except AuthError as e:
        raise e
    except Exception as e:
        logger.exception("OIDC callback 失败")
        raise AuthError("OIDC 回调失败", stage="oidc.callback", cause=e) from e


@app.get("/auth/me")
def oidc_me(p: Principal = Depends(get_principal)) -> dict[str, Any]:
    return {
        "key_id": p.key_id,
        "operator": p.operator,
        "role": p.role.value,
        "permissions": list(p.permissions),
        "source": p.source,
        "expires_at": p.expires_at,
    }


@app.post("/v1/admin/keys")
def api_create_key(
    body: KeyCreateBody,
    request: Request,
    p: Principal = Depends(get_principal),
) -> dict[str, Any]:
    require_permission(p, "*")
    try:
        role = Role(body.role)
    except ValueError as exc:
        raise ValidationError("role 不受支持", stage="auth.key.create") from exc
    from app.auth import DEFAULT_PERMISSIONS
    from app.governance import parse_expiry

    expires_at = body.expires_at
    if expires_at:
        expires_at = parse_expiry(expires_at).isoformat(timespec="seconds")
    permissions = body.permissions or list(DEFAULT_PERMISSIONS[role])
    created = create_dynamic_key(
        operator=body.operator.strip(),
        role=role.value,
        permissions=permissions,
        expires_at=expires_at,
        created_by=p.operator,
    )
    event_id = audit_event(
        request_id=request_id(request),
        action="auth.key.create",
        status="success",
        operator=p.operator,
        key_id=p.key_id,
        source_ip=source_ip(request),
        target_id=created["key_id"],
    )
    return {**created, "audit_event_id": event_id}


@app.get("/v1/admin/keys")
def api_list_keys(request: Request, p: Principal = Depends(get_principal)) -> dict[str, Any]:
    require_permission(p, "*")
    return {"ok": True, "items": list_dynamic_keys()}


@app.post("/v1/admin/keys/{key_id}/revoke")
def api_revoke_key(key_id: str, request: Request, p: Principal = Depends(get_principal)) -> dict[str, Any]:
    require_permission(p, "*")
    if not revoke_dynamic_key(key_id, p.operator):
        raise ValidationError("key 不存在或已经撤销", stage="auth.key.revoke")
    event_id = audit_event(
        request_id=request_id(request),
        action="auth.key.revoke",
        status="success",
        operator=p.operator,
        key_id=p.key_id,
        source_ip=source_ip(request),
        target_id=key_id,
    )
    return {"ok": True, "key_id": key_id, "audit_event_id": event_id}


@app.post("/v1/changes")
def api_submit_change(
    body: ChangeSubmitBody,
    request: Request,
    p: Principal = Depends(get_principal),
) -> dict[str, Any]:
    return change_service.submit_change(
        operation=body.operation,
        target_path=body.target_path,
        after_content=body.after_content,
        principal=p,
        request_id=request_id(request),
        source_ip=source_ip(request),
    )


@app.get("/v1/changes")
def api_list_changes(
    request: Request,
    status: str | None = None,
    limit: int = 100,
    p: Principal = Depends(get_principal),
) -> dict[str, Any]:
    require_permission(p, "knowledge.read")
    return change_service.get_changes(status=status, limit=limit)


@app.get("/v1/changes/{change_id}")
def api_get_change(change_id: str, p: Principal = Depends(get_principal)) -> dict[str, Any]:
    require_permission(p, "knowledge.read")
    item = get_change(change_id)
    if not item:
        raise change_service.ChangeNotFoundError(details={"change_id": change_id}, stage="change.get")
    return {"ok": True, "change": item}


@app.post("/v1/changes/{change_id}/approve")
def api_approve_change(change_id: str, request: Request, p: Principal = Depends(get_principal)) -> dict[str, Any]:
    return change_service.approve_change(
        change_id=change_id, principal=p, request_id=request_id(request), source_ip=source_ip(request)
    )


@app.post("/v1/changes/{change_id}/execute")
def api_execute_change(change_id: str, request: Request, p: Principal = Depends(get_principal)) -> dict[str, Any]:
    return change_service.execute_change(
        change_id=change_id, principal=p, request_id=request_id(request), source_ip=source_ip(request)
    )


@app.post("/v1/changes/{change_id}/rollback")
def api_rollback_change(change_id: str, request: Request, p: Principal = Depends(get_principal)) -> dict[str, Any]:
    return change_service.rollback_change(
        change_id=change_id, principal=p, request_id=request_id(request), source_ip=source_ip(request)
    )


@app.get("/v1/audit")
def api_audit(
    request: Request,
    target_id: str | None = None,
    limit: int = 100,
    p: Principal = Depends(get_principal),
) -> dict[str, Any]:
    require_permission(p, "audit.read")
    return {"ok": True, "items": list_audit_events(target_id=target_id, limit=limit)}


@app.get("/v1/audit/verify")
def api_audit_verify(p: Principal = Depends(get_principal)) -> dict[str, Any]:
    require_permission(p, "audit.read")
    return verify_audit_chain()


@app.post("/v1/temp-files")
async def api_upload_temp_file(
    request: Request,
    file: UploadFile = File(...),
    p: Principal = Depends(get_principal),
) -> dict[str, Any]:
    return temp_file_service.upload_temp_file(
        filename=file.filename or "upload",
        content_type=file.content_type,
        stream=file.file,
        principal=p,
        request_id=request_id(request),
        source_ip=source_ip(request),
    )


@app.post("/v1/temp-files/{file_id}/search")
def api_search_temp_file(
    file_id: str,
    body: TempSearchBody,
    request: Request,
    p: Principal = Depends(get_principal),
) -> dict[str, Any]:
    return temp_file_service.search_temp_file(
        file_id=file_id,
        query=body.query,
        principal=p,
        request_id=request_id(request),
        source_ip=source_ip(request),
    )


@app.post("/v1/temp-files/cleanup")
def api_cleanup_temp_files(request: Request, p: Principal = Depends(get_principal)) -> dict[str, Any]:
    require_permission(p, "*")
    count = temp_file_service.cleanup_expired(request_id=request_id(request), source_ip=source_ip(request))
    return {"ok": True, "deleted": count}


@app.post("/v1/rag/rebuild")
def api_rag_rebuild(p: Principal = Depends(get_principal)) -> dict[str, Any]:
    require_role(p, Role.ADMIN)
    logger.info("rag rebuild by %s", p.key_id)
    return kb.rebuild(owner_key_id=p.key_id if p.source in {"oidc", "database"} else None)


@app.post("/v1/rag/ask")
def api_rag_ask(body: AskBody, p: Principal = Depends(get_principal)) -> dict[str, Any]:
    require_permission(p, "knowledge.read")
    _metrics["rag_ask"] += 1
    logger.info("rag ask session=%s by %s", body.session_id, p.key_id)
    return kb.ask(body.question, session_id=body.session_id, use_memory=body.use_memory, principal=p)


@app.post("/v1/sqlite/ingest")
def api_sqlite_ingest(
    body: IngestBody | None = None,
    p: Principal = Depends(get_principal),
) -> dict[str, Any]:
    require_role(p, Role.ADMIN)
    raw = body.raw_dir if body else None
    logger.info("sqlite ingest by %s", p.key_id)
    return tables.ingest_all(raw)


@app.post("/v1/sqlite/query")
def api_sqlite_query(body: SqlBody, p: Principal = Depends(get_principal)) -> dict[str, Any]:
    require_permission(p, "knowledge.read")
    _metrics["sql_query"] += 1
    # 必须把调用者身份传入，表级 ACL 才会按人校验（防止 ACL 空转）。
    return tables.query(body.sql, principal=p)


@app.post("/v1/hybrid/ask")
def api_hybrid_ask(body: HybridBody, p: Principal = Depends(get_principal)) -> dict[str, Any]:
    require_permission(p, "knowledge.read")
    if not body.question or not body.question.strip():
        raise ValidationError("question 不能为空", stage="api.hybrid.ask")
    _metrics["hybrid_ask"] += 1
    scoped = scoped_session_id(p, body.session_id)
    logger.info("hybrid ask session=%s by %s", scoped, p.key_id)
    return hybrid.ask(
        body.question,
        sql=body.sql,
        session_id=scoped,
        use_rag=body.use_rag,
        use_sql=body.use_sql,
        auto_sql=body.auto_sql,
        use_memory=body.use_memory,
        principal=p,
    )


@app.post("/v1/agent/run")
def api_agent_run(body: AgentBody, p: Principal = Depends(get_principal)) -> dict[str, Any]:
    require_role(p, Role.ADMIN, Role.USER)
    _metrics["agent_run"] += 1
    scoped = scoped_session_id(p, body.session_id)
    logger.info("agent run session=%s by %s", scoped, p.key_id)
    return agents.run(body.task, work_dir=body.work_dir, session_id=scoped, principal=p)


@app.post("/v1/chat")
async def api_chat(
    p: Principal = Depends(get_principal),
    message: str = Form(default=""),
    session_id: str = Form(default="chat"),
    force_mode: str | None = Form(default=None),
    use_memory: bool = Form(default=True),
    unpack_paste: bool = Form(default=True),
    files: list[UploadFile] | None = File(default=None),
) -> dict[str, Any]:
    """
    统一对话框入口（multipart）。

      message       自然语言 / SQL / 粘贴的多文件代码块
      files[]       可选附件（表 csv/xlsx、文档、代码）
      force_mode    可选覆盖自动分流
      session_id    会话隔离（记忆 + 附件工作区）
    """
    require_permission(p, "knowledge.read")
    _metrics["chat"] += 1
    logger.info("chat session=%s by %s files=%s", session_id, p.key_id, len(files or []))

    fm = (force_mode or "").strip().lower() or None
    if fm == "agent":
        require_role(p, Role.ADMIN, Role.USER)

    file_pairs: list[tuple[str, Any]] = []
    if files:
        for uf in files:
            if not uf or not uf.filename:
                continue
            data = await uf.read()
            from io import BytesIO

            file_pairs.append((uf.filename, BytesIO(data)))

    return chat.handle(
        message,
        session_id=session_id,
        force_mode=fm,
        files=file_pairs or None,
        use_memory=use_memory,
        unpack_paste=unpack_paste,
        allow_agent=p.role != Role.READONLY,
        principal=p,
    )


@app.post("/v1/chat/json")
def api_chat_json(
    body: ChatJsonBody,
    p: Principal = Depends(get_principal),
) -> dict[str, Any]:
    """统一对话 JSON 体（无 multipart 时）。"""
    require_permission(p, "knowledge.read")
    _metrics["chat"] += 1
    logger.info("chat.json session=%s by %s", body.session_id, p.key_id)
    fm = (body.force_mode or "").strip().lower() or None
    if fm == "agent":
        require_role(p, Role.ADMIN, Role.USER)
    return chat.handle(
        body.message,
        session_id=body.session_id,
        force_mode=fm,
        files=None,
        use_memory=body.use_memory,
        unpack_paste=body.unpack_paste,
        allow_agent=p.role != Role.READONLY,
        principal=p,
    )
