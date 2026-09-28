"""结构化摄取子包：原件 → 提取 → 清洗 → 校验 → 归库。"""

from .pipeline import ingest_dir, ingest_to_validated, run_ingest

__all__ = ["run_ingest", "ingest_to_validated", "ingest_dir"]
