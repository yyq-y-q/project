from rag.fusion import reciprocal_rank_fusion


def test_fusion_combines_scores_without_mutating_inputs():
    vector_results = [
        {
            "chunk_id": "a",
            "text": "文档 A",
            "metadata": {"source": "a.md"},
            "vector_distance": 0.1,
        },
        {
            "chunk_id": "b",
            "text": "文档 B",
            "metadata": {"source": "b.md"},
            "vector_distance": 0.2,
        },
    ]
    keyword_results = [
        {
            "chunk_id": "b",
            "text": "文档 B",
            "metadata": {"source": "b.md"},
            "bm25_score": 3.0,
        },
        {
            "chunk_id": "a",
            "text": "文档 A",
            "metadata": {"source": "a.md"},
            "bm25_score": 2.0,
        },
    ]

    result = reciprocal_rank_fusion(
        [vector_results, keyword_results],
        k=1,
    )

    assert [item["chunk_id"] for item in result] == ["a", "b"]
    assert result[0]["rrf_score"] == 1 / 2 + 1 / 3
    assert result[0]["vector_distance"] == 0.1
    assert result[0]["bm25_score"] == 2.0
    assert result[0]["metadata"] == {"source": "a.md"}
    assert "rrf_score" not in vector_results[0]
    assert "rrf_score" not in keyword_results[1]


def test_fusion_returns_empty_list_for_empty_rank_lists():
    assert reciprocal_rank_fusion([]) == []
    assert reciprocal_rank_fusion([[]]) == []
