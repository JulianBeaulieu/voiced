"""The ``tts`` service process: HTTP API plus Wyoming listener sharing one engine."""

from voiced.tts.app import build_engine, build_tts_app

__all__ = ["build_engine", "build_tts_app"]
