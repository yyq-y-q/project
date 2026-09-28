"""
Agent → rag 适配层

- 普通知识问答：rag_Query(question, session_id?)
- 索引全局一份：走 app.services.get_rag，与 API 同一单例
- session_id 隔离多轮记忆（跨进程见 memory 文件锁）
"""
from __future__ import annotations

import json


def _get_pipeline():
    # 统一入口：禁止本模块再维护第二份 RAGPipeline
    from app.services import get_rag

    pipe = get_rag()
    if not pipe._is_built and not pipe.load_index():
        raise RuntimeError(
            "RAG 索引未就绪：请先对 data/raw 叙述文档 build_index / mykb rag-rebuild"
        )
    return pipe


def rag_Query(question: str, session_id: str = "agent", principal=None) -> str:
    """
    对知识库做一次 RAG 问答（检索 + 重排 + LLM）。

    参数:
        question: 自然语言问题（概念、制度、说明书等非表数据）
        session_id: 会话 id，同 id 共享多轮记忆；不同 id 并发互不干扰
        principal: 调用者身份（可选）；传入时文档级 ACL 与记忆按人隔离

    返回:
        JSON：answer / sources / cite_ids / session_id / timing；失败含 error
    """
    try:
        pipe = _get_pipeline()
        # Agent 多轮由 ReAct messages 承担；此处默认开 session 记忆便于「刚才说的」
        result = pipe.query(
            question,
            use_memory=True,
            session_id=session_id or "agent",
            principal=principal,
        )
        contexts = result.get("contexts") or []
        sources = list(result.get("sources") or [])
        if not sources:
            for c in contexts:
                meta = c.get("metadata") or {}
                src = meta.get("source") or meta.get("doc_id")
                if src and src not in sources:
                    sources.append(src)

        hits = []
        for c in contexts:
            meta = c.get("metadata") or {}
            hits.append(
                {
                    "source": meta.get("source"),
                    "chunk_id": c.get("chunk_id"),
                    "rerank_score": c.get("rerank_score"),
                    "rrf_score": c.get("rrf_score"),
                }
            )

        payload = {
            "answer": result.get("answer"),
            "sources": sources,
            "cite_ids": result.get("cite_ids") or [],
            "hits": hits,
            "session_id": result.get("session_id"),
            "timing": result.get("timing"),
            "rerank_count": result.get("rerank_count"),
            "history_length": result.get("history_length"),
        }
        return json.dumps(payload, ensure_ascii=False, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)
