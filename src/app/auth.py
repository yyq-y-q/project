"""
无账号 API Key 鉴权与动作级权限，现已支持 OIDC。

角色不是用户账号：key 绑定的是操作者标识、角色、权限和过期时间。
环境变量 key 保持兼容；动态 key 保存于 governance.db，明文只返回一次。
OIDC 启用时，Bearer Token 与 API Key 并存，优先尝试 Bearer。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from dotenv import load_dotenv

from app.errors import AuthError, PermissionDeniedError
from app.governance import is_expired, resolve_dynamic_key
from app.settings import get_settings

_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_ROOT / ".env")


class Role(str, Enum):
    ADMIN = "admin"
    CHANGE_REQUESTER = "change_requester"
    APPROVER = "approver"
    EXECUTOR = "executor"
    AUDITOR = "auditor"
    USER = "user"
    READONLY = "readonly"


DEFAULT_PERMISSIONS: dict[Role, frozenset[str]] = {
    Role.ADMIN: frozenset({"*"}),
    Role.CHANGE_REQUESTER: frozenset({"knowledge.read", "change.submit", "temp_file.upload", "temp_file.search"}),
    Role.APPROVER: frozenset({"knowledge.read", "change.approve", "audit.read", "temp_file.upload", "temp_file.search"}),
    Role.EXECUTOR: frozenset({"knowledge.read", "change.execute", "change.rollback", "audit.read", "temp_file.upload", "temp_file.search"}),
    Role.AUDITOR: frozenset({"knowledge.read", "audit.read", "temp_file.upload", "temp_file.search"}),
    Role.USER: frozenset({"knowledge.read", "temp_file.upload", "temp_file.search"}),
    Role.READONLY: frozenset({"knowledge.read", "temp_file.upload", "temp_file.search"}),
}


@dataclass(frozen=True)
class Principal:
    key_id: str
    role: Role
    operator: str = "unknown"
    permissions: frozenset[str] = field(default_factory=frozenset)
    expires_at: str | None = None
    source: str = "env"
    subject: str | None = None
    groups: frozenset[str] = field(default_factory=frozenset)

    def can(self, permission: str) -> bool:
        return "*" in self.permissions or permission in self.permissions


def _env_specs() -> tuple[tuple[str, str, str], ...]:
    return (
        ("KB_API_KEY_ADMIN", "KB_API_KEY_ADMIN_OPERATOR", "admin"),
        ("KB_API_KEY_CHANGE_REQUESTER", "KB_API_KEY_CHANGE_REQUESTER_OPERATOR", "change_requester"),
        ("KB_API_KEY_APPROVER", "KB_API_KEY_APPROVER_OPERATOR", "approver"),
        ("KB_API_KEY_EXECUTOR", "KB_API_KEY_EXECUTOR_OPERATOR", "executor"),
        ("KB_API_KEY_AUDITOR", "KB_API_KEY_AUDITOR_OPERATOR", "auditor"),
        ("KB_API_KEY_USER", "KB_API_KEY_USER_OPERATOR", "user"),
        ("KB_API_KEY_READONLY", "KB_API_KEY_READONLY_OPERATOR", "readonly"),
    )


def _keys() -> tuple[str, ...]:
    return tuple(os.getenv(name, "").strip() for name, _, _ in _env_specs())


def auth_configured() -> bool:
    if any(_keys()):
        return True
    try:
        from app.governance import has_dynamic_key

        return has_dynamic_key()
    except Exception:
        return False


def _expired_or_config_error(expires_at: str | None, *, key_id: str) -> bool:
    try:
        return is_expired(expires_at)
    except ValueError as exc:
        raise AuthError(
            "API Key 过期时间配置无效",
            stage="auth.config",
            details={"key_id": key_id, "expires_at": expires_at},
            cause=exc,
        ) from exc


def _principal_from_env(index: int, key: str) -> Principal:
    key_name, operator_name, role_name = _env_specs()[index]
    expires_name = f"{key_name}_EXPIRES_AT"
    expires_at = os.getenv(expires_name, "").strip() or None
    if _expired_or_config_error(expires_at, key_id=f"env-{role_name}"):
        raise AuthError(
            "API Key 已过期",
            stage="auth.expired",
            details={"key_id": f"env-{role_name}", "expires_at": expires_at},
        )
    role = Role(role_name)
    operator = os.getenv(operator_name, "").strip() or f"env-{role_name}"
    groups = frozenset(
        item.strip()
        for item in os.getenv(f"{key_name}_GROUPS", "").split(",")
        if item.strip()
    )
    return Principal(
        key_id=f"env-{role_name}",
        role=role,
        operator=operator,
        permissions=DEFAULT_PERMISSIONS[role],
        expires_at=expires_at,
        source="env",
        subject=f"api-key:env-{role_name}",
        groups=groups,
    )


def _principal_from_dynamic(row: dict[str, object]) -> Principal:
    try:
        role = Role(str(row["role"]))
    except ValueError as exc:
        raise AuthError("API Key 角色配置无效", stage="auth.config") from exc
    if _expired_or_config_error(str(row.get("expires_at") or "") or None, key_id=str(row.get("key_id"))):
        raise AuthError(
            "API Key 已过期",
            stage="auth.expired",
            details={"key_id": row.get("key_id"), "expires_at": row.get("expires_at")},
        )
    raw = row.get("permissions_json") or "[]"
    try:
        permissions = frozenset(json.loads(str(raw)))
    except (TypeError, ValueError) as exc:
        raise AuthError("API Key 权限配置无效", stage="auth.config") from exc
    return Principal(
        key_id=str(row["key_id"]),
        role=role,
        operator=str(row["operator"]),
        permissions=permissions or DEFAULT_PERMISSIONS[role],
        expires_at=str(row.get("expires_at") or "") or None,
        source="database",
        subject=f"api-key:{row['key_id']}",
        groups=frozenset(
            item.strip()
            for item in str(row.get("groups") or "").split(",")
            if item.strip()
        ),
    )


def resolve_principal_from_bearer(bearer_token: str) -> Principal | None:
    """尝试从 Bearer Token 解析 OIDC 会话，失败返回 None。"""
    settings = get_settings()
    if not settings.oidc or not settings.oidc.enabled:
        return None
    try:
        from app.oidc import verify_session_token
        return verify_session_token(bearer_token)
    except Exception:
        return None


def resolve_principal(api_key: str | None, bearer: str | None = None) -> Principal:
    """优先 Bearer Token（OIDC），其次 API Key，最后开发兜底。"""
    settings = get_settings()
    
    # 1. 优先尝试 Bearer Token
    if bearer:
        principal = resolve_principal_from_bearer(bearer)
        if principal:
            return principal
        # OIDC 开启时，不能把无效 Bearer 降级为开发管理员。
        if settings.oidc and settings.oidc.enabled and not (api_key or "").strip():
            raise AuthError("无效或过期的 Bearer Token", stage="auth.resolve")
    key = (api_key or "").strip()
    if not key:
        if settings.require_api_key or auth_configured():
            raise AuthError("无效或缺失 API Key", stage="auth.resolve", details={"has_key": False})
        return Principal(
            "dev",
            Role.ADMIN,
            operator="development",
            permissions=DEFAULT_PERMISSIONS[Role.ADMIN],
            source="development",
        )

    dynamic = resolve_dynamic_key(key)
    if dynamic:
        return _principal_from_dynamic(dynamic)

    for index, candidate in enumerate(_keys()):
        if candidate and key == candidate:
            return _principal_from_env(index, key)

    if settings.require_api_key or auth_configured():
        raise AuthError("无效或缺失 API Key", stage="auth.resolve", details={"has_key": True})
    raise AuthError("无效 API Key", stage="auth.resolve")



def local_admin_principal(operator: str = "local-cli") -> Principal:
    """本机 CLI 的显式操作者；不等同于 HTTP 开发兜底。"""
    return Principal(
        key_id=f"cli:{operator}",
        role=Role.ADMIN,
        operator=operator,
        permissions=DEFAULT_PERMISSIONS[Role.ADMIN],
        source="cli",
        subject=f"cli:{operator}",
    )


def require_role(principal: Principal, *allowed: Role) -> None:
    """要求 Principal 的角色在 allowed 集合内，否则抛 403。"""
    if principal.role not in allowed:
        raise PermissionDeniedError(
            f"角色 {principal.role.value} 无权执行，需要 {[r.value for r in allowed]}",
            stage="auth.role",
            details={"role": principal.role.value, "allowed": [r.value for r in allowed]},
        )


def require_permission(principal: Principal, permission: str) -> None:
    if not principal.can(permission):
        raise PermissionDeniedError(
            f"操作者没有 {permission} 权限",
            stage="auth.permission",
            details={"operator": principal.operator, "role": principal.role.value, "permission": permission},
        )


def scoped_session_id(principal: Principal | None, session_id: str | None) -> str:
    """按调用者隔离会话。

    会话记忆与上传工作区都按返回的 id 落盘；不同调用者即使传入
    相同的 session_id，也会落到各自独立的命名空间，避免读到对方
    的记忆或附件（BOLA 防护）。
    """
    raw = (session_id or "").strip() or "default"
    if principal is None:
        return raw
    return f"{principal.key_id}:{raw}"
