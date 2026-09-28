"""
统一对话编排 ChatService

  用户：一句话 + 可选附件/粘贴项目
  → 附件落盘 / 粘贴拆文件
  → chat_router 分流
  → sql | rag | hybrid | agent | chat
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, BinaryIO

from dotenv import load_dotenv
from openai import OpenAI

from app.chat_attachments import (
    AttachmentSummary,
    build_attachment_context,
    materialize_attachments,
    paste_project_bundle,
    session_workspace,
)
from app.chat_router import route
from app.errors import (
    AppError,
    LlmError,
    ValidationError,
    wrap_unexpected,
)
from app.hybrid import HybridService
from app.logging_setup import get_logger
from app.services import AgentService, KnowledgeService, TableService

logger = get_logger(__name__)


def _db_ready() -> bool:
    try:
        from sqlite_tool.config import Config as SqlConfig

        return Path(SqlConfig.QUERY_PATH).is_file()
    except Exception:
        return False


def _merge_summary(a: AttachmentSummary, b: AttachmentSummary) -> AttachmentSummary:
    out = AttachmentSummary(
        count=a.count + b.count,
        tables=list(dict.fromkeys(a.tables + b.tables)),
        docs=list(dict.fromkeys(a.docs + b.docs)),
        codes=list(dict.fromkeys(a.codes + b.codes)),
        others=list(dict.fromkeys(a.others + b.others)),
        workspace_relpaths=list(
            dict.fromkeys(a.workspace_relpaths + b.workspace_relpaths)
        ),
        notes=a.notes + b.notes,
    )
    return out


def _plain_chat(message: str, *, session_id: str) -> str:
    """不检索的轻量回复（闲聊 / 无问题）。"""
    from rag.config import Config as RagConfig

    load_dotenv(RagConfig.ENV_PATH)
    key = os.getenv(RagConfig.LLM_API_KEY_ENV)
    if not key:
        raise LlmError(
            f"未配置 {RagConfig.LLM_API_KEY_ENV}",
            stage="chat.llm",
        )
    client = OpenAI(
        api_key=key.strip(),
        base_url=RagConfig.LLM_BASE_URL,
        timeout=60,
    )
    prompt = (
        "你是企业私有知识库助手。用户可能只是寒暄或确认附件。"
        "简洁友好回答；若对方要查知识库/表数据，提示可以直接提问。\n\n"
        f"session={session_id}\n用户：{message}"
    )
    try:
        resp = client.chat.completions.create(
            model=RagConfig.LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.6,
            max_tokens=512,
        )
        content = resp.choices[0].message.content
        if not content:
            raise LlmError("模型返回空内容", stage="chat.llm")
        return content.strip()
    except LlmError:
        raise
    except Exception as e:
        raise LlmError("闲聊调用失败", stage="chat.llm", cause=e) from e


class ChatService:
    def __init__(
        self,
        knowledge: KnowledgeService | None = None,
        tables: TableService | None = None,
        hybrid: HybridService | None = None,
        agents: AgentService | None = None,
    ):
        self.kb = knowledge or KnowledgeService()
        self.tables = tables or TableService()
        self.hybrid = hybrid or HybridService(knowledge=self.kb, tables=self.tables)
        self.agents = agents or AgentService()

    def handle(
        self,
        message: str,
        *,
        session_id: str | None = "chat",
        force_mode: str | None = None,
        files: list[tuple[str, BinaryIO]] | None = None,
        use_memory: bool = True,
        unpack_paste: bool = True,
        allow_agent: bool = True,
        principal: Any | None = None,
    ) -> dict[str, Any]:
        from app.auth import scoped_session_id

        # 会话工作区与记忆按调用者隔离：不同 key 传相同 session_id 也互不可见。
        sid = scoped_session_id(principal, session_id)
        t0 = time.time()
        raw_msg = message or ""
        attach_details: list[dict[str, Any]] = []
        summary = AttachmentSummary()

        # 1) 上传附件
        if files:
            try:
                s1, d1 = materialize_attachments(session_id=sid, files=files)
                summary = _merge_summary(summary, s1)
                attach_details.extend(d1)
            except AppError:
                raise
            except Exception as e:
                raise wrap_unexpected(
                    e, stage="chat.attach", message="附件处理失败"
                ) from e

        # 2) 粘贴项目/代码块 → 落盘
        work_msg = raw_msg
        if unpack_paste and raw_msg and ("```" in raw_msg or raw_msg.count("\n") > 5):
            cleaned, s2, d2 = paste_project_bundle(session_id=sid, message=raw_msg)
            if d2:
                summary = _merge_summary(summary, s2)
                attach_details.extend(d2)
                work_msg = cleaned

        work_dir = str(session_workspace(sid).resolve())
        ctx = build_attachment_context(summary)
        if ctx:
            user_blob = f"{work_msg}\n\n{ctx}".strip() if work_msg else ctx
        else:
            user_blob = work_msg.strip()

        if not user_blob and not summary.count:
            raise ValidationError(
                "请输入内容或上传附件",
                stage="chat.handle",
            )

        # 3) 分流
        decision = route(
            work_msg or user_blob,
            attachments=summary,
            force_mode=force_mode,
            db_ready=_db_ready(),
        )
        mode = decision["mode"]

        # 只读角色：agent 降级为 rag（有文档则提示用知识库问法）
        if mode == "agent" and not allow_agent:
            from app.errors import PermissionDeniedError

            raise PermissionDeniedError(
                "当前角色不能执行 Agent（终端/写文件）。"
                "请改问知识库问题，或换用 admin/user 密钥。",
                stage="chat.route",
                details={"wanted_mode": "agent", "reason": decision.get("reason")},
            )

        plan = {
            "mode": mode,
            "reason": decision.get("reason"),
            "sql_hint": decision.get("sql"),
            "attachments": summary.to_dict(),
        }
        logger.info(
            "chat route mode=%s reason=%s session=%s files=%s",
            mode,
            plan["reason"],
            sid,
            summary.count,
        )

        result_body: dict[str, Any]

        try:
            if mode == "sql":
                sql = decision.get("sql") or work_msg
                result_body = self.tables.query(sql, principal=principal)
                answer = (
                    f"执行 SQL 成功，返回 {len(result_body.get('rows') or [])} 行。"
                    if result_body.get("ok")
                    else "SQL 执行失败"
                )
                if result_body.get("rows") is not None:
                    import json

                    preview = json.dumps(
                        (result_body.get("rows") or [])[:20],
                        ensure_ascii=False,
                        default=str,
                    )
                    answer = f"{answer}\n{preview}"

            elif mode == "rag":
                result_body = self.kb.ask(
                    user_blob if not summary.count else work_msg or user_blob,
                    session_id=sid,
                    use_memory=use_memory,
                    principal=principal,
                )
                answer = result_body.get("answer") or ""

            elif mode == "hybrid":
                result_body = self.hybrid.ask(
                    work_msg or user_blob,
                    sql=decision.get("sql"),
                    session_id=sid,
                    use_rag=True,
                    use_sql=True,
                    auto_sql=not bool(decision.get("sql")),
                    use_memory=use_memory,
                    principal=principal,
                )
                answer = result_body.get("answer") or ""

            elif mode == "agent":
                task = user_blob
                if summary.workspace_relpaths:
                    task += (
                        "\n\n请优先用 file_Read 查看工作目录中的附件："
                        + ", ".join(summary.workspace_relpaths[:30])
                    )
                result_body = self.agents.run(
                    task, work_dir=work_dir, session_id=sid, principal=principal
                )
                answer = result_body.get("answer") or ""

            else:  # chat
                answer = _plain_chat(
                    user_blob or "（用户上传了附件）",
                    session_id=sid,
                )
                result_body = {"ok": True, "mode": "chat"}

        except AppError:
            raise
        except Exception as e:
            logger.exception("chat dispatch failed mode=%s", mode)
            raise wrap_unexpected(
                e,
                stage=f"chat.{mode}",
                message="对话执行失败",
                details={"mode": mode, "session_id": sid},
            ) from e

        return {
            "ok": True,
            "answer": answer,
            "mode": mode,
            "session_id": sid,
            "work_dir": work_dir,
            "plan": plan,
            "attachments": attach_details,
            "result": {
                k: result_body.get(k)
                for k in (
                    "sources",
                    "cite_ids",
                    "sql",
                    "rows",
                    "timing",
                    "mode",
                    "parts",
                )
                if isinstance(result_body, dict) and k in result_body
            },
            "timing": time.time() - t0,
        }
