"""文档与结构化数据的访问控制边界。

ACL 必须在重排、摘要、Prompt 拼接和模型调用之前生效。文档元数据支持：
- owner_key_id / owner_principal_id / owner_subject / owner_operator
- allowed_key_ids / allowed_principal_ids / allowed_subjects
- allowed_groups / allowed_roles
- visibility=public|private|restricted

未标记文档的兼容策略由 ``KB_ACL_UNTAGGED_POLICY`` 控制：public（默认）或 deny。
SQL 表使用同一套字段，策略通过 ``KB_SQL_TABLE_ACL_JSON`` 配置；未标记表由
``KB_SQL_ACL_UNTAGGED_POLICY`` 控制。管理员默认可绕过 ACL，可用
``KB_ACL_ADMIN_BYPASS=0`` 关闭。
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterable
from typing import Any

from app.auth import Principal, Role

_ACL_KEYS = {
    "visibility",
    "owner_key_id",
    "owner_principal_id",
    "owner_subject",
    "owner_operator",
    "allowed_key_ids",
    "allowed_principal_ids",
    "allowed_subjects",
    "allowed_groups",
    "allowed_roles",
}


def _values(value: Any) -> set[str]:
    """接受逗号分隔字符串或 JSON/原生列表，统一成非空字符串集合。"""
    if isinstance(value, str):
        return {part.strip() for part in value.split(",") if part.strip()}
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, dict)):
        return {str(item).strip() for item in value if str(item).strip()}
    return set()


def _policy(name: str, default: str) -> bool:
    value = os.getenv(name, default).strip().lower()
    if value not in {"public", "deny"}:
        raise ValueError(f"{name} must be public or deny")
    return value == "public"


def _untagged_is_visible() -> bool:
    return _policy("KB_ACL_UNTAGGED_POLICY", "public")


def _admin_bypasses() -> bool:
    return os.getenv("KB_ACL_ADMIN_BYPASS", "1").strip().lower() in {"1", "true", "yes", "on"}


def _is_admin(principal: Principal | None) -> bool:
    return bool(
        principal
        and _admin_bypasses()
        and (principal.role is Role.ADMIN or principal.can("*"))
    )


def _principal_ids(principal: Principal | None) -> set[str]:
    if principal is None:
        return set()
    values = {principal.key_id, principal.operator}
    if principal.subject:
        values.add(principal.subject)
    return {value for value in values if value}


def acl_metadata_is_visible(metadata: dict[str, Any], principal: Principal | None) -> bool:
    """判断一个已标记或未标记资源是否对 Principal 可读。"""
    if _is_admin(principal):
        return True
    if not any(key in metadata for key in _ACL_KEYS):
        return _untagged_is_visible()

    visibility = str(metadata.get("visibility", "private")).strip().lower()
    if visibility == "public":
        return True
    if visibility not in {"private", "restricted"}:
        return False

    principal_ids = _principal_ids(principal)
    groups = set(principal.groups) if principal else set()
    role = principal.role.value if principal else None
    owners: set[str] = set()
    for key in ("owner_key_id", "owner_principal_id", "owner_subject", "owner_operator"):
        owners.update(_values(metadata.get(key)))
    if principal_ids.intersection(owners):
        return True

    allowed_ids: set[str] = set()
    for key in ("allowed_key_ids", "allowed_principal_ids", "allowed_subjects"):
        allowed_ids.update(_values(metadata.get(key)))
    return bool(
        principal_ids.intersection(allowed_ids)
        or groups.intersection(_values(metadata.get("allowed_groups")))
        or (role and role in _values(metadata.get("allowed_roles")))
    )


def filter_chunks_by_acl(
    chunks: list[dict[str, Any]], principal: Principal | None
) -> list[dict[str, Any]]:
    """只保留当前 Principal 可读的块；``None`` 只能读取公开内容。"""
    return [
        chunk
        for chunk in chunks
        if acl_metadata_is_visible(dict(chunk.get("metadata") or {}), principal)
    ]


def _table_policies() -> dict[str, dict[str, Any]]:
    raw = os.getenv("KB_SQL_TABLE_ACL_JSON", "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("KB_SQL_TABLE_ACL_JSON must be a JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("KB_SQL_TABLE_ACL_JSON must be a JSON object")
    return {
        str(name).lower(): dict(metadata)
        for name, metadata in value.items()
        if isinstance(metadata, dict)
    }


def table_access_allowed(table_names: Iterable[str], principal: Principal | None) -> bool:
    """在 SQL 执行前校验其引用的所有表，避免 SQL 结果绕过 ACL。

    管理员默认绕过（与 ``acl_metadata_is_visible`` 一致，可用
    ``KB_ACL_ADMIN_BYPASS=0`` 关闭）。
    """
    if _is_admin(principal):
        return True
    policies = _table_policies()
    for raw_name in table_names:
        name = str(raw_name).strip().lower()
        if not name:
            continue
        if name.startswith("sqlite_") and not _is_admin(principal):
            return False
        metadata = policies.get(name)
        if metadata is None:
            if not _policy("KB_SQL_ACL_UNTAGGED_POLICY", "public"):
                return False
            continue
        if not acl_metadata_is_visible(metadata, principal):
            return False
    return True
