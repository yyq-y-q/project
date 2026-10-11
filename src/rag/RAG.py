import hashlib
import os
import sys
import json
import logging
import time
from collections import deque
import threading

# ---------- 第三方库 ----------

from openai import OpenAI
from dotenv import load_dotenv

# ---------- 日志配置 ----------
#全局日志S
logging.basicConfig(
    level=logging.INFO,#输出级别
    #填时间（自动抓取当前系统时间） 
    #日志记录器的名字（就是你那个 logger = logging.getLogger(__name__) 里的 __name__）
    #填日志级别（INFO、WARNING、ERROR 等大写单词）
    #你具体想打印的文字内容
    #s:当作字符串（string）处理
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    #处理器       流处理器          标准输出流 到终端 
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# ---------- 配置 ----------
from .config import Config


# ---------- 模型管理器 ----------
#在同一进程内模型只被加载一次
from .model_manager import ModelManager

# ---------- 文本切分 ----------
from .splitter import split_documents

# ---------- 向量索引 ----------
from .vector import VectorIndex
# ---------- BM25 索引 ----------
from .bm25 import BM25Index


# ---------- RRF 融合 ----------  
from .fusion import reciprocal_rank_fusion
# ---------- 重排 ----------
from .reranker import Reranker
# ---------- LLM 生成 ----------
from .llm_generator import LLMGenerator
# ---------- 记忆管理器 ----------
from .memory import MemoryManager
# ---------- 完整 RAG Pipeline（带记忆） ----------
#它将“检索、融合、重排、记忆、生成”这五个独立的技术组件，
# 组装成一个开箱即用的“智能问答机器人”对象
class RAGPipeline:
    def __init__(self):
        #语义召回
        self.vector_index = VectorIndex()
        #关键字召回
        self.bm25_index = BM25Index()
        #重排
        self.reranker = Reranker()
        #llm调用
        self.llm = LLMGenerator()
        #记忆摘要管理 滑动窗口轮式加摘要管理
        self.memory = MemoryManager(llm=self.llm)
        self._memories: dict[str, MemoryManager] = {}
        self._memory_lock = threading.Lock()
        self._is_built = False

    def _memory_for(self, session_id: str) -> MemoryManager:
        from .config import Config

        with self._memory_lock:
            if session_id not in self._memories:
                digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
                path = Config.HISTORY_DIR / f"{digest}.json"
                self._memories[session_id] = MemoryManager(self.llm, persist_path=path)
            return self._memories[session_id]
#将原始文档加工成一种支持“快速查找”的数据结构。
    def build_index(
        self,
        documents: list[str],
        doc_metadata: list[dict] | None = None,
        *,
        rebuild: bool = False,
    ) -> bool:
        logger.info("开始构建索引")
        chunks = split_documents(documents, doc_metadata)
        if not chunks:
            logger.warning("切分后无有效块")
            return False
        try:
            if rebuild:
                # 全量重建必须同时清掉两路索引，避免删除文档残留在召回结果中。
                self.vector_index.clear()
                self.bm25_index.clear()
            self.vector_index.add(chunks)
            self.bm25_index.build(chunks)
            self._is_built = True
            logger.info("索引构建完成")
            return True
        except Exception as e:
            self._is_built = False
            logger.exception("索引构建失败: %s", e)
            return False

  
    def load_index(self):

        # 1. 加载 BM25
        bm25_ok = self.bm25_index.load()

        # 2. 检查 Chroma 是否真的有向量数据
        vector_ok = self.vector_index.collection.count() > 0

        # 3. 两边都有数据，才认为索引完整
        if bm25_ok and vector_ok:
            self._is_built = True
            logger.info("索引加载成功")
            return True

        self._is_built = False
        logger.warning("索引加载失败，请先构建索引")
        return False

    def update_index(
        self,
        root=None,
        owner_key_id: str | None = None,
    ) -> dict:
        """
        增量更新索引（企业级：变更检测 + 原子替换 + 失败回滚）。

        - 无增量基线（manifest / chunks 缓存缺失）→ 全量重建并落基线；
        - 有基线 → 只处理新增 / 修改 / 删除的文档：
            向量按 doc_id 更新（upsert / delete）；
            BM25 与两块磁盘产物（chunks.json / index_manifest.json）全部走
            「临时文件 + os.replace」，任一步失败磁盘产物不落盘；
        - 向量（Chroma）实时生效，失败时记录并提示 rag-rebuild 全量兜底。
        """
        import time as _time
        from pathlib import Path

        from .loader import load_doc_paths
        from .manifest import (
            detect_changes,
            file_hash,
            load_chunks,
            load_manifest,
            save_chunks,
            save_manifest,
        )

        root_dir = Path(root) if root else Config.RAW_DOCS_DIR
        t0 = _time.time()

        manifest = load_manifest()
        chunks_cache = load_chunks()

        # 无基线 → 全量
        if not manifest or not chunks_cache:
            from .loader import load_docs_dir

            docs, metas = load_docs_dir(root_dir)
            if not docs:
                raise RuntimeError(f"无 RAG 文档: {root_dir}")
            if owner_key_id:
                for meta in metas:
                    meta.setdefault("owner_key_id", owner_key_id)
                    meta.setdefault("visibility", "private")
            else:
                for meta in metas:
                    meta.setdefault("visibility", "public")
            ok = self.build_index(docs, metas, rebuild=True)
            if not ok:
                raise RuntimeError("全量重建失败")
            # 落基线
            new_chunks = split_documents(docs, metas)
            new_manifest = self._manifest_from_chunks(new_chunks, root_dir)
            save_chunks(new_chunks)
            save_manifest(new_manifest)
            return {
                "mode": "full",
                "added": len(docs),
                "modified": 0,
                "removed": 0,
                "chunks": len(new_chunks),
                "timing_s": round(_time.time() - t0, 2),
            }

        added, modified, removed = detect_changes(manifest, root_dir)
        changed = added + modified
        if not changed and not removed:
            return {
                "mode": "noop",
                "added": 0,
                "modified": 0,
                "removed": 0,
                "chunks": len(chunks_cache),
                "timing_s": round(_time.time() - t0, 2),
            }

        logger.info(
            "增量变更: added=%s modified=%s removed=%s",
            len(added), len(modified), len(removed),
        )

        # 1) 重新加载变更文档 → 切块
        new_chunks: list[dict] = []
        if changed:
            docs, metas = load_doc_paths(changed)
            if not docs:
                raise RuntimeError("变更文档加载失败（全部不可读）")
            if owner_key_id:
                for meta in metas:
                    meta.setdefault("owner_key_id", owner_key_id)
                    meta.setdefault("visibility", "private")
            else:
                for meta in metas:
                    meta.setdefault("visibility", "public")
            new_chunks = split_documents(docs, metas)

        # 2) 更新内存缓存（chunks_cache）
        changed_ids = {c["metadata"]["doc_id"] for c in new_chunks}
        kept = [
            c for c in chunks_cache
            if c.get("metadata", {}).get("doc_id") not in changed_ids
            and c.get("metadata", {}).get("doc_id") not in set(removed)
        ]
        merged = kept + new_chunks

        # 3) 向量索引：先清变更文档的旧块（chunk_id 全局编号会随内容变化，避免残留），
        #    再 upsert 新块；删除文档按 doc_id 移除
        try:
            for chunk in new_chunks:
                self.vector_index.delete_by_doc(chunk["metadata"]["doc_id"])
            if new_chunks:
                self.vector_index.add(new_chunks)
            for doc_id in removed:
                self.vector_index.delete_by_doc(doc_id)
        except Exception as e:
            logger.exception("向量增量更新失败，磁盘产物未落盘（可 rag-rebuild 全量兜底）: %s", e)
            raise

        # 4) BM25 用合并后的全量语料重建（原子写）
        self.bm25_index.build(merged)

        # 5) 磁盘基线原子落盘
        new_manifest = dict(manifest)
        for doc_id in removed:
            new_manifest.pop(doc_id, None)
        for chunk in new_chunks:
            meta = chunk.get("metadata") or {}
            doc_id = meta.get("doc_id")
            if not doc_id:
                continue
            entry = new_manifest.get(doc_id) or {}
            entry["chunk_count"] = sum(
                1 for c in merged if c.get("metadata", {}).get("doc_id") == doc_id
            )
            entry["source"] = meta.get("source")
            entry["path"] = meta.get("source_path", "")
            entry["file_type"] = meta.get("file_type", "")
            try:
                entry["hash"] = file_hash(Path(meta.get("source_path", "")))
            except Exception:
                entry["hash"] = ""
            entry["updated_at"] = int(_time.time())
            new_manifest[doc_id] = entry

        save_chunks(merged)
        save_manifest(new_manifest)

        self._is_built = True
        return {
            "mode": "incremental",
            "added": len(added),
            "modified": len(modified),
            "removed": len(removed),
            "chunks": len(merged),
            "timing_s": round(_time.time() - t0, 2),
        }

    def _manifest_from_chunks(self, chunks: list[dict], root_dir) -> dict:
        from pathlib import Path

        from .manifest import file_hash

        manifest: dict = {}
        for chunk in chunks:
            meta = chunk.get("metadata") or {}
            doc_id = meta.get("doc_id")
            if not doc_id:
                continue
            entry = manifest.get(doc_id) or {}
            entry["chunk_count"] = entry.get("chunk_count", 0) + 1
            entry["source"] = meta.get("source")
            entry["path"] = meta.get("source_path", "")
            entry["file_type"] = meta.get("file_type", "")
            try:
                entry["hash"] = file_hash(Path(meta.get("source_path", "")))
            except Exception:
                entry["hash"] = ""
            manifest[doc_id] = entry
        return manifest


    
    def query(
        self,
        user_input: str,
        use_memory: bool = True,
        session_id: str = "default",
        principal=None,
    ) -> dict[str, object]:
        if not self._is_built:
            raise RuntimeError("索引未构建，请先 build_index 或 load_index")
#开始时间
        start_time = time.time()

        # 1. 检索：两路召回都在进入融合前做 ACL 过滤。
        from app.acl import filter_chunks_by_acl

        t_vec = time.time()
        vec_docs, _ = self.vector_index.retrieve(
            user_input, Config.RETRIEVAL_TOP_K, principal=principal
        )
        t_bm25 = time.time()
        bm25_docs, _ = self.bm25_index.retrieve(
            user_input, Config.RETRIEVAL_TOP_K, principal=principal
        )
        t_fuse = time.time()
        fused = reciprocal_rank_fusion([vec_docs, bm25_docs])
        # 融合后再过滤一次，防止第三方召回实现遗漏过滤。
        fused = filter_chunks_by_acl(fused, principal)
        candidates = fused[:Config.RETRIEVAL_TOP_K]
        t_rerank = time.time()

        # 2. 只对授权候选重排，避免受限文本进入后续模型调用
        reranked = self.reranker.rerank(user_input, candidates, Config.RERANK_TOP_K)
        t_llm = time.time()

        scoped_session = session_id or "default"
        if principal is not None:
            scoped_session = f"{principal.key_id}:{scoped_session}"
        memory = self._memory_for(scoped_session)

        # 3. 构造 Prompt（含当前操作者自己的历史）
        if use_memory:
            history_str = memory.format_history()
            if history_str:
                enhanced_input = f"历史对话：\n{history_str}\n\n当前问题：{user_input}"
            else:
                enhanced_input = user_input
        else:
            enhanced_input = user_input

        # 4. LLM 只接收已授权的重排结果
        answer = self.llm.generate(enhanced_input, reranked)

        # 5. 更新当前操作者隔离的会话记忆
        if use_memory:
            memory.add_turn(user_input, answer)
            if Config.ENABLE_HISTORY_SUMMARY:
                memory.summarize()

        sources: list[str] = []
        cite_ids: list[str] = []
        for item in reranked:
            metadata = item.get("metadata") or {}
            source = metadata.get("source") or metadata.get("doc_id")
            if source and source not in sources:
                sources.append(str(source))
            chunk_id = item.get("chunk_id")
            if chunk_id and chunk_id not in cite_ids:
                cite_ids.append(str(chunk_id))

        # 结构化检索 trace：每通道召回量、RRF/rerank 明细、各阶段耗时
        trace = {
            "query": user_input,
            "vec_retrieved": len(vec_docs),
            "bm25_retrieved": len(bm25_docs),
            "fused_retrieved": len(candidates),
            "rerank_count": len(reranked),
            "channels_ms": {
                "vector": round((t_bm25 - t_vec) * 1000, 1),
                "bm25": round((t_fuse - t_bm25) * 1000, 1),
                "rerank": round((t_llm - t_rerank) * 1000, 1),
            },
            "top_hits": [
                {
                    "chunk_id": item.get("chunk_id"),
                    "source": str(
                        (item.get("metadata") or {}).get("source")
                        or (item.get("metadata") or {}).get("doc_id")
                        or ""
                    ),
                    "rerank_score_norm": item.get("rerank_score_norm"),
                }
                for item in reranked
            ],
        }
        logger.info("RAG trace: %s", json.dumps(trace, ensure_ascii=False, default=str))

        return {
            "answer": answer,
            "contexts": reranked,
            "candidates": candidates,
            "sources": sources,
            "cite_ids": cite_ids,
            "session_id": scoped_session,
            "history_length": len(memory.history) // 2,
            "timing": time.time() - start_time,
            "query": user_input,
            "retrieval_count": len(candidates),
            "rerank_count": len(reranked),
            "trace": trace,
        }

    def reset_memory(self):
        self.memory.clear()
        logger.info("记忆已清空")


# ---------- 使用示例 ----------
if __name__ == "__main__":
    # 检查 .env
    if not os.path.exists(Config.ENV_PATH):
        logger.warning(f".env 文件不存在: {Config.ENV_PATH}")

    sample_docs = [
        "RAG（检索增强生成）是一种结合信息检索与语言生成的技术。",
        "ChromaDB 是一个轻量级向量数据库，支持余弦相似度搜索。",
        "BM25 是一种基于词频和文档长度的排序算法。",
        "BGE Reranker 是一种交叉编码器，能精排候选文档。"
    ]

    pipeline = RAGPipeline()
    pipeline.build_index(sample_docs)

    # 多轮对话测试
    questions = [
        "什么是RAG？",
        "它有哪些核心组件？",
        "刚才说的那个重排模型有什么用？"
    ]

    for q in questions:
        print(f"\n用户: {q}")
        result = pipeline.query(q)
        print(f"助手: {result['answer']}")
        print(f"历史轮数: {result['history_length']}")

    # 重置记忆
    # pipeline.reset_memory()