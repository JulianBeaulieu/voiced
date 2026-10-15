"""Health state reported by ``GET /health`` and polled by the watchdog."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel

HealthStatus = Literal["starting", "ok", "degraded"]


class HealthResponse(BaseModel):
    """Schema of ``GET /health``. Frozen for the watchdog and the status page."""

    status: HealthStatus
    service: Literal["stt", "tts"]
    engine: str
    model: str
    model_loaded: bool
    uptime_s: float
    queue_depth: int
    queue_capacity: int
    last_inference_at: float | None
    last_inference_ms: float | None
    last_error: str | None
    version: str


@dataclass
class HealthState:
    """Mutable per-process health record. One instance per service process.

    ``voiced.context.ServiceContext`` owns it, ``voiced.inference.inference``
    updates it after each inference, and ``/health`` serializes it. ``status`` is derived:

    * ``starting`` until the engine's ``load()`` has finished,
    * ``degraded`` if the most recent inference raised,
    * ``ok`` otherwise.
    """

    service: Literal["stt", "tts"]
    engine: str
    model: str
    version: str
    queue_capacity: int
    started_at: float = field(default_factory=time.monotonic)
    model_loaded: bool = False
    last_inference_at: float | None = None
    last_inference_ms: float | None = None
    last_error: str | None = None
    last_error_type: str | None = None
    """The exception class name of ``last_error``, kept apart so it can be
    shown without the message (``ServiceContext.public_health``)."""

    def mark_loaded(self) -> None:
        self.model_loaded = True
        self.last_error = self.last_error_type = None

    def record_success(self, duration_ms: float) -> None:
        self.last_inference_at = time.time()
        self.last_inference_ms = duration_ms
        self.last_error = self.last_error_type = None

    def record_error(self, error: BaseException) -> None:
        self.last_error_type = type(error).__name__
        self.last_error = f"{self.last_error_type}: {error}"

    @property
    def status(self) -> HealthStatus:
        if not self.model_loaded:
            return "starting"
        if self.last_error is not None:
            return "degraded"
        return "ok"

    def snapshot(self, queue_depth: int) -> HealthResponse:
        """Serialize for ``/health``. Read-only: ``queue_depth`` comes from the queue."""
        return HealthResponse(
            status=self.status,
            service=self.service,
            engine=self.engine,
            model=self.model,
            model_loaded=self.model_loaded,
            uptime_s=round(time.monotonic() - self.started_at, 3),
            queue_depth=queue_depth,
            queue_capacity=self.queue_capacity,
            last_inference_at=self.last_inference_at,
            last_inference_ms=self.last_inference_ms,
            last_error=self.last_error,
            version=self.version,
        )
