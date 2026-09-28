"""知识库文件变更状态机：提交 → 审批 → 执行 → 回滚。"""
from __future__ import annotations

import hashlib
import os
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from app.auth import Principal, require_permission
from app.errors import ChangeNotFoundError, ValidationError, WorkflowError
from app.governance import (
    audit_event,
    create_change_record,
    get_change,
    iso_now,
    list_changes,
    request_retention_days,
    update_change,
    utc_now,
)
from app.logging_setup import get_logger
from app.locks import exclusive

logger = get_logger(__name__)
_MAX_CONTENT_BYTES = 10 * 1024 * 1024


def _raw_root() -> Path:
    from rag.config import Config

    root = Config.RAW_DOCS_DIR.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _target_path(target_path: str) -> tuple[str, Path]:
    relative = (target_path or "").strip().replace("\\", "/")
    if not relative or relative.startswith("/") or "\x00" in relative:
        raise ValidationError("target_path 必须是 data/raw 下的相对路径", stage="change.target")
    root = _raw_root()
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValidationError("target_path 不能越过 data/raw 边界", stage="change.target") from exc
    return target.relative_to(root).as_posix(), target


def _digest(content: str | None) -> str | None:
    return hashlib.sha256(content.encode("utf-8")).hexdigest() if content is not None else None


def _read_target(path: Path) -> str | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise WorkflowError("target_path 不是普通文件", stage="change.target")
    raw = path.read_bytes()
    if len(raw) > _MAX_CONTENT_BYTES:
        raise WorkflowError("目标文件超过变更大小限制", stage="change.target")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkflowError("知识库变更文件必须是 UTF-8 文本", stage="change.target", cause=exc) from exc


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".pending", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def submit_change(
    *,
    operation: str,
    target_path: str,
    after_content: str | None,
    principal: Principal,
    request_id: str,
    source_ip: str,
) -> dict[str, Any]:
    require_permission(principal, "change.submit")
    if operation not in {"upsert", "delete"}:
        raise ValidationError("operation 只能是 upsert 或 delete", stage="change.submit")
    relative, target = _target_path(target_path)
    if operation == "upsert" and after_content is None:
        raise ValidationError("upsert 必须提供 after_content", stage="change.submit")
    if after_content is not None and len(after_content.encode("utf-8")) > _MAX_CONTENT_BYTES:
        raise ValidationError("after_content 超过 10MB 限制", stage="change.submit")
    before = _read_target(target)
    change_id = f"chg_{uuid.uuid4().hex}"
    now = iso_now()
    record = {
        "change_id": change_id,
        "operation": operation,
        "target_path": relative,
        "before_content": before,
        "after_content": after_content,
        "before_hash": _digest(before),
        "after_hash": _digest(after_content),
        "status": "pending_approval",
        "submitter_key_id": principal.key_id,
        "submitter_operator": principal.operator,
        "submitted_at": now,
        "retention_until": (utc_now() + timedelta(days=request_retention_days())).isoformat(timespec="seconds"),
    }
    create_change_record(record)
    event_id = audit_event(
        request_id=request_id,
        action="change.submit",
        status="success",
        operator=principal.operator,
        key_id=principal.key_id,
        source_ip=source_ip,
        target_id=change_id,
        before_hash=record["before_hash"],
        after_hash=record["after_hash"],
        before_content=before,
        after_content=after_content,
    )
    return {"ok": True, "change": get_change(change_id), "audit_event_id": event_id}


def approve_change(*, change_id: str, principal: Principal, request_id: str, source_ip: str) -> dict[str, Any]:
    require_permission(principal, "change.approve")
    change = get_change(change_id)
    if not change:
        raise ChangeNotFoundError(details={"change_id": change_id}, stage="change.approve")
    if change["status"] != "pending_approval":
        raise WorkflowError("只有 pending_approval 可以审批", stage="change.approve", details={"status": change["status"]})
    if change["submitter_key_id"] == principal.key_id:
        raise WorkflowError("提交者不能审批自己的变更", stage="change.approve")
    now = iso_now()
    updated = update_change(change_id, {
        "status": "approved",
        "approver_key_id": principal.key_id,
        "approver_operator": principal.operator,
        "approved_at": now,
    }, expected_status="pending_approval")
    if not updated:
        latest = get_change(change_id)
        raise WorkflowError(
            "变更状态已被其他审批请求更新",
            stage="change.approve",
            details={"status": latest["status"] if latest else "missing"},
        )
    event_id = audit_event(
        request_id=request_id,
        action="change.approve",
        status="success",
        operator=principal.operator,
        key_id=principal.key_id,
        source_ip=source_ip,
        target_id=change_id,
        before_hash=change["before_hash"],
        after_hash=change["after_hash"],
        before_content=change["before_content"],
        after_content=change["after_content"],
    )
    return {"ok": True, "change": get_change(change_id), "audit_event_id": event_id}


def execute_change(*, change_id: str, principal: Principal, request_id: str, source_ip: str) -> dict[str, Any]:
    # 状态检查、文件校验、原子写入和状态落库必须属于同一资源临界区。
    with exclusive(f"change_{change_id}", timeout=60):
        return _execute_change(
            change_id=change_id,
            principal=principal,
            request_id=request_id,
            source_ip=source_ip,
        )


def _execute_change(*, change_id: str, principal: Principal, request_id: str, source_ip: str) -> dict[str, Any]:
    require_permission(principal, "change.execute")
    change = get_change(change_id)
    if not change:
        raise ChangeNotFoundError(details={"change_id": change_id}, stage="change.execute")
    if change["status"] != "approved":
        raise WorkflowError("只有 approved 可以执行", stage="change.execute", details={"status": change["status"]})
    relative, target = _target_path(change["target_path"])
    try:
        current = _read_target(target)
        if _digest(current) != change["before_hash"]:
            raise WorkflowError("目标文件在审批后已变化，拒绝执行", stage="change.execute", details={"target_path": relative})
        if change["operation"] == "delete":
            if target.exists():
                target.unlink()
        else:
            _write_atomic(target, change["after_content"] or "")
        now = iso_now()
        updated = update_change(change_id, {
            "status": "executed",
            "executor_key_id": principal.key_id,
            "executor_operator": principal.operator,
            "executed_at": now,
            "failure_reason": None,
        }, expected_status="approved")
        if not updated:
            latest = get_change(change_id)
            raise WorkflowError(
                "变更状态已被其他执行请求更新",
                stage="change.execute",
                details={"status": latest["status"] if latest else "missing"},
            )
        event_id = audit_event(
            request_id=request_id,
            action="change.execute",
            status="success",
            operator=principal.operator,
            key_id=principal.key_id,
            source_ip=source_ip,
            target_id=change_id,
            before_hash=change["before_hash"],
            after_hash=change["after_hash"],
            before_content=change["before_content"],
            after_content=change["after_content"],
        )
        return {"ok": True, "change": get_change(change_id), "audit_event_id": event_id}
    except Exception as exc:
        reason = str(exc) or type(exc).__name__
        update_change(change_id, {"status": "failed", "failure_reason": reason}, expected_status="approved")
        audit_event(
            request_id=request_id,
            action="change.execute",
            status="failed",
            operator=principal.operator,
            key_id=principal.key_id,
            source_ip=source_ip,
            target_id=change_id,
            before_hash=change["before_hash"],
            after_hash=change["after_hash"],
            before_content=change["before_content"],
            after_content=change["after_content"],
            failure_reason=reason,
        )
        if isinstance(exc, WorkflowError):
            raise
        raise WorkflowError("变更执行失败", stage="change.execute", cause=exc, details={"failure_reason": reason}) from exc


def rollback_change(*, change_id: str, principal: Principal, request_id: str, source_ip: str) -> dict[str, Any]:
    # 回滚同样需要防止与执行或另一个回滚请求交叉修改目标文件。
    with exclusive(f"change_{change_id}", timeout=60):
        return _rollback_change(
            change_id=change_id,
            principal=principal,
            request_id=request_id,
            source_ip=source_ip,
        )


def _rollback_change(*, change_id: str, principal: Principal, request_id: str, source_ip: str) -> dict[str, Any]:
    require_permission(principal, "change.rollback")
    change = get_change(change_id)
    if not change:
        raise ChangeNotFoundError(details={"change_id": change_id}, stage="change.rollback")
    if change["status"] != "executed":
        raise WorkflowError("只有 executed 可以回滚", stage="change.rollback", details={"status": change["status"]})
    _, target = _target_path(change["target_path"])
    try:
        current = _read_target(target)
        if _digest(current) != change["after_hash"]:
            raise WorkflowError("目标文件已偏离执行结果，拒绝回滚", stage="change.rollback")
        if change["before_content"] is None:
            if target.exists():
                target.unlink()
        else:
            _write_atomic(target, change["before_content"])
        updated = update_change(change_id, {
            "status": "rolled_back",
            "rollback_key_id": principal.key_id,
            "rollback_operator": principal.operator,
            "rolled_back_at": iso_now(),
        }, expected_status="executed")
        if not updated:
            latest = get_change(change_id)
            raise WorkflowError(
                "变更状态已被其他回滚请求更新",
                stage="change.rollback",
                details={"status": latest["status"] if latest else "missing"},
            )
        event_id = audit_event(
            request_id=request_id,
            action="change.rollback",
            status="success",
            operator=principal.operator,
            key_id=principal.key_id,
            source_ip=source_ip,
            target_id=change_id,
            before_hash=change["after_hash"],
            after_hash=change["before_hash"],
            before_content=change["after_content"],
            after_content=change["before_content"],
        )
        return {"ok": True, "change": get_change(change_id), "audit_event_id": event_id}
    except Exception as exc:
        reason = str(exc) or type(exc).__name__
        update_change(change_id, {"failure_reason": reason}, expected_status="executed")
        audit_event(
            request_id=request_id,
            action="change.rollback",
            status="failed",
            operator=principal.operator,
            key_id=principal.key_id,
            source_ip=source_ip,
            target_id=change_id,
            before_hash=change["after_hash"],
            after_hash=change["before_hash"],
            before_content=change["after_content"],
            after_content=change["before_content"],
            failure_reason=reason,
        )
        if isinstance(exc, WorkflowError):
            raise
        raise WorkflowError("变更回滚失败", stage="change.rollback", cause=exc, details={"failure_reason": reason}) from exc


def get_changes(status: str | None = None, limit: int = 100) -> dict[str, Any]:
    return {"ok": True, "items": list_changes(status=status, limit=limit)}
