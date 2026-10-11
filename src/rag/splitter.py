import re
import logging

from langchain_text_splitters import RecursiveCharacterTextSplitter

from .config import Config

logger = logging.getLogger(__name__)

# Markdown 标题行：# ~ ###### + 空格 + 标题
_HEADING_RE = re.compile(r"^#{1,6}\s+\S.*$", re.MULTILINE)
# 段落分隔：一个及以上空行
_PARAGRAPH_SPLIT = re.compile(r"\n\s*\n")

_tiktoken_enc = None


def _est_tokens(text: str) -> int:
    """用与切分器一致的 token 口径估算长度；tiktoken 不可用时按字符/2 兜底。"""
    global _tiktoken_enc
    try:
        if _tiktoken_enc is None:
            import tiktoken

            _tiktoken_enc = tiktoken.encoding_for_model(Config.TOKENIZER_MODEL)
        return len(_tiktoken_enc.encode(text))
    except Exception:
        return len(text) // 2


def _make_splitter() -> RecursiveCharacterTextSplitter:
    """超长段落兜底切分器（配置驱动：CHUNK_SIZE / CHUNK_OVERLAP / SEPARATORS）。"""
    return RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        chunk_size=Config.CHUNK_SIZE,
        chunk_overlap=Config.CHUNK_OVERLAP,
        separators=Config.SEPARATORS,
        model_name=Config.TOKENIZER_MODEL,
    )


def _split_sections(text: str) -> list[dict]:
    """
    按 Markdown 标题层级切 section。
    返回 [{title_chain: [标题…], body: 正文}]；无标题文档返回单个空链 section。
    """
    sections: list[dict] = []
    chain: list[dict] = []  # [{"level": int, "title": str}]
    body: list[str] = []

    def flush():
        content = "\n".join(body).strip()
        if content:
            sections.append({"title_chain": [t["title"] for t in chain], "body": content})
        body.clear()

    for line in text.splitlines():
        m = _HEADING_RE.match(line.strip())
        if m:
            flush()
            level = len(line.strip()) - len(line.strip().lstrip("#"))
            title = line.strip()[level:].strip()
            # 标题链：同级或更深标题开启新节，仅保留更浅的祖先标题
            while chain and chain[-1]["level"] >= level:
                chain.pop()
            chain.append({"level": level, "title": title})
        else:
            body.append(line)
    flush()
    if not sections:
        sections.append({"title_chain": [], "body": text.strip()})
    return sections


def _chunk_section(section: dict) -> list[tuple[str, list[str]]]:
    """
    把一个 section 切成 (chunk_text, title_chain) 列表：
      - 短段贪心合并，接近 CHUNK_SIZE 才成块，减少碎片；
      - 超长段（> CHUNK_SIZE）交给递归切分器兜底；
      - 标题链注入 chunk 文本首行，提升召回语义。
    """
    chain = section["title_chain"]
    prefix = " | ".join(chain) if chain else ""
    body = section["body"]
    if not body:
        return []

    paragraphs = [p.strip() for p in _PARAGRAPH_SPLIT.split(body) if p.strip()]
    if not paragraphs:
        paragraphs = [body.strip()]

    out: list[tuple[str, list[str]]] = []
    buf: list[str] = []
    buf_tokens = 0
    splitter = _make_splitter()

    def flush_buf():
        nonlocal buf, buf_tokens
        if buf:
            text = "\n".join(buf)
            out.append((f"{prefix}\n{text}" if prefix else text, chain))
            buf, buf_tokens = [], 0

    for para in paragraphs:
        n = _est_tokens(para)
        if n > Config.CHUNK_SIZE:
            flush_buf()
            for piece in splitter.split_text(para):
                out.append((f"{prefix}\n{piece}" if prefix else piece, chain))
            continue
        if buf and buf_tokens + n > Config.CHUNK_SIZE:
            flush_buf()
        buf.append(para)
        buf_tokens += n
        if buf_tokens >= Config.CHUNK_SIZE * 0.9:
            flush_buf()
    flush_buf()
    return out


def split_documents(documents: list[str], doc_metadata: list[dict] | None = None) -> list[dict]:
    """
    结构感知切块：
      - Markdown（.md/.markdown 或含标题行）按标题层级切 section，段落合并成块；
      - 其他文本按段落切，超长段递归兜底；
      - chunk_id 保持 {doc_id}_chunk_{index} 格式（cite_ids 依赖），
        但跨 section 全局连续编号，块内顺序稳定。
      - metadata 增加 title_hierarchy / section（供引用展示与召回语义）。
    """
    results: list[dict] = []

    for doc_index, doc in enumerate(documents):
        base_meta = doc_metadata[doc_index].copy() if doc_metadata else {}
        doc_id = base_meta.get("doc_id", doc_index)
        base_meta["doc_id"] = doc_id

        source = str(base_meta.get("source") or base_meta.get("filename") or doc_id)
        is_markdown = source.lower().endswith((".md", ".markdown")) or bool(_HEADING_RE.search(doc))

        if is_markdown:
            for sec in _split_sections(doc):
                chunks = _chunk_section(sec)
                for text, chain in chunks:
                    meta = base_meta.copy()
                    if chain:
                        meta["title_hierarchy"] = chain
                        meta["section"] = chain[-1]
                    results.append({"chunk_id": f"{doc_id}_chunk_{len(results)}", "text": text, "metadata": meta})
        else:
            # 非 Markdown：整篇视为单 section，段落贪心合并 + 超长段递归兜底
            for text, _chain in _chunk_section({"title_chain": [], "body": doc}):
                results.append(
                    {"chunk_id": f"{doc_id}_chunk_{len(results)}", "text": text, "metadata": base_meta.copy()}
                )

    logger.info("切分完成:%s chunks", len(results))
    return results
