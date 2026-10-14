from __future__ import annotations

from fastapi import FastAPI

from voiced.api.app import create_tts_app
from voiced.config import Settings
from voiced.engines.base import TTSEngine
from voiced.engines.registry import get_tts_engine
from voiced.wyoming import WYOMING_TTS_SERVICE
from voiced.wyoming.server import attach
from voiced.wyoming.tts_handler import TTSEventHandler


def build_engine(settings: Settings) -> TTSEngine:
    """Instantiate the configured TTS engine (not loaded yet)."""
    cls = get_tts_engine(settings.tts.engine)
    return cls(settings.tts.model)


def build_tts_app(settings: Settings, engine: TTSEngine | None = None) -> FastAPI:
    """The FastAPI app for the tts process, with the Wyoming listener attached
    to its lifespan so both run in one event loop and share the engine."""
    app = create_tts_app(settings, engine or build_engine(settings))
    attach(
        app,
        app.state.ctx,
        settings.server.bind,
        settings.tts.wyoming_port,
        handler_cls=TTSEventHandler,
        service_name=WYOMING_TTS_SERVICE,
    )
    return app
