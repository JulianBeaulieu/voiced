"""``WS /v1/audio/transcriptions/stream`` with Starlette's TestClient (no sockets).

Close codes follow the ADR 0005 outcome table (``voiced.api.context.WS_CLOSE``).
"""

from __future__ import annotations

import json
import time
from typing import Any

import numpy as np
import pytest
from fastapi import FastAPI
from numpy.typing import NDArray
from starlette.testclient import WebSocketDisconnect
from starlette.websockets import WebSocket

from tests.api.conftest import pcm16, sync_client
from voiced import audio
from voiced.api.context import close_ws
from voiced.audio import pcm16_to_float32
from voiced.config import Settings
from voiced.engines.base import STTStreamSession
from voiced.stt import build_stt_app
from voiced.testing import FakeSTTEngine, FakeSTTStreamSession

PATH = "/v1/audio/transcriptions/stream"


def test_partial_then_final(stt_app: FastAPI, stt_engine: FakeSTTEngine) -> None:
    with sync_client(stt_app) as client, client.websocket_connect(PATH) as ws:
        ws.send_text(json.dumps({"type": "config", "sample_rate": 16000, "language": "en"}))
        ws.send_bytes(pcm16(0.3))
        assert ws.receive_json() == {"type": "partial", "text": "hello"}
        ws.send_bytes(pcm16(0.2))  # same hypothesis: no second partial
        ws.send_text(json.dumps({"type": "end"}))
        final = ws.receive_json()
        assert final["type"] == "final"
        assert final["text"] == "hello world"
        assert final["language"] == "en"
        assert final["duration"] == pytest.approx(0.5, abs=0.01)
        assert final["segments"] == [{"start": 0.0, "end": 0.5, "text": "hello world"}]
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1000
    assert stt_engine.calls == 1
    assert stt_app.state.ctx.queue.depth == 0
    stream = stt_app.state.ctx.metrics.snapshot()["operations"]["stream"]
    assert stream["counts"]["ok"] == 1 and stream["latency"]["samples"] == 1


def test_config_is_optional_and_other_rates_are_resampled(stt_app: FastAPI) -> None:
    with sync_client(stt_app) as client:
        with client.websocket_connect(PATH) as ws:
            ws.send_bytes(pcm16(0.5))
            ws.send_text(json.dumps({"type": "end"}))
            assert ws.receive_json()["type"] == "partial"
            assert ws.receive_json()["duration"] == pytest.approx(0.5, abs=0.01)
        with client.websocket_connect(PATH) as ws:
            ws.send_text(json.dumps({"type": "config", "sample_rate": 8000}))
            ws.send_bytes(pcm16(0.5, sample_rate=8000))
            ws.send_text(json.dumps({"type": "end"}))
            ws.receive_json()
            assert ws.receive_json()["duration"] == pytest.approx(0.5, abs=0.01)


def test_end_without_audio_gives_empty_final(stt_app: FastAPI) -> None:
    with sync_client(stt_app) as client, client.websocket_connect(PATH) as ws:
        ws.send_text(json.dumps({"type": "end"}))
        final = ws.receive_json()
        assert final == {
            "type": "final",
            "text": "",
            "language": None,
            "duration": 0.0,
            "segments": [],
        }


def test_bad_frames_are_client_errors_1008(stt_app: FastAPI) -> None:
    with sync_client(stt_app) as client:
        for first_frame in ("not json", json.dumps({"type": "config", "sample_rate": "fast"})):
            with client.websocket_connect(PATH) as ws:
                ws.send_text(first_frame)
                with pytest.raises(WebSocketDisconnect) as exc:
                    ws.receive_json()
                assert exc.value.code == 1008
                assert exc.value.reason.startswith("BadFrame: ")
        with client.websocket_connect(PATH) as ws:
            ws.send_text(json.dumps({"type": "config", "language": "xx"}))
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == 1008
            assert exc.value.reason.startswith("UnsupportedLanguage: ")
    assert stt_app.state.ctx.queue.depth == 0
    stream = stt_app.state.ctx.metrics.snapshot()["operations"]["stream"]
    assert stream["counts"]["client_error"] == 3


class _RecordingSession(FakeSTTStreamSession):
    def __init__(self, engine: FakeSTTEngine, language: str | None) -> None:
        super().__init__(engine, language)
        self.received: list[NDArray[np.float32]] = []

    async def add_audio(self, samples: NDArray[np.float32]) -> None:
        self.received.append(samples.copy())
        await super().add_audio(samples)


class _Recording(FakeSTTEngine):
    def __init__(self) -> None:
        super().__init__()
        self.sessions: list[_RecordingSession] = []

    async def stream(self, language: str | None = None) -> STTStreamSession:
        session = _RecordingSession(self, language)
        self.sessions.append(session)
        return session


def test_odd_sized_frames_carry_the_trailing_byte(settings: Settings) -> None:
    """Every sample reaches the engine intact, whatever the frame boundaries.

    Dropping the odd byte of a frame would lose a sample and swap the byte
    order of every later one, so the samples would no longer match.
    """
    audio = pcm16(0.5)  # 16000 bytes
    frames = [audio[:1001], audio[1001:1002], audio[1002:7777], audio[7777:]]
    assert [len(f) % 2 for f in frames] == [1, 1, 1, 1]
    engine = _Recording()
    app = build_stt_app(settings, engine)
    with sync_client(app) as client, client.websocket_connect(PATH) as ws:
        for frame in frames:
            ws.send_bytes(frame)
        ws.send_text(json.dumps({"type": "end"}))
        while ws.receive_json()["type"] != "final":
            pass
    (session,) = engine.sessions
    np.testing.assert_array_equal(np.concatenate(session.received), pcm16_to_float32(audio))


def test_idle_session_is_a_disconnect(stt_app: FastAPI) -> None:
    stt_app.state.ctx.settings.server.session_idle_s = 0.2
    with sync_client(stt_app) as client:
        with client.websocket_connect(PATH) as ws:
            ws.send_bytes(pcm16(0.1))
            ws.receive_json()
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()  # nothing sent for 0.2 s
            assert exc.value.code == 1001
        _wait_for_depth(stt_app, 0)
        snap = stt_app.state.ctx.metrics.snapshot()
        assert snap["operations"]["stream"]["counts"]["disconnect"] == 1
        assert snap["recent_errors"] == [], "disconnects are counted, not logged"
        assert client.get("/health").json()["status"] == "ok"


def test_api_key_enforced_on_websocket(stt_app: FastAPI) -> None:
    stt_app.state.ctx.settings.server.api_key = "s3cret"
    with sync_client(stt_app) as client:
        with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect(PATH):
            pass
        assert exc.value.code == 1008
        with client.websocket_connect(PATH, headers={"Authorization": "Bearer s3cret"}) as ws:
            ws.send_text(json.dumps({"type": "end"}))
            assert ws.receive_json()["type"] == "final"


def test_configured_languages_are_enforced(settings: Settings) -> None:
    settings.stt.languages = ["de"]
    app = build_stt_app(settings, FakeSTTEngine())
    with sync_client(app) as client, client.websocket_connect(PATH) as ws:
        ws.send_text(json.dumps({"type": "config", "language": "en"}))
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 1008 and "'en' is not served" in exc.value.reason


def test_queue_full_closes_with_1013(settings: Settings) -> None:
    app = build_stt_app(settings, FakeSTTEngine())  # queue_depth is 2 in the test settings
    with sync_client(app) as client:
        first = client.websocket_connect(PATH).__enter__()
        second = client.websocket_connect(PATH).__enter__()
        first.send_bytes(pcm16(0.1))
        first.receive_json()  # partial: the first session holds the slot
        _wait_for_depth(app, 2)
        with client.websocket_connect(PATH) as third:
            with pytest.raises(WebSocketDisconnect) as exc:
                third.receive_json()
            assert exc.value.code == 1013
        for ws in (first, second):
            ws.send_text(json.dumps({"type": "end"}))
            while ws.receive_json()["type"] != "final":
                pass
            ws.__exit__(None, None, None)
    _wait_for_depth(app, 0)


def test_client_disconnect_releases_slot(stt_app: FastAPI) -> None:
    with sync_client(stt_app) as client:
        with client.websocket_connect(PATH) as ws:
            ws.send_bytes(pcm16(0.2))
            ws.receive_json()
            _wait_for_depth(stt_app, 1)
        # Context exit sends a disconnect without "end"; the session closes and the slot frees.
        _wait_for_depth(stt_app, 0)
        with client.websocket_connect(PATH) as ws:
            ws.send_text(json.dumps({"type": "end"}))
            assert ws.receive_json()["type"] == "final"
    assert stt_app.state.ctx.metrics.snapshot()["operations"]["stream"]["counts"]["disconnect"] == 1


def test_engine_failure_closes_with_1011_and_degrades_health(settings: Settings) -> None:
    class BrokenSession(FakeSTTStreamSession):
        async def add_audio(self, samples) -> None:  # type: ignore[no-untyped-def]
            raise RuntimeError("metal wedged")

    class Broken(FakeSTTEngine):
        async def stream(self, language: str | None = None) -> STTStreamSession:
            return BrokenSession(self, language)

    app = build_stt_app(settings, Broken())
    with sync_client(app) as client:
        with client.websocket_connect(PATH) as ws:
            ws.send_bytes(pcm16(0.1))
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == 1011
            assert exc.value.reason == "RuntimeError: metal wedged"
        _wait_for_depth(app, 0)
        health = client.get("/health").json()
        assert health["status"] == "degraded"
        assert "metal wedged" in health["last_error"]


def _wait_for_depth(app: FastAPI, depth: int, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while app.state.ctx.queue.depth != depth:
        if time.monotonic() > deadline:
            raise AssertionError(f"queue depth {app.state.ctx.queue.depth}, expected {depth}")
        time.sleep(0.01)


async def test_close_ws_tolerates_a_dropped_connection() -> None:
    """After a TCP drop the server's send fails; Starlette turns that into
    WebSocketDisconnect(1006). Closing must swallow it, or uvicorn logs a traceback."""
    sent: list[str] = []

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.connect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message["type"])
        if message["type"] == "websocket.close":
            raise OSError("connection lost")

    scope = {"type": "websocket", "path": PATH, "headers": [], "query_string": b""}
    ws = WebSocket(scope, receive, send)
    await ws.accept()
    await close_ws(ws, 1011, "x" * 200)  # must not raise
    assert sent == ["websocket.accept", "websocket.close"]


async def test_close_ws_truncates_the_reason_to_the_protocol_limit() -> None:
    closes: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "websocket.connect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "websocket.close":
            closes.append(message)

    scope = {"type": "websocket", "path": PATH, "headers": [], "query_string": b""}
    ws = WebSocket(scope, receive, send)
    await ws.accept()
    await close_ws(ws, 1008, "é" * 100)  # 200 bytes of UTF-8
    (close,) = closes
    assert close["code"] == 1008
    assert len(close["reason"].encode()) <= 123 and close["reason"] == "é" * 61


def test_stream_audio_is_bounded(stt_app: FastAPI) -> None:
    """Like a Wyoming utterance, a session carries at most ``max_upload_mb`` of
    16-bit 16 kHz audio; more is a client error (1008)."""
    stt_app.state.ctx.settings.stt.max_upload_mb = 1  # about 31 s
    with sync_client(stt_app) as client:
        with client.websocket_connect(PATH) as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                for _ in range(8):  # 40 s
                    ws.send_bytes(pcm16(5.0))
                while True:
                    ws.receive_json()
            assert exc.value.code == 1008
            assert "s of audio" in exc.value.reason
        _wait_for_depth(stt_app, 0)
        assert client.get("/health").json()["status"] == "ok"


def test_stream_session_has_a_wall_clock_bound(
    stt_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client that sends a little audio just inside ``session_idle_s`` each
    time could otherwise hold the slot forever."""
    monkeypatch.setattr(audio, "utterance_limit_s", lambda settings: 0.3)
    stt_app.state.ctx.settings.server.session_idle_s = 1.0  # open for at most 1.3 s
    with sync_client(stt_app) as client:
        with client.websocket_connect(PATH) as ws:
            for _ in range(40):  # 2 s of one sample every 50 ms, never idle for 1 s
                ws.send_bytes(b"\0\0")
                time.sleep(0.05)
            with pytest.raises(WebSocketDisconnect) as exc:
                while True:
                    ws.receive_json()
            assert exc.value.code == 1008
            assert "session open longer than" in exc.value.reason
        _wait_for_depth(stt_app, 0)
