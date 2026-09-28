import rag.model_manager as model_module
from rag.model_manager import ModelManager


def test_embedding_model_is_created_once(monkeypatch):
    calls = []

    class FakeEmbeddingModel:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr(model_module, "SentenceTransformer", FakeEmbeddingModel)
    monkeypatch.setattr(ModelManager, "_embedding_model", None)

    first = ModelManager.get_embedding_model()
    second = ModelManager.get_embedding_model()

    assert first is second
    assert calls == [(
        ("BAAI/bge-m3",),
        {"device": "cpu"},
    )]


def test_rerank_model_is_created_once_with_fixed_max_length(monkeypatch):
    calls = []

    class FakeRerankModel:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr(model_module, "CrossEncoder", FakeRerankModel)
    monkeypatch.setattr(ModelManager, "_rerank_model", None)

    first = ModelManager.get_rerank_model()
    second = ModelManager.get_rerank_model()

    assert first is second
    assert calls == [(
        ("BAAI/bge-reranker-v2-m3",),
        {"device": "cpu", "max_length": 512},
    )]
