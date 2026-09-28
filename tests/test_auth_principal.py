"""鉴权与 ACL 相关的回归测试：principal 贯穿、会话隔离、表级访问控制。"""
from __future__ import annotations

import pytest

from app.acl import table_access_allowed
from app.auth import (
    DEFAULT_PERMISSIONS,
    Principal,
    Role,
    require_permission,
    require_role,
    scoped_session_id,
)
from app.errors import PermissionDeniedError


def _principal(
    key_id: str = "k-user",
    role: Role = Role.USER,
    *,
    operator: str = "tester",
) -> Principal:
    return Principal(
        key_id=key_id,
        role=role,
        operator=operator,
        permissions=DEFAULT_PERMISSIONS[role],
    )


def _admin() -> Principal:
    return Principal(
        key_id="k-admin",
        role=Role.ADMIN,
        operator="admin",
        permissions=frozenset({"*"}),
    )


# ---------- require_role / require_permission ----------

def test_require_role_admin_allowed():
    assert require_role(_admin(), Role.ADMIN) is None


def test_require_role_user_denied_for_admin():
    with pytest.raises(PermissionDeniedError):
        require_role(_principal(), Role.ADMIN)


def test_require_role_multiple_roles_user_allowed():
    assert require_role(_principal(), Role.ADMIN, Role.USER) is None


def test_require_permission_user_allowed_read():
    assert require_permission(_principal(), "knowledge.read") is None


def test_require_permission_readonly_denied_submit():
    ro = _principal(key_id="k-ro", role=Role.READONLY)
    with pytest.raises(PermissionDeniedError):
        require_permission(ro, "change.submit")


# ---------- 会话按调用者隔离 ----------

def test_scoped_session_id_none_principal_keeps_raw():
    assert scoped_session_id(None, "chat-1") == "chat-1"


def test_scoped_session_id_with_principal_prefixes_key():
    p = _principal(key_id="k-user")
    assert scoped_session_id(p, "chat-1") == "k-user:chat-1"


def test_scoped_session_id_distinguishes_users():
    a = _principal(key_id="k-a")
    b = _principal(key_id="k-b")
    assert scoped_session_id(a, "chat-1") != scoped_session_id(b, "chat-1")


def test_scoped_session_id_empty_falls_back():
    p = _principal(key_id="k-user")
    assert scoped_session_id(p, None).endswith("default")
    assert scoped_session_id(None, "  ").endswith("default")


# ---------- 表级 ACL ----------

def test_acl_untagged_default_public_for_user(monkeypatch):
    monkeypatch.delenv("KB_SQL_TABLE_ACL_JSON", raising=False)
    monkeypatch.delenv("KB_SQL_ACL_UNTAGGED_POLICY", raising=False)
    assert table_access_allowed({"employees"}, _principal()) is True


def test_acl_untagged_deny_blocks_user_but_admin_bypasses(monkeypatch):
    monkeypatch.delenv("KB_SQL_TABLE_ACL_JSON", raising=False)
    monkeypatch.setenv("KB_SQL_ACL_UNTAGGED_POLICY", "deny")
    assert table_access_allowed({"employees"}, _principal()) is False
    assert table_access_allowed({"employees"}, _admin()) is True


def test_acl_policy_json_restricts_table(monkeypatch):
    monkeypatch.setenv(
        "KB_SQL_TABLE_ACL_JSON",
        '{"hr": {"visibility": "restricted", "allowed_roles": "admin"}}',
    )
    monkeypatch.delenv("KB_SQL_ACL_UNTAGGED_POLICY", raising=False)
    assert table_access_allowed({"hr"}, _principal()) is False
    assert table_access_allowed({"hr"}, _admin()) is True
    assert table_access_allowed({"employees"}, _principal()) is True


def test_acl_sqlite_system_table_admin_only(monkeypatch):
    monkeypatch.delenv("KB_SQL_TABLE_ACL_JSON", raising=False)
    monkeypatch.delenv("KB_SQL_ACL_UNTAGGED_POLICY", raising=False)
    assert table_access_allowed({"sqlite_master"}, _principal()) is False
    assert table_access_allowed({"sqlite_master"}, _admin()) is True
