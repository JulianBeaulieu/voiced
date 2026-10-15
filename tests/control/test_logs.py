"""``tail_lines``: bounded in time and memory whatever the log file holds."""

from __future__ import annotations

import time
import tracemalloc
from pathlib import Path

from voiced.control import logs
from voiced.control.logs import tail_lines


def test_last_lines(tmp_path: Path) -> None:
    path = tmp_path / "a.log"
    path.write_text("".join(f"line {i}\n" for i in range(5000)))
    assert tail_lines(path, 3) == ["line 4997", "line 4998", "line 4999"]
    assert tail_lines(path, 1000) == [f"line {i}" for i in range(4000, 5000)]
    path.write_text("one\ntwo")  # no final newline
    assert tail_lines(path, 5) == ["one", "two"]
    assert tail_lines(tmp_path / "missing.log", 5) == []
    assert tail_lines(path, 0) == []


def test_long_lines_are_truncated_with_a_marker(tmp_path: Path) -> None:
    path = tmp_path / "a.log"
    long = "x" * (logs.MAX_LINE_BYTES * 3)
    path.write_text(f"first\n{long}\nlast\n")
    lines = tail_lines(path, 3)
    assert lines[0] == "first" and lines[2] == "last"
    assert lines[1].startswith("x" * logs.MAX_LINE_BYTES)
    assert lines[1].endswith(logs.TRUNCATED)
    assert len(lines[1]) < logs.MAX_LINE_BYTES + 64


def test_one_huge_line_is_bounded(tmp_path: Path) -> None:
    """A log that is one 32 MB line: a fraction of a second and a few MiB,
    not a scan of the whole file."""
    path = tmp_path / "a.log"
    with path.open("wb") as fh:
        chunk = b"y" * (1 << 20)
        for _ in range(32):
            fh.write(chunk)
    tracemalloc.start()
    started = time.perf_counter()
    try:
        lines = tail_lines(path, 1000)
        elapsed = time.perf_counter() - started
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert elapsed < 0.5
    assert peak < 3 * logs.MAX_SCAN_BYTES
    assert len(lines) == 1
    assert lines[0].startswith(logs.CUT) and lines[0].endswith(logs.TRUNCATED)
    assert len(lines[0]) < logs.MAX_LINE_BYTES + 64


def test_scan_stops_at_the_cap(tmp_path: Path) -> None:
    """Many long lines: only the last MAX_SCAN_BYTES are read."""
    path = tmp_path / "a.log"
    line = b"z" * (logs.MAX_SCAN_BYTES // 10) + b"\n"
    path.write_bytes(line * 40)
    lines = tail_lines(path, 1000)
    assert 10 <= len(lines) <= 11
    assert all(text.endswith(logs.TRUNCATED) for text in lines)
