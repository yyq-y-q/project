from pathlib import Path

# ---------------------------------------------------------------------------
# 路径锚点（与 rag/config 同一规则）
#
#   config.py: <项目根>/src/sqlite_tool/config.py
#   parents[2] = 项目根
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
SQLITE_DIR = DATA_DIR / "sqlite"
# 原件仓：txt → RAG；xlsx/csv → 结构化线路（按扩展名分流）
RAW_DIR = DATA_DIR / "raw"


class Config:
    # 只读业务库（查询网关读这个）
    QUERY_PATH = str(SQLITE_DIR / "query.db")
    # 审计日志库（可写；与业务库物理隔离）
    LOGGER_PATH = str(SQLITE_DIR / "logger.db")

    # 结构化原件默认目录
    RAW_DIR = RAW_DIR

    MAX_ROWS = 10
    TIME_OUT = 5
