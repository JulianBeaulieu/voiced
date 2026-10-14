"""OpenAI-compatible HTTP API, one FastAPI app per modality."""

from voiced.api.app import create_stt_app, create_tts_app

__all__ = ["create_stt_app", "create_tts_app"]
