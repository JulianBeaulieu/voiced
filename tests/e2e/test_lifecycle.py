"""Service lifecycle with real engines: the warm-up 503 window and graceful
shutdown. The port-clash behaviour is covered through the real CLI in
``test_serve_cli.py`` (no model needed)."""

from __future__ import annotations

import threading
import time

import httpx
import pytest

from tests.e2e.conftest import (
    LONG_INPUT,
    MIN_AUDIO_BYTES,
    LiveService,
    _settings_for,
    start_service,
)
from voiced.cli import GRACEFUL_SHUTDOWN_S

pytestmark = pytest.mark.model


def test_health_is_503_until_warm_then_200(e2e_tmp) -> None:
    settings = _settings_for("stt", e2e_tmp / "warmup", **{"engine": '"parakeet"'})
    service = start_service("stt", settings, wait=False)  # return before the model warms
    try:
        saw_503 = False
        saw_200 = False
        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            response = httpx.get(f"{service.base_url}/health", timeout=5)
            if response.status_code == 503:
                saw_503 = True
                assert response.json()["model_loaded"] is False
                assert response.json()["status"] == "starting"
            elif response.status_code == 200:
                saw_200 = True
                assert response.json()["model_loaded"] is True
                break
            time.sleep(0.01)
        assert saw_503, "never observed the loading (503) state"
        assert saw_200, "service never became ready"
    finally:
        service.stop()


def test_graceful_shutdown_finishes_in_flight_work(e2e_tmp) -> None:
    settings = _settings_for("tts", e2e_tmp / "graceful", **{"engine": '"kokoro"'})
    service: LiveService = start_service("tts", settings)
    result: dict[str, object] = {}
    streaming = threading.Event()  # set once the response body has started

    def synthesize() -> None:
        try:
            with httpx.stream(
                "POST",
                f"{service.v1_url}/audio/speech",
                json={
                    "model": "voiced",
                    "input": LONG_INPUT,
                    "voice": "af_heart",
                    "response_format": "pcm",
                },
                timeout=GRACEFUL_SHUTDOWN_S + 5,
            ) as response:
                result["status"] = response.status_code
                received = 0
                for chunk in response.iter_bytes():
                    received += len(chunk)
                    streaming.set()  # the request is genuinely in flight
                result["bytes"] = received
        except Exception as exc:
            result["error"] = repr(exc)

    synth_thread = threading.Thread(target=synthesize)
    try:
        synth_thread.start()
        assert streaming.wait(timeout=10), "synthesis never started streaming"
        started = time.monotonic()
        service.server.should_exit = True  # SIGTERM-equivalent graceful shutdown
        service.thread.join(timeout=GRACEFUL_SHUTDOWN_S + 5)
        shutdown_s = time.monotonic() - started
        synth_thread.join(timeout=10)

        assert not service.thread.is_alive(), "server did not stop"
        assert shutdown_s < GRACEFUL_SHUTDOWN_S, f"shutdown took {shutdown_s:.1f}s"
        assert result.get("status") == 200, result
        assert isinstance(result.get("bytes"), int) and result["bytes"] > MIN_AUDIO_BYTES, result
    finally:
        service.stop()
