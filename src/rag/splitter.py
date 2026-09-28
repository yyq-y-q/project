import logging

from langchain_text_splitters import RecursiveCharacterTextSplitter

from .config import Config

logger = logging.getLogger(__name__)

def split_documents(
    
    documents:list[str],
    doc_metadata:list[dict]| None = None
)->list[dict]:
    """
    当前这段代码的 doc_index fallback 并不是一个严格的唯一 ID 方案。

不过我们现在是在做**“原代码纯分项”**，所以这不是现在修改的地方。你已经发现了一个未来做生产化时应该解决的问题。

等我们后面做到文件摄取 / 文档 ID 管理时，这个问题就要处理：

文件上传
 ↓
生成稳定唯一 doc_id
 ↓
Splitter 只负责使用这个 doc_id
 ↓
doc_id + chunk_index
 ↓
稳定唯一 chunk_id

这比现在拿 doc_index 当 fallback 更合理。
    按 Token 切分文档，并返回每个块的元数据。
    :param docs: 文档文本列表
    :param doc_metadata: 与 docs 一一对应的元数据列表（如文件名、页码等），若不提供，则用默认索引
    """
    splitter = (RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        chunk_size=Config.CHUNK_SIZE,
        chunk_overlap=Config.CHUNK_OVERLAP,
        separators=Config.SEPARATORS,
        model_name=Config.TOKENIZER_MODEL
    )
    )
    results=[]

    for doc_index, doc in enumerate(documents):
        # 当前文档的元数据基础（可传入外部信息）
        base_meta = doc_metadata[doc_index].copy() if doc_metadata else {}

        doc_id = base_meta.get(
        "doc_id",
        doc_index
        )
        base_meta["doc_id"] = doc_id
        chunks = splitter.split_text(doc)

        for index, chunk in enumerate(chunks):

            chunk_id = (
                f"{doc_id}"
                f"_chunk_{index}"
            )

            results.append(
                {
                    "chunk_id": chunk_id,
                    "text": chunk,
                    "metadata": base_meta.copy()
                }
        )



    logger.info(
        f"切分完成:{len(results)} chunks"
    )


    return results