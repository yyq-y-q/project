"""
统一日志：默认写入容器/服务标准输出；可显式启用滚动文件日志。
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LOG_DIR = _PROJECT_ROOT / "data" / "logs"
_configured = False


def setup_logging(level: int = logging.INFO, to_file: bool | None = None) -> None:
    global _configured
    if _configured:
        return
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    if to_file is None:
        to_file = os.getenv("KB_LOG_TO_FILE", "0").strip().lower() in {
            "1", "true", "yes", "on"
        }
    if to_file:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            _LOG_DIR / "app.log",
            maxBytes=5_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
