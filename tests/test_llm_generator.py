from types import SimpleNamespace

import pytest

import rag.llm_generator as llm_module
from rag.llm_generator import LLMGenerator


class FakeCompletions:
    def __init__(self, content=None, error=None):
        self.content = content
        self.error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))]
        )


class FakeClient:
    def __init__(self, completions):
        self.chat = SimpleNamespace(completions=completions)


def build_generator(monkeypatch, completions):
    client = FakeClient(completions)
    monkeypatch.setenv("DEEP_SEEK_KEY", "test-key")
    monkeypatch.setattr(llm_module, "OpenAI", lambda **_: client)
    return LLMGenerator(), completions


def test_generate_returns_fallback_without_calling_llm(monkeypatch):
    generator, completions = build_generator(monkeypatch, FakeCompletions())

    result = generator.generate("没有资料的问题", [])

    assert result == "未找到相关资料。"
    assert completions.calls == []


def test_generate_builds_prompt_and_returns_model_content(monkeypatch):
    generator, completions = build_generator(
        monkeypatch,
        FakeCompletions(content="这是基于资料的回答"),
    )
    contexts = [
        {
            "chunk_id": "chunk-1",
            "text": "RAG 先检索再生成。",
            "metadata": {"source": "guide.md"},
        }
    ]

    result = generator.generate("什么是 RAG？", contexts)

    assert result == "这是基于资料的回答"
    request = completions.calls[0]
    assert request["model"] == "deepseek-chat"
    assert request["stream"] is False
    prompt = request["messages"][0]["content"]
    assert "什么是 RAG？" in prompt
    assert "chunk-1" in prompt
    assert "RAG 先检索再生成。" in prompt


def test_generate_handles_empty_model_content(monkeypatch):
    generator, _ = build_generator(monkeypatch, FakeCompletions(content=None))

    result = generator.generate(
        "问题",
        [{"chunk_id": "chunk-1", "text": "资料", "metadata": {}}],
    )

    assert result == "模型未返回有效内容。"


def test_generate_hides_provider_errors(monkeypatch):
    generator, _ = build_generator(
        monkeypatch,
        FakeCompletions(error=RuntimeError("provider unavailable")),
    )

    result = generator.generate(
        "问题",
        [{"chunk_id": "chunk-1", "text": "资料", "metadata": {}}],
    )

    assert result == "服务暂时不可用，请稍后再试"


def test_generator_requires_api_key(monkeypatch):
    monkeypatch.delenv("DEEP_SEEK_KEY", raising=False)
    monkeypatch.setattr(llm_module, "load_dotenv", lambda *_: None)

    with pytest.raises(ValueError, match="DEEP_SEEK_KEY"):
        LLMGenerator()
