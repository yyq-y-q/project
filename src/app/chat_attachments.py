"""
对话附件：落盘到 session 工作区，并按后缀分类。

  tables  → 立刻 sqlite ingest（进全局 query.db）
  docs    → session 目录；本轮可注入上下文 / 可选进 raw 重建
  codes   → session 目录，供 Agent file_Read
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, BinaryIO

from app.chat_router import AttachmentSummary
from app.errors import ValidationError
from app.logging_setup import get_logger

logger = get_logger(__name__)

_SAFE_NAME = re.compile(r"[^a-zA-Z0-9._\-\u4e00-\u9fff]+")

_TABLE_EXT = {".xlsx", ".xlsm", ".csv"}
_DOC_EXT = {".txt", ".md", ".markdown", ".docx", ".pdf"}
_CODE_EXT = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".toml", ".yml", ".yaml",
    ".go", ".rs", ".java", ".kt", ".c", ".cpp", ".h", ".hpp", ".cs",
    ".sql", ".sh", ".bash", ".ps1", ".html", ".css", ".vue", ".svelte",
    ".xml", ".ini", ".cfg", ".env.example", ".gitignore", ".dockerfile",
}
_CODE_NAMES = {"dockerfile", "makefile", "readme", "license", "cmakelists.txt"}

# 单文件上限 8MB；单次最多 20 个
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_FILES = 20


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def session_workspace(session_id: str) -> Path:
    from rag.memory import normalize_session_id

    sid = normalize_session_id(session_id)
    root = _project_root() / "data" / "chat_uploads" / sid
    root.mkdir(parents=True, exist_ok=True)
    return root


def safe_filename(name: str) -> str:
    base = Path(name or "file").name
    cleaned = _SAFE_NAME.sub("_", base).strip("._") or "file"
    return cleaned[:180]


def classify_name(filename: str) -> str:
    p = Path(filename)
    suf = p.suffix.lower()
    stem = p.name.lower()
    if suf in _TABLE_EXT:
        return "table"
    if suf in _DOC_EXT:
        return "doc"
    if suf in _CODE_EXT or stem in _CODE_NAMES:
        return "code"
    # 无后缀当文本代码/笔记
    if not suf:
        return "code"
    return "other"


def _read_limited(stream: BinaryIO, limit: int = MAX_FILE_BYTES) -> bytes:
    data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValidationError(
            f"单个附件超过 {MAX_FILE_BYTES // (1024 * 1024)}MB 限制",
            stage="chat.attach",
        )
    return data


def save_upload(
    *,
    session_id: str,
    filename: str,
    stream: BinaryIO,
) -> dict[str, Any]:
    """保存一个上传文件，返回元信息。"""
    kind = classify_name(filename)
    name = safe_filename(filename)
    root = session_workspace(session_id)
    dest = root / name
    # 重名加序号
    if dest.exists():
        stem, suf = dest.stem, dest.suffix
        n = 1
        while True:
            cand = root / f"{stem}_{n}{suf}"
            if not cand.exists():
                dest = cand
                break
            n += 1

    raw = _read_limited(stream)
    dest.write_bytes(raw)
    return {
        "kind": kind,
        "filename": dest.name,
        "path": str(dest),
        "relpath": dest.name,
        "bytes": len(raw),
    }


def materialize_attachments(
    *,
    session_id: str,
    files: list[tuple[str, BinaryIO]],
    ingest_tables: bool = True,
) -> tuple[AttachmentSummary, list[dict[str, Any]]]:
    """
    files: [(filename, binary_io), ...]
    返回 (summary, details)
    """
    if len(files) > MAX_FILES:
        raise ValidationError(
            f"附件最多 {MAX_FILES} 个",
            stage="chat.attach",
            details={"count": len(files)},
        )

    summary = AttachmentSummary(count=len(files))
    details: list[dict[str, Any]] = []

    for filename, stream in files:
        meta = save_upload(session_id=session_id, filename=filename, stream=stream)
        kind = meta["kind"]
        details.append(meta)
        summary.workspace_relpaths.append(meta["relpath"])

        if kind == "table":
            summary.tables.append(meta["relpath"])
            if ingest_tables:
                try:
                    from sqlite_tool.ingest import run_ingest

                    ing = run_ingest(meta["path"], replace=True)
                    meta["ingest"] = {
                        "ok": True,
                        "table": ing.get("table"),
                        "rowcount": (ing.get("load") or {}).get("rowcount"),
                    }
                    summary.notes.append(
                        f"已导入表 {ing.get('table')} ← {meta['relpath']}"
                    )
                except Exception as e:
                    logger.exception("attach table ingest fail")
                    meta["ingest"] = {"ok": False, "error": str(e)}
                    summary.notes.append(f"表导入失败 {meta['relpath']}: {e}")
        elif kind == "doc":
            summary.docs.append(meta["relpath"])
        elif kind == "code":
            summary.codes.append(meta["relpath"])
        else:
            summary.others.append(meta["relpath"])

    return summary, details


def paste_project_bundle(
    *,
    session_id: str,
    message: str,
) -> tuple[str, AttachmentSummary, list[dict[str, Any]]]:
    """
    从消息中拆出 ```path 或 ```lang 大块，写成 session 文件。
    返回 (清理后的用户说明, summary, details)
    """
    text = message or ""
    summary = AttachmentSummary()
    details: list[dict[str, Any]] = []
    root = session_workspace(session_id)

    fence = re.compile(
        r"```([^\n`]*)\n([\s\S]*?)```",
        re.MULTILINE,
    )
    kept_parts: list[str] = []
    last = 0
    file_idx = 0
    for m in fence.finditer(text):
        kept_parts.append(text[last : m.start()])
        header = (m.group(1) or "").strip()
        body = m.group(2) or ""
        last = m.end()

        rel = None
        if header and ("/" in header or "\\" in header or "." in header):
            tokens = header.split()
            for t in reversed(tokens):
                if "." in t or "/" in t or "\\" in t:
                    rel = t.strip().lstrip("./")
                    break
        if rel and len(body.strip()) >= 1:
            rel = rel.replace("\\", "/")
            parts = [p for p in rel.split("/") if p and p != ".."]
            if not parts:
                kept_parts.append(m.group(0))
                continue
            dest = root.joinpath(*parts)
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(body, encoding="utf-8")
            except Exception as e:
                summary.notes.append(f"粘贴块写入失败 {rel}: {e}")
                kept_parts.append(m.group(0))
                continue
            kind = classify_name(dest.name)
            meta = {
                "kind": kind,
                "filename": dest.name,
                "path": str(dest),
                "relpath": str(Path(*parts)).replace("\\", "/"),
                "bytes": len(body.encode("utf-8")),
                "from": "paste",
            }
            details.append(meta)
            summary.count += 1
            summary.workspace_relpaths.append(meta["relpath"])
            if kind == "table":
                summary.tables.append(meta["relpath"])
            elif kind == "doc":
                summary.docs.append(meta["relpath"])
            elif kind == "code":
                summary.codes.append(meta["relpath"])
            else:
                summary.others.append(meta["relpath"])
            file_idx += 1
            kept_parts.append(f"\n[已保存附件: {meta['relpath']}]\n")
        else:
            if len(body.strip()) < 40:
                kept_parts.append(m.group(0))
                continue
            lang = (header.split()[0] if header else "txt").lower()
            ext_map = {
                "python": ".py",
                "py": ".py",
                "javascript": ".js",
                "js": ".js",
                "ts": ".ts",
                "typescript": ".ts",
                "json": ".json",
                "sql": ".sql",
                "bash": ".sh",
                "shell": ".sh",
                "html": ".html",
                "css": ".css",
                "md": ".md",
                "markdown": ".md",
            }
            ext = ext_map.get(lang, ".txt")
            file_idx += 1
            name = f"snippet_{file_idx}{ext}"
            dest = root / name
            dest.write_text(body, encoding="utf-8")
            kind = classify_name(name)
            meta = {
                "kind": kind,
                "filename": name,
                "path": str(dest),
                "relpath": name,
                "bytes": len(body.encode("utf-8")),
                "from": "paste_fence",
            }
            details.append(meta)
            summary.count += 1
            summary.workspace_relpaths.append(name)
            if kind == "code":
                summary.codes.append(name)
            elif kind == "doc":
                summary.docs.append(name)
            else:
                summary.others.append(name)
            kept_parts.append(f"\n[已保存代码块: {name}]\n")

    kept_parts.append(text[last:])
    cleaned = "".join(kept_parts).strip()
    return cleaned, summary, details


def build_attachment_context(summary: AttachmentSummary, limit_chars: int = 6000) -> str:
    """附件清单注入提示。"""
    if not summary.workspace_relpaths:
        return ""
    lines = ["【本轮附件】"]
    if summary.tables:
        lines.append("表文件: " + ", ".join(summary.tables))
    if summary.docs:
        lines.append("文档: " + ", ".join(summary.docs))
    if summary.codes:
        lines.append("代码/项目文件: " + ", ".join(summary.codes))
    if summary.notes:
        lines.extend(summary.notes)
    lines.append("（Agent 可在工作目录用 file_Read 打开上述相对路径）")
    text = "\n".join(lines)
    return text[:limit_chars]
