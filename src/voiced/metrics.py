"""Rolling request metrics for the status page and ``/metrics.json``.

One :class:`Metrics` instance per service process, owned by the service
context and fed by :func:`voiced.inference.inference`. Transport-agnostic: it
counts :class:`Outcome` values per operation (``transcribe``, ``speech``,
``stream`` ...), never HTTP status codes or Wyoming error codes.

Everything is in memory and bounded: each operation keeps its last ``window``
successful latencies, and the error log keeps the last ``error_window``
non-``ok`` records. Only the event loop thread touches it, so there is no
locking.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import psutil

LATENCY_WINDOW = 1000
ERROR_WINDOW = 50
DETAIL_LIMIT = 500


class Outcome(StrEnum):
    """How one inference ended. See ``voiced.inference.classify``."""

    OK = "ok"
    BUSY = "busy"  # queue full; nothing ran
    CLIENT_ERROR = "client_error"  # EngineError: the request was bad
    DISCONNECT = "disconnect"  # the client went away mid-request
    FAILURE = "failure"  # anything else: the engine or the server broke


@dataclass(frozen=True, slots=True)
class ErrorRecord:
    at: float
    operation: str
    outcome: Outcome
    detail: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": datetime.fromtimestamp(self.at, tz=UTC).isoformat(timespec="seconds"),
            "operation": self.operation,
            "outcome": self.outcome.value,
            "detail": self.detail,
        }


def percentile(sorted_values: list[float], q: float) -> float | None:
    """Linear-interpolated percentile of an already sorted list; ``None`` if empty."""
    if not sorted_values:
        return None
    pos = (len(sorted_values) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


class _Operation:
    def __init__(self, window: int) -> None:
        self.counts = dict.fromkeys(Outcome, 0)
        self.latencies: deque[float] = deque(maxlen=window)


class Metrics:
    def __init__(self, window: int = LATENCY_WINDOW, error_window: int = ERROR_WINDOW) -> None:
        self.window = window
        self._ops: dict[str, _Operation] = {}
        self._errors: deque[ErrorRecord] = deque(maxlen=error_window)

    def record(
        self, operation: str, outcome: Outcome, duration_ms: float, detail: str | None = None
    ) -> None:
        """Count one finished request.

        Only ``ok`` durations feed the percentiles. ``busy``, ``client_error``
        and ``failure`` also go into the error log; ``disconnect`` is only
        counted, because a client hanging up is not something to investigate.
        """
        op = self._ops.get(operation)
        if op is None:
            op = self._ops[operation] = _Operation(self.window)
        op.counts[outcome] += 1
        if outcome is Outcome.OK:
            op.latencies.append(float(duration_ms))
        elif outcome is not Outcome.DISCONNECT:
            if detail is not None:
                detail = detail[:DETAIL_LIMIT]
            self._errors.append(ErrorRecord(time.time(), operation, outcome, detail))

    def snapshot(self) -> dict[str, Any]:
        """Plain, JSON-serializable dict. Errors are newest first."""
        operations: dict[str, Any] = {}
        for name, op in self._ops.items():
            values = sorted(op.latencies)
            operations[name] = {
                "counts": {k.value: v for k, v in op.counts.items()},
                "latency": {
                    "p50_ms": _round(percentile(values, 0.50)),
                    "p95_ms": _round(percentile(values, 0.95)),
                    "samples": len(values),
                    "window": self.window,
                },
            }
        return {
            "operations": operations,
            "recent_errors": [e.as_dict() for e in reversed(self._errors)],
            "memory": {"rss_bytes": psutil.Process().memory_info().rss},
        }


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 1)
