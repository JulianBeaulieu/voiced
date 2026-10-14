"""FastAPI app factories.

Each service process builds exactly one app with :func:`create_stt_app` or
:func:`create_tts_app`. The app owns a :class:`ServiceContext` (engine, queue,
health state, settings) available to routes through ``Ctx``. The engine loads
in a background task so the socket binds first; ``/health`` and ``/v1``
answer 503 until it is warm (ADR 0005).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from typing import NoReturn

from fastapi import Depends, FastAPI, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocket

from voiced import __version__
from voiced.api import routes_stt, routes_tts
from voiced.api.context import APIError, Ctx, require_api_key, require_loaded
from voiced.config import Settings
from voiced.context import ServiceContext
from voiced.engines.base import EngineError, EngineNotLoaded, STTEngine, TTSEngine
from voiced.health import HealthResponse, HealthState
from voiced.inference import classify
from voiced.metrics import Outcome
from voiced.queue import InferenceQueue, QueueFull

# ServiceContext stays importable from here until voiced.wyoming.server
# (owned by another PR) imports it from voiced.context.
__all__ = ["ServiceContext", "create_stt_app", "create_tts_app"]

log = logging.getLogger(__name__)

V1_DEPENDENCIES = [Depends(require_api_key), Depends(require_loaded)]

# Outcome -> (HTTP status, error type). Wyoming keeps the same shape of table.
_HTTP: dict[Outcome, tuple[int, str]] = {
    Outcome.BUSY: (status.HTTP_503_SERVICE_UNAVAILABLE, "server_busy"),
    Outcome.CLIENT_ERROR: (status.HTTP_400_BAD_REQUEST, "invalid_request_error"),
    Outcome.FAILURE: (status.HTTP_500_INTERNAL_SERVER_ERROR, "server_error"),
}

exit_process: Callable[[int], NoReturn] = os._exit
"""Ends the process without waiting for threads. Replaced in tests."""


async def _load(ctx: ServiceContext) -> None:
    """Load the engine in the background; the process exits 1 if that fails.

    Serving ``starting`` forever would hide the failure from launchd, which
    only restarts a process that exits.
    """
    started = time.perf_counter()
    log.info("loading %s engine %s (%s)", ctx.service, ctx.engine.name, ctx.engine.model_id)
    try:
        await ctx.engine.load()
    except Exception:
        log.exception("%s engine failed to load; exiting", ctx.service)
        logging.shutdown()
        exit_process(1)
    ctx.health.mark_loaded()
    log.info("%s ready in %.1fs", ctx.service, time.perf_counter() - started)


async def _error(
    conn: HTTPConnection,
    code: int,
    message: str,
    type: str,
    headers: dict[str, str] | None = None,
) -> Response | None:
    """The one error envelope for HTTP; a close frame for a WebSocket handshake."""
    if isinstance(conn, WebSocket):
        if code == status.HTTP_503_SERVICE_UNAVAILABLE:
            close_code = status.WS_1013_TRY_AGAIN_LATER
        elif code >= 500:
            close_code = status.WS_1011_INTERNAL_ERROR
        else:
            close_code = status.WS_1008_POLICY_VIOLATION
        # Close reasons are capped at 123 bytes by the protocol.
        reason = message.encode()[:123].decode(errors="ignore")
        with suppress(RuntimeError):  # already closed
            await conn.close(close_code, reason)
        return None
    return JSONResponse(
        status_code=code, content={"error": {"message": message, "type": type}}, headers=headers
    )


def _build(ctx: ServiceContext) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(_load(ctx), name=f"voiced-{ctx.service}-load")
        app.state.load_task = task
        yield
        if not task.done():
            # The load thread cannot be interrupted, and waiting it out could
            # exceed launchd's ExitTimeOut (about 20 s). Leave now.
            log.warning("%s: shutdown during engine load; exiting", ctx.service)
            logging.shutdown()
            exit_process(0)

    app = FastAPI(
        title=f"voiced {ctx.service}",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.ctx = ctx

    # Exceptions map to statuses here, never in routes. Nothing here writes
    # metrics: inference() already recorded anything raised inside a slot.
    @app.exception_handler(APIError)
    async def _api_error(conn: HTTPConnection, exc: APIError) -> Response | None:
        return await _error(conn, exc.status, exc.message, exc.type)

    @app.exception_handler(QueueFull)
    @app.exception_handler(EngineError)
    async def _classified(conn: HTTPConnection, exc: Exception) -> Response | None:
        code, kind = _HTTP[classify(exc)]
        if isinstance(exc, EngineNotLoaded):
            kind = "starting"
        headers = {"Retry-After": "1"} if code == status.HTTP_503_SERVICE_UNAVAILABLE else None
        return await _error(conn, code, f"{type(exc).__name__}: {exc}", kind, headers)

    @app.exception_handler(RequestValidationError)
    async def _invalid(conn: HTTPConnection, exc: RequestValidationError) -> Response | None:
        # 400 rather than FastAPI's 422, as OpenAI does, so the openai client
        # raises BadRequestError.
        message = "; ".join(
            f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('msg', '')}"
            for e in exc.errors()
        )
        return await _error(conn, 400, message, "invalid_request_error")

    @app.exception_handler(Exception)
    async def _unexpected(conn: HTTPConnection, exc: Exception) -> Response | None:
        if classify(exc) is Outcome.FAILURE:
            ctx.health.record_error(exc)
            log.error("unhandled error on %s", conn.url.path, exc_info=exc)
        code, kind = _HTTP[Outcome.FAILURE]
        return await _error(conn, code, f"internal error ({type(exc).__name__})", kind)

    @app.get("/health", response_model=HealthResponse, tags=["service"])
    async def health(ctx: Ctx) -> JSONResponse:
        """Service health. 503 while the model is loading, 200 otherwise.

        ``status`` is ``degraded`` if the last inference failed; the watchdog
        decides what to do with that.
        """
        snapshot = ctx.health.snapshot(ctx.queue.depth)
        code = status.HTTP_200_OK if snapshot.model_loaded else status.HTTP_503_SERVICE_UNAVAILABLE
        return JSONResponse(status_code=code, content=snapshot.model_dump())

    @app.get("/v1/models", tags=["discovery"], dependencies=V1_DEPENDENCIES)
    async def models(ctx: Ctx) -> dict[str, object]:
        engine = ctx.engine
        return {
            "object": "list",
            "data": [
                {
                    "id": engine.model_id,
                    "object": "model",
                    "owned_by": "voiced",
                    "engine": engine.name,
                    "task": "transcription" if ctx.service == "stt" else "speech",
                    "languages": ctx.languages,
                    "license": engine.license,
                }
            ],
        }

    @app.get("/", include_in_schema=False)
    async def status_page() -> JSONResponse:
        return _not_implemented("status page")

    return app


def _not_implemented(what: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        content={"error": {"message": f"{what} is not implemented yet", "type": "not_implemented"}},
    )


def create_stt_app(settings: Settings, engine: STTEngine) -> FastAPI:
    ctx = ServiceContext(
        service="stt",
        settings=settings,
        engine=engine,
        queue=InferenceQueue(settings.stt.queue_depth),
        health=HealthState(
            service="stt",
            engine=engine.name,
            model=engine.model_id,
            version=__version__,
            queue_capacity=settings.stt.queue_depth,
        ),
    )
    app = _build(ctx)
    app.include_router(routes_stt.router, dependencies=V1_DEPENDENCIES)
    return app


def create_tts_app(settings: Settings, engine: TTSEngine) -> FastAPI:
    ctx = ServiceContext(
        service="tts",
        settings=settings,
        engine=engine,
        queue=InferenceQueue(settings.tts.queue_depth),
        health=HealthState(
            service="tts",
            engine=engine.name,
            model=engine.model_id,
            version=__version__,
            queue_capacity=settings.tts.queue_depth,
        ),
    )
    app = _build(ctx)
    app.include_router(routes_tts.router, dependencies=V1_DEPENDENCIES)
    return app
