"""Health state reported by ``GET /health`` and polled by the watchdog."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel

from voiced.models import ModelNotDownloaded

HealthStatus = Literal["starting", "ok", "degraded", "missing_model", "idle", "loading"]

READY_STATUSES: frozenset[HealthStatus] = frozenset({"ok", "degraded", "idle", "loading"})
"""Statuses in which the service takes requests (``/health`` answers 200).
``idle`` and ``loading`` too: the request waits for the model to load."""


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

    * ``missing_model`` if the load found the model not downloaded: the
      process stays up and serves nothing until it is downloaded and the
      service restarted (ADR 0022),
    * ``loading`` while the model loads again for a request after an idle
      unload (ADR 0033),
    * ``starting`` until the engine's first ``load()`` has finished,
    * ``degraded`` if the most recent inference (or a load for one) raised,
    * ``idle`` while the model is unloaded on purpose; the next request
      loads it,
    * ``ok`` otherwise.

    ``model_loaded`` says whether the model is in memory now: false while
    ``idle`` and ``loading``.
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
    missing: ModelNotDownloaded | None = None
    """What the load found missing, if it did."""
    unloaded: bool = False
    """The model was unloaded on purpose (idle); set until it is loaded again."""
    reloading: bool = False
    """A request is waiting for the model to load again after an unload."""

    def mark_loaded(self) -> None:
        self.model_loaded = True
        self.unloaded = self.reloading = False
        self.last_error = self.last_error_type = None

    def mark_unloaded(self) -> None:
        self.model_loaded = False
        self.unloaded = True

    @property
    def started(self) -> bool:
        """The first load has finished: requests are taken from then on."""
        return self.model_loaded or self.unloaded

    def mark_missing(self, missing: ModelNotDownloaded) -> None:
        self.missing = missing
        self.record_error(missing)

    def record_success(self, duration_ms: float) -> None:
        self.last_inference_at = time.time()
        self.last_inference_ms = duration_ms
        self.last_error = self.last_error_type = None

    def record_error(self, error: BaseException) -> None:
        self.last_error_type = type(error).__name__
        self.last_error = f"{self.last_error_type}: {error}"

    @property
    def status(self) -> HealthStatus:
        if self.missing is not None:
            return "missing_model"
        if self.reloading:
            return "loading"
        if not self.started:
            return "starting"
        if self.last_error is not None:
            return "degraded"
        if self.unloaded:
            return "idle"
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
