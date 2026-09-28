import sqlite3
from contextlib import contextmanager
from typing import Iterator

from .config import Config

try:
    from app.locks import sqlite_logger_write
except ImportError:

    @contextmanager
    def sqlite_logger_write(*, timeout: float | None = 30.0) -> Iterator[None]:
        yield


class Logger:
    """
    SQL 审计库写入。

    注意：每次 write 用短连接 + 跨进程锁，避免多线程共享一个 sqlite3 连接。
    """

    def __init__(self):
        self.path = Config.LOGGER_PATH
        self._ensure_table()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30.0)

    def _ensure_table(self) -> None:
        with sqlite_logger_write():
            con = self._connect()
            try:
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS logger(
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        first_sql TEXT NOT NULL,
                        final_sql TEXT,
                        create_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        duration REAL,
                        status TEXT,
                        return_rows INTEGER,
                        error TEXT
                    )
                    """
                )
                con.commit()
            finally:
                con.close()

    def create_table(self):
        self._ensure_table()

    def logger_write(
        self,
        first_sql,
        final_sql=None,
        duration=None,
        status="False",
        return_row=None,
        error=None,
    ):
        with sqlite_logger_write():
            con = self._connect()
            try:
                con.execute(
                    """
                    INSERT INTO logger(
                        first_sql,
                        final_sql,
                        duration,
                        status,
                        return_rows,
                        error
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        first_sql,
                        final_sql,
                        duration,
                        status,
                        return_row,
                        error,
                    ),
                )
                con.commit()
            finally:
                con.close()

    def close(self):
        # 短连接模式：无长连可关；保留方法兼容 query.py finally
        return