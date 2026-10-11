"""重排测试：源去重（RERANK_MAX_PER_SOURCE）与分数归一化。

覆盖 rag/reranker.py 的企业级增强逻辑，用替身模型替代真实交叉编码器，
不下载推理模型，可离线运行。
"""
import pytest

from rag import reranker
from rag.config import Config


class _FakeModel:
    def __init__(self, scores):
        self._scores = scores

    def predict(self, pairs):
        return self._scores


def _candidate(doc_id, source, text="内容"):
    return {
        "chunk_id": f"{doc_id}_chunk_0",
        "text": text,
        "metadata": {"doc_id": doc_id, "source": source},
    }


@pytest.fixture
def rerank(monkeypatch):
    monkeypatch.setattr(Config, "RERANK_MAX_PER_SOURCE", 2)
    monkeypatch.setattr(Config, "RERANK_TOP_K", 10)
    inst = object.__new__(reranker.Reranker)  # 绕过 __init__，不加载真实模型
    return inst


class TestSourceDedup:
    def test_max_per_source_enforced(self, rerank):
        candidates = [
            _candidate("a", "doc_a.txt", "块1"),
            _candidate("b", "doc_a.txt", "块2"),
            _candidate("c", "doc_a.txt", "块3"),
            _candidate("d", "doc_b.txt", "块4"),
            _candidate("e", "doc_b.txt", "块5"),
        ]
        rerank.model = _FakeModel([0.9, 0.8, 0.7, 0.6, 0.5])
        out = rerank.rerank("query", candidates)
        sources = [c["metadata"]["source"] for c in out]
        assert sources.count("doc_a.txt") <= 2
        assert sources.count("doc_b.txt") <= 2

    def test_top_k_respected(self, rerank):
        candidates = [_candidate(str(i), f"doc_{i}.txt") for i in range(5)]
        rerank.model = _FakeModel([0.5, 0.4, 0.3, 0.2, 0.1])
        out = rerank.rerank("query", candidates, top_k=2)
        assert len(out) == 2

    def test_empty_candidates(self, rerank):
        assert rerank.rerank("query", []) == []


class TestScoreNormalization:
    def test_norm_in_range_zero_to_one(self, rerank):
        candidates = [
            _candidate("a", "doc_a.txt"),
            _candidate("b", "doc_b.txt"),
            _candidate("c", "doc_c.txt"),
        ]
        rerank.model = _FakeModel([0.9, 0.7, 0.2])
        out = rerank.rerank("query", candidates)
        norms = [c["rerank_score_norm"] for c in out]
        # 最高分归一为 1，最低为 0
        assert norms[0] == 1.0
        assert norms[-1] == 0.0
        assert all(0.0 <= n <= 1.0 for n in norms)
        # 排序：rerank_score 降序
        scores = [c["rerank_score"] for c in out]
        assert scores == sorted(scores, reverse=True)
        # 原始分数保留
        assert all("rerank_score" in c for c in out)

    def test_flat_scores_no_division_by_zero(self, rerank):
        candidates = [
            _candidate("a", "doc_a.txt"),
            _candidate("b", "doc_b.txt"),
        ]
        rerank.model = _FakeModel([0.5, 0.5])
        out = rerank.rerank("query", candidates)
        assert len(out) == 2
        assert all(c["rerank_score_norm"] == 0.0 for c in out)
