"""Reading the end of a log file without reading all of it.

The work is bounded whatever the file holds: at most :data:`MAX_SCAN_BYTES`
are read from the end, in blocks whose newlines are counted once each, and
every returned line is cut to :data:`MAX_LINE_BYTES` with a marker. A file
that is one 200 MB line costs the same as a short one.
"""

from __future__ import annotations

import os
from pathlib import Path

BLOCK_BYTES = 64 * 1024
MAX_LINE_BYTES = 4096
"""A longer line is cut here and ends with :data:`TRUNCATED`."""
MAX_SCAN_BYTES = 1000 * MAX_LINE_BYTES
"""At most this much of the end of the file is read (1000 full-length lines)."""
TRUNCATED = " [line truncated]"
CUT = "[...] "
"""Starts a line whose beginning lies before the bytes that were read."""


def tail_lines(path: Path, n: int) -> list[str]:
    """The last ``n`` lines of ``path``; empty if it cannot be read."""
    if n <= 0:
        return []
    blocks: list[bytes] = []
    newlines = 0
    try:
        with path.open("rb") as fh:
            pos = fh.seek(0, os.SEEK_END)
            end = pos
            # n + 1 newlines: the last one may end the file, and the first
            # full line starts after the one before it.
            while pos > 0 and newlines <= n and end - pos < MAX_SCAN_BYTES:
                step = min(BLOCK_BYTES, pos, MAX_SCAN_BYTES - (end - pos))
                pos -= step
                fh.seek(pos)
                block = fh.read(step)
                newlines += block.count(b"\n")
                blocks.append(block)
    except OSError:
        return []
    blocks.reverse()
    data = b"".join(blocks)
    del blocks
    if not data:
        return []
    # Line boundaries from the end, slicing at most MAX_LINE_BYTES per line
    # rather than splitting the whole buffer.
    stop = len(data) - 1 if data.endswith(b"\n") else len(data)
    lines: list[str] = []
    while len(lines) < n:
        start = data.rfind(b"\n", 0, stop) + 1
        text = _line(data, start, stop)
        if start == 0 and pos > 0:
            text = CUT + text
        lines.append(text)
        if start == 0:
            break
        stop = start - 1
    lines.reverse()
    return lines


def _line(data: bytes, start: int, stop: int) -> str:
    if stop > start and data[stop - 1 : stop] == b"\r":
        stop -= 1
    if stop - start <= MAX_LINE_BYTES:
        return data[start:stop].decode("utf-8", "replace")
    return data[start : start + MAX_LINE_BYTES].decode("utf-8", "ignore") + TRUNCATED
