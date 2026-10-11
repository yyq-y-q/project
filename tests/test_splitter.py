"""结构感知切块测试：Markdown 标题层级、段落合并、超长兜底、chunk_id 契约。

覆盖 rag/splitter.py 的完整行为，不依赖真实模型与网络，可离线运行。
"""
import pytest

from rag.splitter import split_documents, _est_tokens


class TestStructureAwareChunking:
    def test_markdown_sections_and_title_injection(self):
        md = """# 员工手册
本手册适用于全体在职员工。

## 考勤制度
员工每日需打卡两次。
迟到超过 30 分钟记一次迟到。

## 请假流程
请假需提前一天提交申请。
紧急情况可电话报备。
"""
        chunks = split_documents([md], [{"doc_id": "doc1", "source": "手册.md"}])
        assert len(chunks) >= 2

        # chunk_id 契约：doc_id_chunk_N，从 0 连续编号
        assert all(c["chunk_id"].startswith("doc1_chunk_") for c in chunks)
        assert [c["chunk_id"] for c in chunks] == [
            f"doc1_chunk_{i}" for i in range(len(chunks))
        ]

        # 标题链注入 metadata + 文本前缀
        assert any("title_hierarchy" in c["metadata"] for c in chunks)
        assert any(
            c["metadata"].get("section") in ("考勤制度", "请假流程", "员工手册")
            for c in chunks
        )

    def test_short_paragraphs_merged_within_section(self):
        md = """# 考勤制度
员工每日需打卡两次。
迟到超过 30 分钟记一次迟到。
"""
        chunks = split_documents([md], [{"doc_id": "doc1", "source": "考勤.md"}])
        kq = [c for c in chunks if c["metadata"].get("section") == "考勤制度"]
        assert len(kq) == 1
        assert "迟到超过 30 分钟" in kq[0]["text"]

    def test_overlong_paragraph_split_within_limit(self):
        long_para = "这是超长段落。" * 400  # ~2800 tokens
        chunks = split_documents(
            [f"# 长文\n\n{long_para}"], [{"doc_id": "doc2", "source": "长文.md"}]
        )
        assert len(chunks) >= 3
        assert all(_est_tokens(c["text"]) <= 512 + 128 for c in chunks)

    def test_plain_text_paragraph_merge(self):
        txt = "第一段内容。\n第二段内容。\n第三段内容。"
        chunks = split_documents([txt], [{"doc_id": "doc3", "source": "a.txt"}])
        assert len(chunks) == 1

    def test_empty_document_yields_no_chunks(self):
        chunks = split_documents([""], [{"doc_id": "doc4", "source": "b.txt"}])
        assert len(chunks) == 0

    def test_missing_metadata_fallback(self):
        chunks = split_documents(["hello world. hello again."])
        assert chunks[0]["chunk_id"].startswith("0_chunk_")
