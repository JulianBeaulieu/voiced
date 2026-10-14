from __future__ import annotations

from fastapi import FastAPI

from voiced.api.app import create_tts_app
from voiced.config import Settings
from voiced.engines.base import TTSEngine
from voiced.engines.registry import get_tts_engine


def build_engine(settings: Settings) -> TTSEngine:
    """Instantiate the configured TTS engine (not loaded yet)."""
    cls = get_tts_engine(settings.tts.engine)
    return cls(settings.tts.model)


def build_tts_app(settings: Settings, engine: TTSEngine | None = None) -> FastAPI:
    """The FastAPI app for the tts process. The Wyoming listener is started by
    ``voiced.wyoming`` alongside it in the same event loop (Phase 1)."""
    return create_tts_app(settings, engine or build_engine(settings))
