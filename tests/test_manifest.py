"""增量索引基线测试：原子写、指纹、变更检测（新增/修改/删除/noop）。

覆盖 rag/manifest.py：路径通过 monkeypatch 指向临时目录，
detect_changes 只读取给定 root，均不触碰生产 data/ 目录。
"""
import json

import pytest

from rag import manifest
from rag.loader import _stable_doc_id
from rag.manifest import (
    file_hash,
    detect_changes,
    load_manifest,
    save_manifest,
    load_chunks,
    save_chunks,
)


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    """把 manifest/chunks 的落盘路径指到临时目录。"""
    monkeypatch.setattr(manifest, "MANIFEST_PATH", tmp_path / "index_manifest.json")
    monkeypatch.setattr(manifest, "CHUNKS_PATH", tmp_path / "chunks.json")
    return tmp_path


def _write(root, name, content):
    p = root / name
    p.write_text(content, encoding="utf-8")
    return p


class TestAtomicWrite:
    def test_save_load_roundtrip(self, isolated_paths):
        save_manifest({"doc1": {"hash": "abc", "chunk_count": 1}})
        save_chunks([{"chunk_id": "doc1_chunk_0", "text": "x"}])
        assert load_manifest() == {"doc1": {"hash": "abc", "chunk_count": 1}}
        assert load_chunks() == [{"chunk_id": "doc1_chunk_0", "text": "x"}]

    def test_missing_files_return_empty(self, isolated_paths):
        assert load_manifest() == {}
        assert load_chunks() == []

    def test_no_temp_files_left_after_write(self, isolated_paths):
        save_manifest({"a": {"hash": "h", "chunk_count": 1}})
        leftovers = list(isolated_paths.glob(".tmp_*"))
        assert leftovers == []


class TestFileHash:
    def test_hash_changes_with_content(self, tmp_path):
        p = _write(tmp_path, "a.txt", "hello")
        h1 = file_hash(p)
        p.write_text("hello world", encoding="utf-8")
        h2 = file_hash(p)
        assert h1 != h2
        assert len(h1) == 64  # sha256 hex


class TestDetectChanges:
    def test_all_new_files_detected_as_added(self, tmp_path):
        root = tmp_path / "raw"
        root.mkdir()
        _write(root, "a.txt", "alpha")
        _write(root, "b.md", "# beta\n\n内容")
        added, modified, removed = detect_changes({}, root)
        assert sorted(p.name for p in added) == ["a.txt", "b.md"]
        assert modified == [] and removed == []

    def test_modified_detected_by_hash(self, tmp_path):
        root = tmp_path / "raw"
        root.mkdir()
        p = _write(root, "a.txt", "v1")
        added, _, _ = detect_changes({}, root)
        assert len(added) == 1
        doc_id = _stable_doc_id(p)
        entry_hash = file_hash(p)
        manifest_map = {doc_id: {"hash": entry_hash, "chunk_count": 1}}
        # 内容未变 → 无变更
        added2, modified2, removed2 = detect_changes(manifest_map, root)
        assert added2 == [] and modified2 == [] and removed2 == []
        # 内容变化 → modified
        p.write_text("v2 changed", encoding="utf-8")
        _, modified3, _ = detect_changes(manifest_map, root)
        assert len(modified3) == 1

    def test_removed_doc_id_returned(self, tmp_path):
        root = tmp_path / "raw"
        root.mkdir()
        p = _write(root, "a.txt", "alpha")
        doc_id = _stable_doc_id(p)
        manifest_map = {doc_id: {"hash": file_hash(p), "chunk_count": 1}}
        p.unlink()
        _, _, removed = detect_changes(manifest_map, root)
        assert removed == [doc_id]

    def test_unsupported_suffix_ignored(self, tmp_path):
        root = tmp_path / "raw"
        root.mkdir()
        _write(root, "a.png", "\x89PNG")
        added, _, _ = detect_changes({}, root)
        assert added == []
