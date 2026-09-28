from pathlib import Path


# ---------------------------------------------------------------------------
# 路径锚点（整份 Config 里「唯一」允许碰 __file__ 的地方）
#
#   config.py 真实位置:  <项目根>/src/rag/config.py
#   parents[2] = 项目根
#
# 规则：运行时文件 = 项目根算出来的绝对 Path；业务参数仍是普通常量。
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"


class Config:
    """
    配置：摄取 / 分块 / 嵌入重排 / 向量与 BM25 / 召回 / LLM / 记忆会话
    """

    # ---------- 摄取（原始文件；与索引产物分开）----------
    RAW_DOCS_DIR = DATA_DIR / "raw"
    # RAG：非结构化叙述。表 xlsx/csv → sqlite_tool（禁止双写）
    RAG_DOC_SUFFIXES = (
        ".txt",
        ".md",
        ".markdown",
        ".docx",
        ".pdf",
    )

    # ---------- 文档分块 ----------
    CHUNK_SIZE = 512
    CHUNK_OVERLAP = 50
    SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", "，", "、", " ", ""]
    TOKENIZER_MODEL = "gpt-4o"

    # ---------- 嵌入模型 ----------
    EMBEDDING_MODEL = "BAAI/bge-m3"
    EMBEDDING_DEVICE = "cpu"

    # ---------- 重排模型 ----------
    RERANK_MODEL = "BAAI/bge-reranker-v2-m3"
    RERANK_DEVICE = "cpu"

    # ---------- 向量数据库 ----------
    CHROMA_PERSIST_DIR = str(DATA_DIR / "chroma_db")
    CHROMA_COLLECTION_NAME = "knowledge_base"

    # ---------- BM25 索引 ----------
    BM25_PATH = DATA_DIR / "bm25_index.json"

    # ---------- 召回参数 ----------
    RETRIEVAL_TOP_K = 30
    RERANK_TOP_K = 3
    RRF_K = 60

    # ---------- LLM 生成 ----------
    LLM_MODEL = "deepseek-chat"
    LLM_BASE_URL = "https://api.deepseek.com"
    LLM_API_KEY_ENV = "DEEP_SEEK_KEY"

    ENV_PATH = PROJECT_ROOT / ".env"

    # ---------- 记忆（按 session 隔离文件，支持并发会话）----------
    MAX_HISTORY_ROUNDS = 5
    HISTORY_DIR = DATA_DIR / "sessions"
    HISTORY_PERSIST_PATH = DATA_DIR / "history.json"  # 兼容旧默认 session
    ENABLE_HISTORY_SUMMARY = False
