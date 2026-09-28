import logging
import chromadb

from .config import Config
from .model_manager import ModelManager

logger = logging.getLogger(__name__)


class VectorIndex:
    def __init__(self):
        # 建库
        self.client = chromadb.PersistentClient(path=Config.CHROMA_PERSIST_DIR)
        # 建表（余弦空间）
        self.collection = self._open_collection()
        # 模型实例
        self.embedding_model = ModelManager.get_embedding_model()

    def _open_collection(self):
        return self.client.get_or_create_collection(
            name=Config.CHROMA_COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )

    def clear(self) -> None:
        """
        删掉整张 collection 再重建空表。
        全量重建用：避免 upsert 残留已删除文件的旧 chunk。
        """
        name = Config.CHROMA_COLLECTION_NAME
        try:
            self.client.delete_collection(name)
            logger.info("已删除 Chroma collection: %s", name)
        except Exception as e:
            # 不存在时部分版本会抛；视为已空
            logger.info("删除 collection 跳过/失败（可忽略）: %s", e)
        self.collection = self._open_collection()
        logger.info("Chroma collection 已重置，count=%s", self.collection.count())

    def add(self, chunks: list[dict]) -> None:
        if not chunks:
            return
        documents = [item["text"] for item in chunks]
        metadatas = [
            {
                **item["metadata"],
                "chunk_id": item["chunk_id"],
            }
            for item in chunks
        ]
        ids = [item["chunk_id"] for item in chunks]

        logger.info("向量化 %s 个块...", len(chunks))
        embeddings = self.embedding_model.encode(
            documents,
            batch_size=64,
            show_progress_bar=True,
            normalize_embeddings=True,
        ).tolist()
        self.collection.upsert(
            documents=documents,
            embeddings=embeddings,
            metadatas=metadatas,
            ids=ids,
        )
        logger.info("向量索引更新，总数: %s", self.collection.count())
#topk语义召回
    def retrieve(
        self,
        query: str,
        top_k: int = Config.RETRIEVAL_TOP_K,
        principal=None,
    ) -> tuple[list[dict], list[float]]:
        # 先取更宽候选，避免受限块占满 top_k 后漏掉可见文档。
        query_vec = self.embedding_model.encode(query, normalize_embeddings=True).tolist()
        total = self.collection.count()
        n_results = min(total, max(top_k, top_k * 10))
        if n_results <= 0:
            return [], []
        results = self.collection.query(
            query_embeddings=[query_vec],
            n_results=n_results,
            include=["documents", "distances", "metadatas"],
        )
        #切片
        documents = results["documents"][0] \
            if results["documents"] else []


        distances = results["distances"][0] \
            if results["distances"] else []


        metadatas = results["metadatas"][0] \
            if results["metadatas"] else []
    # 3. 整理成和 BM25 一样的数据结构
        docs = []
        scores = []

        for text, distance, metadata in zip(
            documents,
            distances,
            metadatas
        ):
            metadata = metadata or {}

            chunk_id = metadata.get("chunk_id")

            if chunk_id is None:
                continue

            docs.append({
                "chunk_id": chunk_id,
                "text": text,
                "metadata": metadata,
                # cosine distance：越小越近；供融合后调试，不参与 RRF 公式
                "vector_distance": float(distance),
            })

            scores.append(distance)

        if principal is not None:
            from app.acl import filter_chunks_by_acl

            docs = filter_chunks_by_acl(docs, principal)
            scores = [float(item.get("vector_distance", 0.0)) for item in docs]

        return docs[:top_k], scores[:top_k]
