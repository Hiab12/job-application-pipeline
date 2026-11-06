"""Logging configuration: readable console output + a rotating log file."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
LOG_FILE_NAME = "pipeline.log"

# Third-party libraries that log every HTTP request at INFO level.
NOISY_LOGGERS = ("httpx", "httpx2", "httpcore", "urllib3", "anthropic", "notion_client")


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    """Configure the root logger.

    Safe to call more than once (existing handlers are replaced), which keeps tests clean.

    Args:
        level: Minimum level for the pipeline's own messages.
        log_dir: If given, also write logs to ``log_dir/pipeline.log`` (1 MB x 5 files).
    """
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / LOG_FILE_NAME, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
