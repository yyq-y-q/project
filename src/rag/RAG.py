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

        vec_docs, _ = self.vector_index.retrieve(
            user_input, Config.RETRIEVAL_TOP_K, principal=principal
        )
        bm25_docs, _ = self.bm25_index.retrieve(
            user_input, Config.RETRIEVAL_TOP_K, principal=principal
        )
        fused = reciprocal_rank_fusion([vec_docs, bm25_docs])
        # 融合后再过滤一次，防止第三方召回实现遗漏过滤。
        fused = filter_chunks_by_acl(fused, principal)
        candidates = fused[:Config.RETRIEVAL_TOP_K]

        # 2. 只对授权候选重排，避免受限文本进入后续模型调用
        reranked = self.reranker.rerank(user_input, candidates, Config.RERANK_TOP_K)

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