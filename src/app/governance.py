"""治理持久化：API Key、变更记录与哈希链审计。

设计约束：
- 不建立用户账号；操作者由 key 的 operator 字段标识。
- key 只保存 SHA-256 摘要，明文只在创建响应中出现一次。
- audit_log 通过 SQLite 触发器禁止 UPDATE/DELETE，并用前序哈希形成链。
- 审计不提供删除 API；retention_until 只表达保留责任边界。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.logging_setup import get_logger

logger = get_logger(__name__)
_ROOT = Path(__file__).resolve().parents[2]
_DB_LOCK = threading.RLock()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat(timespec="seconds")


def parse_expiry(value: str | None) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError as exc:
        raise ValueError("expires_at 必须是 ISO-8601 时间，例如 2027-01-01T00:00:00Z") from exc


def is_expired(value: str | None) -> bool:
    parsed = parse_expiry(value)
    return parsed is not None and parsed <= utc_now()


def governance_db_path() -> Path:
    raw = os.getenv("KB_GOVERNANCE_DB", "").strip()
    return Path(raw).expanduser() if raw else _ROOT / "data" / "governance.db"


def _connect() -> sqlite3.Connection:
    path = governance_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema() -> None:
    with _DB_LOCK, _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS api_keys (
                key_id TEXT PRIMARY KEY,
                secret_hash TEXT NOT NULL UNIQUE,
                operator TEXT NOT NULL,
                role TEXT NOT NULL,
                permissions_json TEXT NOT NULL,
                expires_at TEXT,
                revoked_at TEXT,
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_api_keys_active ON api_keys(revoked_at, expires_at);

            CREATE TABLE IF NOT EXISTS change_requests (
                change_id TEXT PRIMARY KEY,
                operation TEXT NOT NULL CHECK(operation IN ('upsert', 'delete')),
                target_path TEXT NOT NULL,
                before_content TEXT,
                after_content TEXT,
                before_hash TEXT,
                after_hash TEXT,
                status TEXT NOT NULL,
                submitter_key_id TEXT NOT NULL,
                submitter_operator TEXT NOT NULL,
                submitted_at TEXT NOT NULL,
                approver_key_id TEXT,
                approver_operator TEXT,
                approved_at TEXT,
                executor_key_id TEXT,
                executor_operator TEXT,
                executed_at TEXT,
                rollback_key_id TEXT,
                rollback_operator TEXT,
                rolled_back_at TEXT,
                failure_reason TEXT,
                retention_until TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_change_requests_status ON change_requests(status, submitted_at);

            CREATE TABLE IF NOT EXISTS temp_files (
                file_id TEXT PRIMARY KEY,
                stored_path TEXT NOT NULL UNIQUE,
                original_name TEXT NOT NULL,
                content_type TEXT,
                size_bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                owner_key_id TEXT NOT NULL,
                owner_operator TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_temp_files_expiry ON temp_files(expires_at);

            CREATE TABLE IF NOT EXISTS audit_log (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                request_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                action TEXT NOT NULL,
                status TEXT NOT NULL,
                operator TEXT NOT NULL,
                key_id TEXT NOT NULL,
                source_ip TEXT NOT NULL,
                target_id TEXT,
                before_hash TEXT,
                after_hash TEXT,
                before_content TEXT,
                after_content TEXT,
                failure_reason TEXT,
                retention_until TEXT NOT NULL,
                prev_hash TEXT NOT NULL,
                record_hash TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_audit_occurred_at ON audit_log(occurred_at);
            CREATE INDEX IF NOT EXISTS idx_audit_target ON audit_log(target_id);

            CREATE TRIGGER IF NOT EXISTS audit_no_update
            BEFORE UPDATE ON audit_log
            BEGIN
                SELECT RAISE(ABORT, 'audit_log is append-only');
            END;
            CREATE TRIGGER IF NOT EXISTS audit_no_delete
            BEFORE DELETE ON audit_log
            BEGIN
                SELECT RAISE(ABORT, 'audit_log is append-only');
            END;
            """
        )


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def audit_event(
    *,
    request_id: str,
    action: str,
    status: str,
    operator: str,
    key_id: str,
    source_ip: str,
    target_id: str | None = None,
    before_hash: str | None = None,
    after_hash: str | None = None,
    before_content: str | None = None,
    after_content: str | None = None,
    failure_reason: str | None = None,
    retention_days: int | None = None,
) -> str:
    """追加一条不可变审计记录，返回 event_id。"""
    init_schema()
    retention = max(1, retention_days or int(os.getenv("KB_AUDIT_RETENTION_DAYS", "365")))
    now = iso_now()
    retention_until = (utc_now() + timedelta(days=retention)).isoformat(timespec="seconds")
    event_id = f"evt_{uuid.uuid4().hex}"
    with _DB_LOCK, _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            previous = conn.execute(
                "SELECT record_hash FROM audit_log ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            prev_hash = previous["record_hash"] if previous else "GENESIS"
            payload = {
                "event_id": event_id,
                "request_id": request_id,
                "occurred_at": now,
                "action": action,
                "status": status,
                "operator": operator,
                "key_id": key_id,
                "source_ip": source_ip,
                "target_id": target_id,
                "before_hash": before_hash,
                "after_hash": after_hash,
                "before_content": before_content,
                "after_content": after_content,
                "failure_reason": failure_reason,
                "retention_until": retention_until,
                "prev_hash": prev_hash,
            }
            record_hash = hashlib.sha256(
                f"{prev_hash}|{_canonical(payload)}".encode("utf-8")
            ).hexdigest()
            conn.execute(
                """INSERT INTO audit_log (
                    event_id, request_id, occurred_at, action, status, operator, key_id,
                    source_ip, target_id, before_hash, after_hash, before_content,
                    after_content, failure_reason, retention_until, prev_hash, record_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    request_id,
                    now,
                    action,
                    status,
                    operator,
                    key_id,
                    source_ip,
                    target_id,
                    before_hash,
                    after_hash,
                    before_content,
                    after_content,
                    failure_reason,
                    retention_until,
                    prev_hash,
                    record_hash,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return event_id


def verify_audit_chain() -> dict[str, Any]:
    init_schema()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY seq ASC").fetchall()
    previous = "GENESIS"
    for row in rows:
        payload = {
            "event_id": row["event_id"],
            "request_id": row["request_id"],
            "occurred_at": row["occurred_at"],
            "action": row["action"],
            "status": row["status"],
            "operator": row["operator"],
            "key_id": row["key_id"],
            "source_ip": row["source_ip"],
            "target_id": row["target_id"],
            "before_hash": row["before_hash"],
            "after_hash": row["after_hash"],
            "before_content": row["before_content"],
            "after_content": row["after_content"],
            "failure_reason": row["failure_reason"],
            "retention_until": row["retention_until"],
            "prev_hash": row["prev_hash"],
        }
        expected = hashlib.sha256(
            f"{previous}|{_canonical(payload)}".encode("utf-8")
        ).hexdigest()
        if row["prev_hash"] != previous or not hmac.compare_digest(row["record_hash"], expected):
            return {"ok": False, "verified": row["seq"] - 1, "broken_at": row["seq"]}
        previous = row["record_hash"]
    return {"ok": True, "verified": len(rows), "last_hash": previous}


def rows_as_dict(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def create_change_record(data: dict[str, Any]) -> None:
    init_schema()
    columns = tuple(data.keys())
    placeholders = ", ".join("?" for _ in columns)
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            f"INSERT INTO change_requests ({', '.join(columns)}) VALUES ({placeholders})",
            tuple(data[column] for column in columns),
        )


def update_change(
    change_id: str,
    values: dict[str, Any],
    *,
    expected_status: str | None = None,
) -> bool:
    if not values:
        return False
    init_schema()
    assignments = ", ".join(f"{column} = ?" for column in values)
    where = "change_id = ?"
    params: list[Any] = [*values.values(), change_id]
    if expected_status is not None:
        where += " AND status = ?"
        params.append(expected_status)
    with _DB_LOCK, _connect() as conn:
        result = conn.execute(
            f"UPDATE change_requests SET {assignments} WHERE {where}",
            tuple(params),
        )
    return result.rowcount == 1


def list_audit_events(*, target_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    init_schema()
    limit = min(max(1, limit), 500)
    with _DB_LOCK, _connect() as conn:
        if target_id:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE target_id = ? ORDER BY seq DESC LIMIT ?",
                (target_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY seq DESC LIMIT ?", (limit,)
            ).fetchall()
    return rows_as_dict(rows)


def purge_expired_temp_files() -> list[dict[str, Any]]:
    """只清理临时文件元数据，调用方负责删除实际文件并保留审计。"""
    init_schema()
    now = iso_now()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM temp_files WHERE expires_at <= ?", (now,)
        ).fetchall()
        if rows:
            conn.executemany(
                "DELETE FROM temp_files WHERE file_id = ?",
                [(row["file_id"],) for row in rows],
            )
    return rows_as_dict(rows)


def register_temp_file(data: dict[str, Any]) -> None:
    init_schema()
    columns = tuple(data.keys())
    placeholders = ", ".join("?" for _ in columns)
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            f"INSERT INTO temp_files ({', '.join(columns)}) VALUES ({placeholders})",
            tuple(data[column] for column in columns),
        )


def get_temp_file(file_id: str) -> dict[str, Any] | None:
    init_schema()
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM temp_files WHERE file_id = ?", (file_id,)
        ).fetchone()
    return dict(row) if row else None
def get_change(change_id: str) -> dict[str, Any] | None:
    init_schema()
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM change_requests WHERE change_id = ?", (change_id,)
        ).fetchone()
    return dict(row) if row else None


def list_changes(status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    init_schema()
    limit = min(max(1, limit), 500)
    with _DB_LOCK, _connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM change_requests WHERE status = ? ORDER BY submitted_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM change_requests ORDER BY submitted_at DESC LIMIT ?", (limit,)
            ).fetchall()
    return rows_as_dict(rows)


def resolve_dynamic_key(secret: str) -> dict[str, Any] | None:
    init_schema()
    digest = hash_secret(secret)
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT * FROM api_keys WHERE secret_hash = ? AND revoked_at IS NULL",
            (digest,),
        ).fetchone()
    if not row:
        return None
    return dict(row)


def has_dynamic_key() -> bool:
    init_schema()
    with _DB_LOCK, _connect() as conn:
        return conn.execute(
            "SELECT 1 FROM api_keys WHERE revoked_at IS NULL LIMIT 1"
        ).fetchone() is not None


def create_dynamic_key(
    *,
    operator: str,
    role: str,
    permissions: list[str],
    expires_at: str | None,
    created_by: str,
) -> dict[str, Any]:
    init_schema()
    secret = f"kb_{secrets.token_urlsafe(32)}"
    key_id = f"key_{secrets.token_hex(8)}"
    created = iso_now()
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            """INSERT INTO api_keys (
                key_id, secret_hash, operator, role, permissions_json,
                expires_at, revoked_at, created_at, created_by
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
            (
                key_id,
                hash_secret(secret),
                operator,
                role,
                json.dumps(sorted(set(permissions)), ensure_ascii=False),
                expires_at,
                created,
                created_by,
            ),
        )
    return {
        "key_id": key_id,
        "api_key": secret,
        "operator": operator,
        "role": role,
        "permissions": sorted(set(permissions)),
        "expires_at": expires_at,
        "created_at": created,
    }


def revoke_dynamic_key(key_id: str, revoked_by: str) -> bool:
    init_schema()
    with _DB_LOCK, _connect() as conn:
        result = conn.execute(
            "UPDATE api_keys SET revoked_at = ? WHERE key_id = ? AND revoked_at IS NULL",
            (iso_now(), key_id),
        )
    return result.rowcount == 1


def list_dynamic_keys() -> list[dict[str, Any]]:
    init_schema()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT key_id, operator, role, permissions_json, expires_at,
                      revoked_at, created_at, created_by
               FROM api_keys ORDER BY created_at DESC"""
        ).fetchall()
    output = []
    for row in rows:
        item = dict(row)
        item["permissions"] = json.loads(item.pop("permissions_json"))
        output.append(item)
    return output


def request_retention_days() -> int:
    try:
        return max(1, int(os.getenv("KB_AUDIT_RETENTION_DAYS", "365")))
    except ValueError:
        return 365
