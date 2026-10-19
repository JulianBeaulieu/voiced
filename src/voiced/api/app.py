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
from contextlib import asynccontextmanager
from typing import NoReturn

from fastapi import Depends, FastAPI, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocket

from voiced import __version__
from voiced.api import routes_stt, routes_tts, status_page
from voiced.api.context import (
    WS_CLOSE,
    APIError,
    Ctx,
    close_ws,
    require_api_key,
    require_loaded,
)
from voiced.api.foreign_models import foreign_model_map
from voiced.config import Settings
from voiced.context import ServiceContext
from voiced.engines import registry
from voiced.engines.base import EngineError, STTEngine, TTSEngine
from voiced.health import HealthResponse, HealthState
from voiced.inference import classify
from voiced.metrics import Outcome
from voiced.queue import InferenceQueue, QueueFull

__all__ = ["create_stt_app", "create_tts_app"]

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

STARTUP_FAILED_EXIT = 3
"""Exit code when startup fails while the engine loads (uvicorn's own code for
a failed startup)."""


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
    if not ctx.languages:
        log.warning(
            "%s serves no language: engine %s offers %s but %s.languages is %s",
            ctx.service,
            ctx.engine.name,
            ctx.engine.languages,
            ctx.service,
            ctx._configured,
        )
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
            close_code = WS_CLOSE[Outcome.BUSY]
        elif code >= 500:
            close_code = WS_CLOSE[Outcome.FAILURE]
        else:
            close_code = WS_CLOSE[Outcome.CLIENT_ERROR]
        await close_ws(conn, close_code, message)
        return None
    return JSONResponse(
        status_code=code, content={"error": {"message": message, "type": type}}, headers=headers
    )


def _build(ctx: ServiceContext) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(_load(ctx), name=f"voiced-{ctx.service}-load")
        app.state.load_task = task
        try:
            yield
        except Exception as exc:
            # Startup failed after the load began: a Wyoming port clash in the
            # listener's outer lifespan, for one. The load thread cannot be
            # interrupted, so returning would keep a process that can never
            # serve alive until the model has loaded and warmed. Leave now.
            if not task.done():
                log.error("%s: startup failed during engine load: %s", ctx.service, exc)
                logging.shutdown()
                exit_process(STARTUP_FAILED_EXIT)
            raise
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
    # metrics or health: inference() already recorded anything raised inside
    # a slot.
    @app.exception_handler(APIError)
    async def _api_error(conn: HTTPConnection, exc: APIError) -> Response | None:
        return await _error(conn, exc.status, exc.message, exc.type)

    @app.exception_handler(QueueFull)
    @app.exception_handler(EngineError)
    async def _classified(conn: HTTPConnection, exc: Exception) -> Response | None:
        code, kind = _HTTP[classify(exc)]
        headers = {"Retry-After": "1"} if code == status.HTTP_503_SERVICE_UNAVAILABLE else None
        return await _error(conn, code, f"{type(exc).__name__}: {exc}", kind, headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http(conn: HTTPConnection, exc: StarletteHTTPException) -> Response | None:
        # Starlette's own 4xx/5xx (malformed multipart, 404, 405) in the shared
        # envelope instead of FastAPI's {"detail": ...}.
        kind = "invalid_request_error" if exc.status_code < 500 else "server_error"
        headers = dict(exc.headers) if exc.headers else None
        return await _error(conn, exc.status_code, str(exc.detail), kind, headers)

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
        # inference() already marked health degraded if this came from an
        # engine; a bug outside a slot is not an engine failure. uvicorn logs
        # the traceback after this response, so log one line here.
        if classify(exc) is Outcome.DISCONNECT:
            # A client hang-up is not an error, so it is not logged as one here.
            # This only quiets our own line: ServerErrorMiddleware re-raises after
            # this handler and the server logs its traceback anyway, so routes
            # stop disconnects themselves (ClosingStreamingResponse, the upload
            # route). The middleware still needs a response object if none
            # started; the server drops it for a closed connection.
            log.debug("client went away on %s: %r", conn.url.path, exc)
        else:
            log.error("unhandled %s on %s: %s", type(exc).__name__, conn.url.path, exc)
        code, kind = _HTTP[Outcome.FAILURE]
        return await _error(conn, code, f"internal error ({type(exc).__name__})", kind)

    @app.get("/health", response_model=HealthResponse, tags=["service"])
    async def health(ctx: Ctx) -> JSONResponse:
        """Service health. 503 while the model is loading, 200 otherwise.

        ``status`` is ``degraded`` if the last inference failed; the watchdog
        decides what to do with that.
        """
        snapshot = ctx.public_health()
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

    # GET / (the status page) and GET /metrics.json. Unauthenticated like /health.
    app.include_router(status_page.router)

    return app


def create_stt_app(settings: Settings, engine: STTEngine) -> FastAPI:
    ctx = ServiceContext(
        service="stt",
        settings=settings,
        engine=engine,
        queue=InferenceQueue(
            settings.stt.queue_depth, shared=min(settings.stt.streams, settings.stt.queue_depth)
        ),
        health=HealthState(
            service="stt",
            engine=engine.name,
            model=engine.model_id,
            version=__version__,
            queue_capacity=settings.stt.queue_depth,
        ),
    )
    app = _build(ctx)
    app.state.foreign_models = foreign_model_map(registry.list_stt_engines(), engine.name)
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
    app.state.foreign_models = foreign_model_map(registry.list_tts_engines(), engine.name)
    app.include_router(routes_tts.router, dependencies=V1_DEPENDENCIES)
    return app
