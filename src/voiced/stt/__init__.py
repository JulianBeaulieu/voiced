"""The ``stt`` service process: HTTP API plus Wyoming listener sharing one engine."""

from voiced.stt.app import build_engine, build_stt_app

__all__ = ["build_engine", "build_stt_app"]
