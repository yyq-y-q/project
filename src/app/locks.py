"""
并发锁 / 隔离锁

职责：
  - 进程内 RW：多读者一写者（索引查询 vs 重建）
  - 跨进程 FileLock：多 worker / 多 CLI 互斥写同一份落盘资源
  - 按资源命名：rag_index / sqlite_query / sqlite_logger / session 文件

不在这里做业务；只提供锁原语与命名入口。
"""
from __future__ import annotations

import os
import sys
import time
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# ---------------------------------------------------------------------------
# 路径：data/locks/（与 rag/sqlite 共用项目根 data）
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOCK_DIR = _PROJECT_ROOT / "data" / "locks"


def _ensure_lock_dir() -> Path:
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    return LOCK_DIR


def lock_path(name: str) -> Path:
    """把逻辑名收成安全文件名。"""
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name).strip("._") or "anon"
    return _ensure_lock_dir() / f"{safe}.lock"


# ---------------------------------------------------------------------------
# 跨进程互斥（独占）
#   Unix  : fcntl.flock
#   Windows: msvcrt.locking（按字节锁；此处锁文件首字节）
# ---------------------------------------------------------------------------
class FileLock:
    """
    跨进程独占文件锁。

    用法:
      with FileLock("rag_index"):
          ... 写索引 ...
    """

    def __init__(
        self,
        name: str,
        *,
        timeout: float | None = 60.0,
        poll_interval: float = 0.05,
    ):
        self.name = name
        self.path = lock_path(name)
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # a+b：不存在则建；读写二进制，兼容 msvcrt
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        try:
            while True:
                try:
                    self._lock_fd(fd, blocking=False)
                    # 占住首字节内容，便于人工排查
                    try:
                        os.lseek(fd, 0, os.SEEK_SET)
                        os.write(fd, f"pid={os.getpid()}\n".encode("utf-8"))
                    except OSError:
                        pass
                    self._fd = fd
                    return
                except (BlockingIOError, OSError):
                    if deadline is not None and time.monotonic() >= deadline:
                        os.close(fd)
                        raise TimeoutError(
                            f"获取文件锁超时: {self.name} ({self.path})"
                        )
                    time.sleep(self.poll_interval)
        except Exception:
            if self._fd is None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        try:
            self._unlock_fd(fd)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            self._fd = None

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    @staticmethod
    def _lock_fd(fd: int, *, blocking: bool) -> None:
        if sys.platform == "win32":
            import msvcrt

            # LK_NBLCK / LK_LOCK：锁 1 字节；非阻塞失败抛 OSError
            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
            os.lseek(fd, 0, os.SEEK_SET)
            # 文件至少 1 字节，否则部分 Windows 上 locking 行为不稳定
            try:
                size = os.fstat(fd).st_size
            except OSError:
                size = 0
            if size < 1:
                os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, mode, 1)
        else:
            import fcntl

            flags = fcntl.LOCK_EX
            if not blocking:
                flags |= fcntl.LOCK_NB
            fcntl.flock(fd, flags)

    @staticmethod
    def _unlock_fd(fd: int) -> None:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def exclusive(name: str, *, timeout: float | None = 60.0) -> Iterator[FileLock]:
    """命名跨进程独占锁。"""
    lock = FileLock(name, timeout=timeout)
    lock.acquire()
    try:
        yield lock
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# 进程内读写锁
#   多线程 query 可并行读；build 独占写；写时阻塞新读，读时阻塞写
# ---------------------------------------------------------------------------
class RWLock:
    """读者-写者锁（进程内）。写者优先，避免重建饿死。"""

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    def acquire_read(self) -> None:
        with self._cond:
            while self._writer or self._waiting_writers:
                self._cond.wait()
            self._readers += 1

    def release_read(self) -> None:
        with self._cond:
            self._readers -= 1
            if self._readers == 0:
                self._cond.notify_all()

    def acquire_write(self) -> None:
        with self._cond:
            self._waiting_writers += 1
            try:
                while self._writer or self._readers:
                    self._cond.wait()
                self._writer = True
            finally:
                self._waiting_writers -= 1

    def release_write(self) -> None:
        with self._cond:
            self._writer = False
            self._cond.notify_all()

    @contextmanager
    def read_lock(self) -> Iterator[None]:
        self.acquire_read()
        try:
            yield
        finally:
            self.release_read()

    @contextmanager
    def write_lock(self) -> Iterator[None]:
        self.acquire_write()
        try:
            yield
        finally:
            self.release_write()


# ---------------------------------------------------------------------------
# 命名资源锁（全进程单例表）
# ---------------------------------------------------------------------------
_registry_guard = threading.Lock()
_rw_locks: dict[str, RWLock] = {}


def get_rw_lock(name: str) -> RWLock:
    with _registry_guard:
        lock = _rw_locks.get(name)
        if lock is None:
            lock = RWLock()
            _rw_locks[name] = lock
        return lock


# ---- 业务资源名（约定，避免魔法字符串散落）----
RAG_INDEX = "rag_index"
SQLITE_QUERY_DB = "sqlite_query_db"
SQLITE_LOGGER_DB = "sqlite_logger_db"


def rag_rw() -> RWLock:
    return get_rw_lock(RAG_INDEX)


@contextmanager
def rag_index_read() -> Iterator[None]:
    """
    读索引：仅进程内 read 锁。
    跨进程重建由写侧 file lock 串行；读侧不抢独占，避免 query 互斥。
    """
    with rag_rw().read_lock():
        yield


@contextmanager
def rag_index_write(*, timeout: float | None = 120.0) -> Iterator[None]:
    """
    写/重建索引：
      1) 跨进程 FileLock — 多 worker 同时 rebuild 互斥
      2) 进程内 write 锁 — 挡本进程 query
    """
    with exclusive(RAG_INDEX, timeout=timeout):
        with rag_rw().write_lock():
            yield


@contextmanager
def sqlite_query_write(*, timeout: float | None = 120.0) -> Iterator[None]:
    """ingest 写 query.db：跨进程串行。"""
    with exclusive(SQLITE_QUERY_DB, timeout=timeout):
        yield


@contextmanager
def sqlite_logger_write(*, timeout: float | None = 30.0) -> Iterator[None]:
    """审计 logger.db 写入串行（多线程 query 同时记日志）。"""
    with exclusive(SQLITE_LOGGER_DB, timeout=timeout):
        yield


def session_lock_name(session_id: str) -> str:
    """同一 session 的跨进程文件名。"""
    safe = "".join(
        c if c.isalnum() or c in "-_" else "_" for c in (session_id or "default")
    )[:64]
    return f"session_{safe or 'default'}"


@contextmanager
def session_file_lock(session_id: str, *, timeout: float | None = 30.0) -> Iterator[None]:
    """
    同一 session 历史文件跨进程互斥。
    不同 session 各锁各的，可并发。
    """
    with exclusive(session_lock_name(session_id), timeout=timeout):
        yield


