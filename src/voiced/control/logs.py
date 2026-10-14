"""Reading the end of a log file without reading all of it."""

from __future__ import annotations

import os
from pathlib import Path


def tail_lines(path: Path, n: int) -> list[str]:
    """The last ``n`` lines of ``path``; empty if it cannot be read."""
    if n <= 0:
        return []
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            block = 8192
            data = b""
            while size > 0 and data.count(b"\n") <= n:
                step = min(block, size)
                size -= step
                fh.seek(size)
                data = fh.read(step) + data
    except OSError:
        return []
    return data.decode("utf-8", "replace").splitlines()[-n:]
