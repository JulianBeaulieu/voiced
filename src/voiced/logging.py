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
    service: str,
    level: LogLevel = "info",
    log_dir: Path | None = None,
    *,
    interactive: bool | None = None,
) -> logging.Logger:
    """Log to ``<log_dir>/<service>.log`` (rotated) and, when someone watches, to stderr.

    Without ``log_dir`` everything goes to stderr. With it, the rotating file
    gets every line and stderr gets them too only when it is a terminal
    (``interactive``, default ``sys.stderr.isatty()``): ``voiced serve`` run by
    hand. Under launchd stderr is a file the plist names, which launchd never
    rotates, so nothing is logged there; it keeps only what bypasses logging,
    such as a traceback from a crash before or outside it (ADR 0006). Safe to
    call more than once: handlers are replaced, not stacked.
    """
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)
    formatter = logging.Formatter(FORMAT)

    if interactive is None:
        interactive = sys.stderr.isatty()
    if log_dir is None or interactive:
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
