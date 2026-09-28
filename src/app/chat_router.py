"""
对话分流（规则优先，可 force_mode 覆盖）

mode:
  sql     — 纯只读 SELECT
  rag     — 知识库问答（多轮记忆）
  hybrid  — 表 + 知识库确定性融合
  agent   — 多步工具（读改文件 / 终端 / rag / sql）
  chat    — 轻量闲聊（不强制检索）

输入信号：
  message 文本 + attachments 摘要（表/文档/代码是否出现）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

Mode = Literal["sql", "rag", "hybrid", "agent", "chat"]

_SELECT_ONLY = re.compile(
    r"^\s*select\b[\s\S]+$",
    re.IGNORECASE,
)
_SQL_FENCE = re.compile(
    r"```(?:sql)?\s*(select[\s\S]+?)```",
    re.IGNORECASE,
)
_CODE_FENCE = re.compile(r"```[\w+-]*\n[\s\S]{40,}?```")
_PATH_LINE = re.compile(
    r"(?m)^(?:\./|src/|app/|[\w.-]+/)[\w./\\-]+\.\w{1,8}\s*$"
)

_TABLE_WORDS = (
    "表", "员工", "工资", "部门", "统计", "多少人", "名单",
    "csv", "excel", "xlsx", "select", "count(", "sum(",
    "employees", "salary", "dept",
)
_CODE_WORDS = (
    "代码", "项目", "重构", "bug", "报错", "函数", "类",
    "实现", "编写", "脚本", "python", "文件", "目录",
    "readme", "依赖", "pip", "调试", "改一下", "帮我写",
)
_KB_WORDS = (
    "什么是", "如何", "怎么", "政策", "制度", "说明",
    "rag", "检索", "知识库", "文档", "规定", "流程",
)
_CHAT_WORDS = (
    "你好", "您好", "谢谢", "哈哈", "在吗", "早上好",
    "晚上好", "再见", "谁是你", "你是谁", "介绍一下你",
)


@dataclass
class AttachmentSummary:
    count: int = 0
    tables: list[str] = field(default_factory=list)
    docs: list[str] = field(default_factory=list)
    codes: list[str] = field(default_factory=list)
    others: list[str] = field(default_factory=list)
    workspace_relpaths: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def has_tables(self) -> bool:
        return bool(self.tables)

    @property
    def has_docs(self) -> bool:
        return bool(self.docs)

    @property
    def has_codes(self) -> bool:
        return bool(self.codes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "tables": self.tables,
            "docs": self.docs,
            "codes": self.codes,
            "others": self.others,
            "workspace_relpaths": self.workspace_relpaths,
            "notes": self.notes,
        }


def extract_inline_sql(message: str) -> str | None:
    """整段或 fenced 的单条 SELECT。"""
    text = (message or "").strip()
    if not text:
        return None
    m = _SQL_FENCE.search(text)
    if m:
        sql = m.group(1).strip().rstrip(";")
        if sql.lower().startswith("select") and "select" in sql.lower():
            # 排除明显还含中文任务说明的长文
            if len(text) < 800 or text.lower().strip().startswith("select"):
                return sql
    if _SELECT_ONLY.match(text) and text.lower().count("select") == 1:
        # 禁止夹带多语句
        if ";" in text.rstrip().rstrip(";"):
            return None
        return text.rstrip().rstrip(";")
    return None


def looks_like_pasted_project(message: str) -> bool:
    """对话框粘贴多文件/大段代码。"""
    text = message or ""
    if len(_CODE_FENCE.findall(text)) >= 2:
        return True
    if len(_PATH_LINE.findall(text)) >= 3:
        return True
    if text.count("```") >= 4 and len(text) > 400:
        return True
    return False


def _hit_any(text: str, words: tuple[str, ...]) -> bool:
    low = text.lower()
    return any(w.lower() in low for w in words)


def route(
    message: str,
    *,
    attachments: AttachmentSummary | None = None,
    force_mode: str | None = None,
    db_ready: bool = False,
) -> dict[str, Any]:
    """
    返回 {mode, reason, sql?}
    force_mode 合法则直接用。
    """
    att = attachments or AttachmentSummary()
    msg = (message or "").strip()
    forced = (force_mode or "").strip().lower() or None
    allowed = {"sql", "rag", "hybrid", "agent", "chat"}
    if forced in allowed:
        return {"mode": forced, "reason": "force_mode", "sql": extract_inline_sql(msg)}

    inline_sql = extract_inline_sql(msg)
    if inline_sql and not att.has_codes and not looks_like_pasted_project(msg):
        # 纯 SQL 或「就这一句 SELECT」
        if not msg or msg == inline_sql or len(msg) < len(inline_sql) + 40:
            return {"mode": "sql", "reason": "inline_select", "sql": inline_sql}

    if att.has_codes or looks_like_pasted_project(msg) or (
        msg and _hit_any(msg, _CODE_WORDS) and (att.count > 0 or "```" in msg)
    ):
        return {"mode": "agent", "reason": "code_or_project", "sql": inline_sql}

    if att.has_tables or (db_ready and msg and _hit_any(msg, _TABLE_WORDS)):
        # 表相关：有知识意图或文档 → hybrid；仅数数也可 hybrid（可降级）
        if att.has_docs or _hit_any(msg, _KB_WORDS) or not inline_sql:
            return {"mode": "hybrid", "reason": "table_and_or_kb", "sql": inline_sql}
        return {"mode": "hybrid", "reason": "table_query", "sql": inline_sql}

    if att.has_docs:
        # 本轮上传文档：用 agent 读工作区，或 hybrid/rag；优先 agent 读附件
        if msg and _hit_any(msg, _CODE_WORDS):
            return {"mode": "agent", "reason": "docs_plus_task", "sql": None}
        if msg:
            return {"mode": "agent", "reason": "session_docs", "sql": None}
        return {"mode": "chat", "reason": "docs_no_question", "sql": None}

    if not msg:
        return {"mode": "chat", "reason": "empty_message", "sql": None}

    if len(msg) <= 30 and _hit_any(msg, _CHAT_WORDS) and not _hit_any(msg, _KB_WORDS):
        return {"mode": "chat", "reason": "chitchat", "sql": None}

    if _hit_any(msg, _CODE_WORDS) and not _hit_any(msg, _KB_WORDS):
        return {"mode": "agent", "reason": "coding_intent", "sql": None}

    if db_ready and _hit_any(msg, _TABLE_WORDS) and _hit_any(msg, _KB_WORDS):
        return {"mode": "hybrid", "reason": "mixed_intent", "sql": inline_sql}

    if db_ready and _hit_any(msg, _TABLE_WORDS):
        return {"mode": "hybrid", "reason": "table_intent", "sql": inline_sql}

    return {"mode": "rag", "reason": "knowledge_default", "sql": inline_sql}
