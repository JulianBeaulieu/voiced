from __future__ import annotations

from fastapi import FastAPI

from voiced.api.app import create_stt_app
from voiced.config import Settings
from voiced.engines.base import STTEngine
from voiced.engines.registry import get_stt_engine


def build_engine(settings: Settings) -> STTEngine:
    """Instantiate the configured STT engine (not loaded yet)."""
    cls = get_stt_engine(settings.stt.engine)
    return cls(settings.stt.model)


def build_stt_app(settings: Settings, engine: STTEngine | None = None) -> FastAPI:
    """The FastAPI app for the stt process. The Wyoming listener is started by
    ``voiced.wyoming`` alongside it in the same event loop (Phase 1)."""
    return create_stt_app(settings, engine or build_engine(settings))
