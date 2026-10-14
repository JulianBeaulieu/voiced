"""Per-app state and request-level errors shared by every module in ``voiced.api``.

Route modules import from here, never from ``voiced.api.app``, so the app
factory can import the routes at module level without a cycle.

Error responses on ``/v1`` routes share one JSON shape, which is also what
the OpenAI clients parse into their exceptions::

    {"error": {"message": "...", "type": "..."}}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Literal

from fastapi import Depends, status
from starlette.requests import HTTPConnection

from voiced.config import Settings
from voiced.engines.base import STTEngine, TTSEngine
from voiced.health import HealthState
from voiced.metrics import Metrics
from voiced.queue import InferenceQueue


@dataclass
class ServiceContext:
    service: Literal["stt", "tts"]
    settings: Settings
    engine: STTEngine | TTSEngine
    queue: InferenceQueue
    health: HealthState
    metrics: Metrics = field(default_factory=Metrics)

    @property
    def stt_engine(self) -> STTEngine:
        assert isinstance(self.engine, STTEngine)
        return self.engine

    @property
    def tts_engine(self) -> TTSEngine:
        assert isinstance(self.engine, TTSEngine)
        return self.engine


def get_context(conn: HTTPConnection) -> ServiceContext:
    """Works for HTTP requests and WebSocket connections alike."""
    ctx: ServiceContext = conn.app.state.ctx
    return ctx


Ctx = Annotated[ServiceContext, Depends(get_context)]
"""Route parameter annotation: ``async def route(ctx: Ctx) -> ...``."""


class APIError(Exception):
    """A request-level error with an explicit HTTP status.

    Routes raise this for anything the client did wrong that is not an
    :class:`~voiced.engines.base.EngineError`: bad parameters (400), oversize
    input (413), missing credentials (401). ``type`` is a stable
    machine-readable tag. On a WebSocket handshake it closes the socket
    instead (code 1008 for 4xx, 1011 for 5xx).
    """

    def __init__(self, status: int, message: str, type: str = "invalid_request_error") -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.type = type


def require_api_key(conn: HTTPConnection, ctx: Ctx) -> None:
    """Bearer auth for ``/v1`` routes and WebSocket handshakes when ``server.api_key`` is set."""
    expected = ctx.settings.server.api_key
    if not expected:
        return
    scheme, _, token = conn.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or token != expected:
        raise APIError(status.HTTP_401_UNAUTHORIZED, "invalid or missing API key", "auth_error")


def require_loaded(ctx: Ctx) -> None:
    """503 ``starting`` until the engine has loaded, so routes never hit a cold engine."""
    if not ctx.health.model_loaded:
        raise APIError(status.HTTP_503_SERVICE_UNAVAILABLE, "model is still loading", "starting")
