from __future__ import annotations

from fastapi import FastAPI

from voiced.api.app import create_stt_app
from voiced.config import Settings
from voiced.engines.base import STTEngine
from voiced.engines.registry import get_stt_engine
from voiced.wyoming import WYOMING_STT_SERVICE
from voiced.wyoming.server import attach
from voiced.wyoming.stt_handler import STTEventHandler


def build_engine(settings: Settings) -> STTEngine:
    """Instantiate the configured STT engine (not loaded yet)."""
    cls = get_stt_engine(settings.stt.engine)
    return cls(settings.stt.model)


def build_stt_app(settings: Settings, engine: STTEngine | None = None) -> FastAPI:
    """The FastAPI app for the stt process, with the Wyoming listener attached
    to its lifespan so both run in one event loop and share the engine."""
    app = create_stt_app(settings, engine or build_engine(settings))
    attach(
        app,
        app.state.ctx,
        settings.server.bind,
        settings.stt.wyoming_port,
        handler_cls=STTEventHandler,
        service_name=WYOMING_STT_SERVICE,
    )
    return app
