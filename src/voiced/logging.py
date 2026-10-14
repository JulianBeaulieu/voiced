"""Logging setup shared by both services and the CLI."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from voiced.config import LogLevel

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5


def configure_logging(
    service: str, level: LogLevel = "info", log_dir: Path | None = None
) -> logging.Logger:
    """Log to stderr and, if ``log_dir`` is given, to ``<log_dir>/<service>.log``.

    Under launchd stderr is also redirected to a file by the plist, so the file
    handler is what matters in production; stderr is for ``voiced serve`` in a
    terminal. Safe to call more than once: handlers are replaced, not stacked.
    """
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)
    formatter = logging.Formatter(FORMAT)

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / f"{service}.log", maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # uvicorn installs its own handlers; route them through ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True

    return logging.getLogger(f"voiced.{service}")
