"""FastAPI app factories.

Each service process builds exactly one app with :func:`create_stt_app` or
:func:`create_tts_app`. The app owns a :class:`ServiceContext` (engine, queue,
health state, settings) available to routes through :func:`get_context`.
Startup loads the engine inside the lifespan, so ``/health`` reports
``starting`` until the model is warm.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from voiced import __version__
from voiced.api import routes_stt, routes_tts
from voiced.config import Settings
from voiced.engines.base import EngineError, STTEngine, TTSEngine
from voiced.health import HealthResponse, HealthState
from voiced.queue import InferenceQueue, QueueFull

log = logging.getLogger(__name__)


@dataclass
class ServiceContext:
    service: Literal["stt", "tts"]
    settings: Settings
    engine: STTEngine | TTSEngine
    queue: InferenceQueue
    health: HealthState

    @property
    def stt_engine(self) -> STTEngine:
        assert isinstance(self.engine, STTEngine)
        return self.engine

    @property
    def tts_engine(self) -> TTSEngine:
        assert isinstance(self.engine, TTSEngine)
        return self.engine


def get_context(request: Request) -> ServiceContext:
    ctx: ServiceContext = request.app.state.ctx
    return ctx


Ctx = Annotated[ServiceContext, Depends(get_context)]
"""Route parameter annotation: ``async def route(ctx: Ctx) -> ...``."""


async def require_api_key(request: Request, ctx: Ctx) -> None:
    """Bearer auth for ``/v1`` routes when ``server.api_key`` is set."""
    expected = ctx.settings.server.api_key
    if not expected:
        return
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or token != expected:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing API key")


def _build(ctx: ServiceContext) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        started = time.perf_counter()
        log.info("loading %s engine %s (%s)", ctx.service, ctx.engine.name, ctx.engine.model_id)
        await ctx.engine.load()
        ctx.health.mark_loaded()
        log.info("%s ready in %.1fs", ctx.service, time.perf_counter() - started)
        yield

    app = FastAPI(
        title=f"voiced {ctx.service}",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.ctx = ctx

    @app.exception_handler(QueueFull)
    async def _queue_full(request: Request, exc: QueueFull) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"error": {"message": str(exc), "type": "server_busy"}},
            headers={"Retry-After": "1"},
        )

    @app.exception_handler(EngineError)
    async def _engine_error(request: Request, exc: EngineError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"error": {"message": str(exc), "type": type(exc).__name__}},
        )

    @app.get("/health", response_model=HealthResponse, tags=["service"])
    async def health(ctx: Ctx) -> JSONResponse:
        """Service health. 503 while the model is loading, 200 otherwise.

        ``status`` is ``degraded`` if the last inference failed; the watchdog
        decides what to do with that.
        """
        ctx.health.queue_depth = ctx.queue.depth
        snapshot = ctx.health.snapshot()
        code = status.HTTP_200_OK if snapshot.model_loaded else status.HTTP_503_SERVICE_UNAVAILABLE
        return JSONResponse(status_code=code, content=snapshot.model_dump())

    @app.get("/v1/models", tags=["discovery"], dependencies=[Depends(require_api_key)])
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
    app.include_router(routes_stt.router, dependencies=[Depends(require_api_key)])
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
    app.include_router(routes_tts.router, dependencies=[Depends(require_api_key)])
    return app
