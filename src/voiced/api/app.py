"""FastAPI app factories.

Each service process builds exactly one app with :func:`create_stt_app` or
:func:`create_tts_app`. The app owns a :class:`ServiceContext` (engine, queue,
health state, settings) available to routes through :func:`get_context`.
Startup loads the engine inside the lifespan, so ``/health`` reports
``starting`` until the model is warm.
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
from voiced.api.context import (
    APIError,
    Ctx,
    ServiceContext,
    get_context,
    require_api_key,
    require_loaded,
)
from voiced.config import Settings
from voiced.engines.base import EngineError, STTEngine, TTSEngine
from voiced.health import HealthResponse, HealthState
from voiced.inference import record_exception
from voiced.metrics import Outcome
from voiced.queue import InferenceQueue, QueueFull

__all__ = ["APIError", "Ctx", "ServiceContext", "create_stt_app", "create_tts_app", "get_context"]

log = logging.getLogger(__name__)

V1_DEPENDENCIES = [Depends(require_api_key), Depends(require_loaded)]

exit_process: Callable[[int], NoReturn] = os._exit
"""Called with 1 when the engine fails to load. Replaced in tests."""


async def _load(ctx: ServiceContext) -> None:
    """Load the engine in the background; the process dies if that fails.

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
    """The one error envelope for HTTP; a close frame for WebSockets."""
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
        # Load in the background so the socket binds first: /health answers
        # 503 "starting" during a long model load instead of refusing
        # connections, which the watchdog could not tell apart from a crash.
        task = asyncio.create_task(_load(ctx), name=f"voiced-{ctx.service}-load")
        app.state.load_task = task
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(
        title=f"voiced {ctx.service}",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.ctx = ctx

    # Every exception class maps to a status here, never in the routes. The
    # outcome is recorded through voiced.inference so that an exception that
    # already passed through an inference() block is not counted twice.
    @app.exception_handler(APIError)
    async def _api_error(conn: HTTPConnection, exc: APIError) -> Response | None:
        return await _error(conn, exc.status, exc.message, exc.type)

    @app.exception_handler(QueueFull)
    async def _queue_full(conn: HTTPConnection, exc: QueueFull) -> Response | None:
        record_exception(ctx, conn.url.path, exc)
        return await _error(conn, 503, str(exc), "server_busy", {"Retry-After": "1"})

    @app.exception_handler(EngineError)
    async def _engine_error(conn: HTTPConnection, exc: EngineError) -> Response | None:
        record_exception(ctx, conn.url.path, exc)
        return await _error(conn, 400, str(exc), type(exc).__name__)

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
        if record_exception(ctx, conn.url.path, exc) is Outcome.FAILURE:
            log.error("unhandled error on %s", conn.url.path, exc_info=exc)
        return await _error(conn, 500, f"internal error ({type(exc).__name__})", "server_error")

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
                    "languages": engine.languages,
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
