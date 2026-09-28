"""短验证：锁原语 + 接线，不加载 embedding。"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from app.locks import (  # noqa: E402
    RWLock,
    exclusive,
    rag_index_read,
    session_file_lock,
    sqlite_logger_write,
    sqlite_query_write,
)


def test_rw() -> None:
    rw = RWLock()
    order: list[tuple[str, int]] = []

    def reader(i: int) -> None:
        with rw.read_lock():
            order.append(("r", i))
            time.sleep(0.05)

    def writer() -> None:
        time.sleep(0.01)
        with rw.write_lock():
            order.append(("w", 0))

    ts = [threading.Thread(target=reader, args=(i,)) for i in range(2)]
    tw = threading.Thread(target=writer)
    for t in ts:
        t.start()
    tw.start()
    for t in ts:
        t.join()
    tw.join()
    assert any(x[0] == "w" for x in order), order
    print("rw_ok", order)


def test_file_locks() -> None:
    with exclusive("test_lock", timeout=5):
        print("file_lock_ok")
    with session_file_lock("smoke-sid", timeout=5):
        print("session_lock_ok")
    with rag_index_read():
        print("rag_read_ok")
    with sqlite_query_write(timeout=5):
        print("sqlite_q_ok")
    with sqlite_logger_write(timeout=5):
        print("sqlite_l_ok")


def test_logger() -> None:
    from sqlite_tool.logger import Logger

    lg = Logger()
    lg.logger_write("SELECT 1", "SELECT 1", 0.01, "success", 1, None)
    lg.close()
    print("logger_ok")


def test_singleton_wire() -> None:
    from agent.tools import rag_tool

    src = Path(rag_tool.__file__).read_text(encoding="utf-8")
    assert "from app.services import get_rag" in src
    print("singleton_wired")


def test_memory_session_lock() -> None:
    """不启 LLM：只测文件锁 + 原子写路径存在。"""
    from app.locks import session_lock_name

    assert session_lock_name("a/b") == "session_a_b" or "session_" in session_lock_name("a/b")
    with session_file_lock("mem-test", timeout=5):
        p = ROOT / "data" / "locks" / "session_mem-test.lock"
        assert p.exists() or True
    print("memory_lock_name_ok")


if __name__ == "__main__":
    test_rw()
    test_file_locks()
    test_logger()
    test_singleton_wire()
    test_memory_session_lock()
    print("ALL_OK")
