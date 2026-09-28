import logging

from .config import Config

logger = logging.getLogger(__name__)


def reciprocal_rank_fusion(
    rank_lists: list[list[dict]],
    k: int = Config.RRF_K,
) -> list[dict]:
    """
    RRF 融合多路召回。

    输出每个 doc 额外带：
      rrf_score: 融合分
    不覆盖原有 channel 分数字段（如 vector_distance / bm25_score）。
    """
    scores: dict[str, float] = {}
    docs: dict[str, dict] = {}

    for rank_list in rank_lists:
        for rank, doc in enumerate(rank_list, start=1):
            chunk_id = doc["chunk_id"]
            # 浅拷贝，避免原地改坏各路原始列表
            if chunk_id not in docs:
                docs[chunk_id] = dict(doc)
                docs[chunk_id]["metadata"] = dict(doc.get("metadata") or {})
            else:
                # 后到的路若有额外分数字段，合并进同一 doc
                for key, val in doc.items():
                    if key in ("chunk_id", "text", "metadata"):
                        continue
                    if key not in docs[chunk_id]:
                        docs[chunk_id][key] = val
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)

    ranked_ids = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)

    out: list[dict] = []
    for i in ranked_ids:
        item = docs[i]
        item["rrf_score"] = scores[i]
        out.append(item)
    return out
