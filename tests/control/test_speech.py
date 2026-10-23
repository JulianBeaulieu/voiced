"""``tts.speak`` against a real tts service behind uvicorn, and stubs."""

from __future__ import annotations

import asyncio
import io
import time
import wave
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
import uvicorn
from fastapi import FastAPI

from voiced.cli import uvicorn_config
from voiced.cli_lifecycle import watchdog_specs
from voiced.config import Settings
from voiced.control.speech import SPEECH_RATE, Speech, SpeechRefused, SpeechUnavailable
from voiced.engines.base import TTS_SAMPLE_RATE, TTSEngine
from voiced.testing import FakeTTSEngine
from voiced.testing.fakes import PIECE_GAP_S, STEPS_PER_PIECE, SteppedTTSEngine
from voiced.tts import build_tts_app
from voiced.watchdog import ServiceSpec


def test_the_rate_is_the_services() -> None:
    assert SPEECH_RATE == TTS_SAMPLE_RATE


async def _until(condition: Callable[[], bool], timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        await asyncio.sleep(0.01)


@asynccontextmanager
async def serving(
    settings: Settings, engine: TTSEngine
) -> AsyncIterator[tuple[ServiceSpec, FastAPI]]:
    """The tts service on a free port; yields its spec as the watchdog has
    it, and the app."""
    settings.server.bind = "127.0.0.1"
    settings.server.api_key = "secret"
    app = build_tts_app(settings, engine)
    server = uvicorn.Server(uvicorn_config(app, settings, port=0))
    task = asyncio.create_task(server.serve())
    try:
        await _until(lambda: server.started)
        await asyncio.wait_for(app.state.load_task, 5.0)
        port = server.servers[0].sockets[0].getsockname()[1]
        spec = next(s for s in watchdog_specs(settings) if s.name == "tts")
        yield replace(spec, base_url=f"http://127.0.0.1:{port}"), app
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5.0)


async def test_speaks_with_the_key_the_service_was_started_with(settings: Settings) -> None:
    engine = FakeTTSEngine()
    async with serving(settings, engine) as (spec, _):
        audio = await Speech(lambda: spec)("Hi, I'm Adam.", "am_adam")
        with wave.open(io.BytesIO(audio)) as file:
            assert (file.getnchannels(), file.getsampwidth(), file.getframerate()) == (1, 2, 24_000)
            assert file.getnframes() > 0  # the real length, not the streamed "unknown"
        assert engine.calls == 1

        with pytest.raises(SpeechRefused, match="does not serve the voice 'nobody'"):
            await Speech(lambda: spec)("Hi.", "nobody")
        with pytest.raises(SpeechRefused, match="API key"):
            await Speech(lambda: replace(spec, api_key="wrong"))("Hi.", "am_adam")
        # FakeTTSEngine: 100 ms per 10 characters, so this is 0.3 s of audio.
        with pytest.raises(SpeechRefused, match=r"longer than 0\.1 s"):
            await Speech(lambda: spec, max_seconds=0.1)("Thirty characters, near enough", "am_adam")
        assert engine.calls == 2
    with pytest.raises(SpeechUnavailable, match="not answering"):
        await Speech(lambda: spec)("Hi.", "am_adam")  # stopped, or switched off
    with pytest.raises(SpeechUnavailable, match="not supervised"):
        await Speech(lambda: None)("Hi.", "am_adam")


async def test_a_cancel_ends_the_synthesis(settings: Settings) -> None:
    """Stopping a preview closes the request, and the service stops at its
    next step instead of synthesizing the rest for no one."""
    engine = SteppedTTSEngine(steps_before_first=STEPS_PER_PIECE)
    async with serving(settings, engine) as (spec, app):
        task = asyncio.create_task(Speech(lambda: spec)("Hi, I'm Heart.", "af_heart"))
        await _until(lambda: engine.steps > 0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        hung_up = time.perf_counter()
        await _until(lambda: app.state.ctx.queue.depth == 0)
        assert time.perf_counter() - hung_up < PIECE_GAP_S / 4
        assert engine.steps < STEPS_PER_PIECE
        counts = app.state.ctx.metrics.snapshot()["operations"]["speech"]["counts"]
        assert counts["disconnect"] == 1 and counts["ok"] == 0


@asynccontextmanager
async def stub(answer: bytes) -> AsyncIterator[ServiceSpec]:
    """A server that sends ``answer`` to every request and closes."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(answer)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield ServiceSpec("tts", "test.tts", f"http://127.0.0.1:{port}")
    finally:
        server.close()
        await server.wait_closed()


async def test_a_server_error_is_unavailable_and_a_client_error_refused() -> None:
    busy = b'HTTP/1.0 503 Service Unavailable\r\n\r\n{"error": {"message": "queue full"}}'
    async with stub(busy) as spec:
        with pytest.raises(SpeechUnavailable, match="queue full"):
            await Speech(lambda: spec)("Hi.", "a")
    bad = b'HTTP/1.0 400 Bad Request\r\n\r\n{"error": {"message": "bad voice"}}'
    async with stub(bad) as spec:
        with pytest.raises(SpeechRefused, match="bad voice"):
            await Speech(lambda: spec)("Hi.", "a")


async def test_no_http_answer_is_unavailable() -> None:
    async with stub(b"") as spec:
        with pytest.raises(SpeechUnavailable, match="no HTTP answer"):
            await Speech(lambda: spec)("Hi.", "a")


async def test_one_deadline_for_the_whole_exchange() -> None:
    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(10)

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    spec = ServiceSpec("tts", "test.tts", f"http://127.0.0.1:{port}")
    try:
        with pytest.raises(SpeechUnavailable, match=r"did not answer within 0\.2s"):
            await Speech(lambda: spec, timeout_s=0.2)("Hi.", "a")
    finally:
        server.close()


async def test_a_chunked_answer_is_refused_rather_than_played() -> None:
    chunked = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4\r\nabcd\r\n0\r\n\r\n"
    async with stub(chunked) as spec:
        with pytest.raises(SpeechUnavailable, match="chunks"):
            await Speech(lambda: spec)("Hi.", "a")
