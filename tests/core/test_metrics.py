from __future__ import annotations

import json
import sys

import pytest

from voiced.metrics import Metrics, Operation, Outcome, _percentile, phys_footprint


def test_percentile_interpolates() -> None:
    assert _percentile([], 0.5) is None
    assert _percentile([7.0], 0.95) == 7.0
    assert _percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5


def test_per_operation_latency_and_counts() -> None:
    m = Metrics()
    for ms in range(1, 101):
        m.record(Operation.TRANSCRIBE, Outcome.OK, float(ms))
    m.record(Operation.TRANSCRIBE, Outcome.BUSY, 0.0, "queue full")
    m.record(Operation.SPEECH, Outcome.OK, 5.0)
    snap = m.snapshot()
    stt = snap["operations"]["transcribe"]
    assert stt["latency"]["p50_ms"] == 50.5
    assert stt["latency"]["p95_ms"] == 95.0
    assert stt["latency"]["samples"] == 100
    assert stt["counts"] == {
        "ok": 100,
        "busy": 1,
        "client_error": 0,
        "disconnect": 0,
        "failure": 0,
    }
    assert snap["operations"]["speech"]["latency"]["p50_ms"] == 5.0
    assert snap["memory"]["rss_bytes"] > 0
    # MLX allocation is reported when MLX is installed, None otherwise (CI).
    assert "mlx" in snap["memory"]
    if sys.platform == "darwin":
        assert snap["memory"]["footprint_bytes"] > 0
    else:
        assert snap["memory"]["footprint_bytes"] is None
    json.dumps(snap)


def test_error_ring_is_bounded_newest_first_and_skips_disconnects() -> None:
    m = Metrics(window=3, error_window=2)
    m.record(Operation.SPEECH, Outcome.CLIENT_ERROR, 1.0, "first")
    m.record(Operation.SPEECH, Outcome.FAILURE, 1.0, "x" * 1000)
    m.record(Operation.SPEECH, Outcome.FAILURE, 1.0, "third")
    m.record(Operation.SPEECH, Outcome.DISCONNECT, 1.0, "gone")
    for ms in (1.0, 2.0, 3.0, 100.0):
        m.record(Operation.SPEECH, Outcome.OK, ms)
    snap = m.snapshot()
    errors = snap["recent_errors"]
    assert [e["detail"][:5] for e in errors] == ["third", "xxxxx"]
    assert len(errors[1]["detail"]) == 500
    assert errors[0]["outcome"] == "failure"
    assert snap["operations"]["speech"]["counts"]["disconnect"] == 1
    assert snap["operations"]["speech"]["latency"]["samples"] == 3


@pytest.mark.skipif(sys.platform != "darwin", reason="proc_pid_rusage is macOS only")
def test_phys_footprint_counts_memory_that_rss_does_not_see() -> None:
    """The footprint is read for this process and grows with a fresh allocation."""
    before = phys_footprint()
    assert before is not None and before > 0
    block = bytearray(64 * 1024 * 1024)
    block[::4096] = b"x" * len(block[::4096])  # touch every page
    after = phys_footprint()
    assert after is not None and after - before > 32 * 1024 * 1024
    del block
