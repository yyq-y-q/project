import json

import pytest

from rag.bm25 import BM25Index
from rag.config import Config


def sample_chunks():
    return [
        {
            "chunk_id": "fruit",
            "text": "苹果是一种常见水果。",
            "metadata": {"source": "fruit.md"},
        },
        {
            "chunk_id": "vehicle",
            "text": "汽车需要定期保养。",
            "metadata": {"source": "vehicle.md"},
        },
        {
            "chunk_id": "database",
            "text": "SQLite 适合轻量级数据查询。",
            "metadata": {"source": "database.md"},
        },
    ]


def test_retrieve_requires_an_index():
    index = BM25Index()

    with pytest.raises(RuntimeError, match="BM25 索引未加载"):
        index.retrieve("苹果")


def test_build_persists_index_and_retrieve_does_not_mutate_corpus(
    monkeypatch,
    tmp_path,
):
    index_path = tmp_path / "bm25_index.json"
    monkeypatch.setattr(Config, "BM25_PATH", index_path)
    chunks = sample_chunks()

    index = BM25Index()
    index.build(chunks)

    assert index_path.exists()
    persisted = json.loads(index_path.read_text(encoding="utf-8"))
    assert persisted["corpus"] == chunks
    assert len(persisted["tokens"]) == len(chunks)

    docs, scores = index.retrieve("苹果", top_k=1)

    assert [doc["chunk_id"] for doc in docs] == ["fruit"]
    assert len(scores) == 1
    assert docs[0]["bm25_score"] == scores[0]
    assert "bm25_score" not in chunks[0]


def test_load_recreates_searchable_index(monkeypatch, tmp_path):
    index_path = tmp_path / "bm25_index.json"
    monkeypatch.setattr(Config, "BM25_PATH", index_path)
    chunks = sample_chunks()

    BM25Index().build(chunks)
    loaded = BM25Index()

    assert loaded.load() is True
    docs, _ = loaded.retrieve("SQLite", top_k=1)

    assert docs[0]["chunk_id"] == "database"
    assert docs[0]["metadata"] == {"source": "database.md"}


def test_load_returns_false_when_index_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "BM25_PATH", tmp_path / "missing.json")

    assert BM25Index().load() is False


def test_clear_resets_memory_and_removes_persisted_index(monkeypatch, tmp_path):
    index_path = tmp_path / "bm25_index.json"
    monkeypatch.setattr(Config, "BM25_PATH", index_path)
    index = BM25Index()
    index.build(sample_chunks())

    index.clear()

    assert index.corpus == []
    assert index.tokenized_corpus == []
    assert index.bm25 is None
    assert not index_path.exists()
