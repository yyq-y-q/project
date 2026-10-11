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
        交叉编码器精排（企业级增强版）：

        1. 按 rerank 分降序排序；
        2. 分数组内归一化到 [0,1]（rerank_score_norm），便于跨查询比较与 trace 展示；
        3. 来源去重：同一 source（文档）最多保留 RERANK_MAX_PER_SOURCE 个块，
           保证答案覆盖多个文档，避免单文档片段堆满上下文。

        返回的每个 doc 带 rerank_score / rerank_score_norm（越大越相关）。
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

        raw = [float(s) for _, s in ranked]
        lo, hi = min(raw), max(raw)
        span = (hi - lo) or 1.0

        max_per_source = getattr(Config, "RERANK_MAX_PER_SOURCE", 2) or 0

        selected: list[dict] = []
        seen_sources: dict[str, int] = {}
        for doc, score in ranked:
            meta = doc.get("metadata") or {}
            src = str(meta.get("source") or meta.get("doc_id") or "")
            cnt = seen_sources.get(src, 0)
            if max_per_source and cnt >= max_per_source:
                continue
            item = dict(doc)
            item["metadata"] = dict(meta)
            item["rerank_score"] = float(score)
            item["rerank_score_norm"] = round((float(score) - lo) / span, 4)
            selected.append(item)
            seen_sources[src] = cnt + 1
            if len(selected) >= top_k:
                break
        return selected
