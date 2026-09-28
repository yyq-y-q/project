"""短验证：锁原语 + 源码接线（不加载 embedding / openpyxl）。"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from app.locks import (  # noqa: E402
    RWLock,
    exclusive,
    rag_index_read,
    session_file_lock,
    sqlite_logger_write,
    sqlite_query_write,
)


def test_rwlock() -> None:
    rw = RWLock()
    order: list[str] = []

    def reader(tag: str) -> None:
        with rw.read_lock():
            order.append(f"{tag}_in")
            time.sleep(0.05)
            order.append(f"{tag}_out")

    def writer() -> None:
        time.sleep(0.01)
        with rw.write_lock():
            order.append("w_in")
            time.sleep(0.02)
            order.append("w_out")

    threads = [
        threading.Thread(target=reader, args=("r1",)),
        threading.Thread(target=reader, args=("r2",)),
        threading.Thread(target=writer),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert "w_in" in order and "w_out" in order
    # 写者应在两读者退出后进入（写者优先排队）
    assert order.index("w_in") > order.index("r1_out")
    assert order.index("w_in") > order.index("r2_out")
    print("rw_ok", order)


def test_file_locks() -> None:
    with exclusive("smoke_test", timeout=5):
        pass
    with session_file_lock("smoke_sid", timeout=5):
        pass
    with rag_index_read():
        pass
    with sqlite_query_write(timeout=5):
        pass
    with sqlite_logger_write(timeout=5):
        pass
    print("file_locks_ok")


def test_wiring_sources() -> None:
    checks = {
        "src/agent/tools/rag_tool.py": ["from app.services import get_rag"],
        "src/app/services.py": [
            "def get_rag(",
            "def set_rag_singleton(",
            "pipe = get_rag()",
        ],
        "src/rag/RAG.py": ["rag_index_write", "rag_index_read"],
        "src/rag/memory.py": ["session_file_lock"],
        "src/sqlite_tool/ingest/load.py": ["sqlite_query_write"],
        "src/sqlite_tool/logger.py": ["sqlite_logger_write"],
        "src/app/locks.py": [
            "class FileLock",
            "class RWLock",
            "def rag_index_write",
            "def session_file_lock",
        ],
    }
    for rel, needles in checks.items():
        text = (ROOT / rel).read_text(encoding="utf-8")
        for n in needles:
            assert n in text, f"missing {n!r} in {rel}"
    print("wiring_ok")


if __name__ == "__main__":
    test_rwlock()
    test_file_locks()
    test_wiring_sources()
    print("ALL_OK")
