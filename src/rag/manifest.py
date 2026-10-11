"""
增量索引基线：文件指纹清单 + chunks 缓存，全部原子读写。

- index_manifest.json: doc_id -> {path, hash, mtime, chunk_ids, chunk_count}
- chunks.json: 全量 chunk 列表缓存（BM25 重建的语料源，也用于失败恢复）
"""
import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path

from .config import Config, DATA_DIR

logger = logging.getLogger(__name__)

MANIFEST_PATH = DATA_DIR / "index_manifest.json"
CHUNKS_PATH = DATA_DIR / "chunks.json"


def _atomic_write_json(path: Path, data) -> None:
    """临时文件 + os.replace 原子替换；失败清理临时文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# 清单（manifest）
# ---------------------------------------------------------------------------


def load_manifest() -> dict:
    try:
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("manifest 解析失败，按空处理: %s", e)
        return {}


def save_manifest(manifest: dict) -> None:
    _atomic_write_json(MANIFEST_PATH, manifest)


# ---------------------------------------------------------------------------
# chunks 缓存
# ---------------------------------------------------------------------------


def load_chunks() -> list[dict]:
    try:
        with open(CHUNKS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []
    except Exception as e:
        logger.warning("chunks 缓存解析失败，按空处理: %s", e)
        return []


def save_chunks(chunks: list[dict]) -> None:
    _atomic_write_json(CHUNKS_PATH, chunks)


# ---------------------------------------------------------------------------
# 指纹与变更检测
# ---------------------------------------------------------------------------


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def detect_changes(manifest: dict, root: Path) -> tuple[list[Path], list[Path], list[str]]:
    """返回 (新增文件, 修改文件, 已删除 doc_id 列表)。"""
    from .loader import _stable_doc_id

    supported = {s.lower() for s in Config.RAG_DOC_SUFFIXES}
    added: list[Path] = []
    modified: list[Path] = []
    removed: list[str] = []
    seen: set[str] = set()

    if root.is_dir():
        for p in sorted(root.iterdir()):
            if not p.is_file():
                continue
            if p.suffix.lower() not in supported:
                continue
            try:
                doc_id = _stable_doc_id(p)
            except OSError as e:
                logger.warning("计算 doc_id 失败，跳过 %s: %s", p, e)
                continue
            seen.add(doc_id)
            entry = manifest.get(doc_id)
            if entry is None:
                added.append(p)
            else:
                try:
                    cur = file_hash(p)
                except OSError as e:
                    logger.warning("计算文件指纹失败，跳过 %s: %s", p, e)
                    continue
                if entry.get("hash") != cur:
                    modified.append(p)

    for doc_id in manifest:
        if doc_id not in seen:
            removed.append(doc_id)

    return added, modified, removed
