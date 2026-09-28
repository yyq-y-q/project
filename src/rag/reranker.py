import logging

from .config import Config
from .model_manager import ModelManager

logger = logging.getLogger(__name__)


class Reranker:
    def __init__(self):
        self.model = ModelManager.get_rerank_model()

    def rerank(
        self,
        query: str,
        candidates: list[dict],
        top_k: int = Config.RERANK_TOP_K,
    ) -> list[dict]:
        """
        交叉编码器精排。

        返回的每个 doc 带 rerank_score（越大越相关）。
        """
        if not candidates:
            return []

        pairs = [[query, item["text"]] for item in candidates]
        scores = self.model.predict(pairs)

        ranked = sorted(
            zip(candidates, scores),
            key=lambda x: x[1],
            reverse=True,
        )

        out: list[dict] = []
        for doc, score in ranked[:top_k]:
            item = dict(doc)
            item["metadata"] = dict(doc.get("metadata") or {})
            item["rerank_score"] = float(score)
            out.append(item)
        return out
