"""Fixtures for the HTTP API contract tests.

Everything runs against the fake engines through the ASGI app in-process:
no models, no sockets. Test audio is generated here with numpy and the
stdlib ``wave`` module so no fixture files are needed. Tests that need
ffmpeg are marked with :data:`requires_ffmpeg` and skip when it is absent.
"""

from __future__ import annotations

import asyncio
import io
import time
import wave
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import NoReturn

import httpx
import numpy as np
import openai
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import voiced.api.app as app_module
from voiced.api import ffmpeg
from voiced.engines.base import AudioInput, PCMChunk, Transcript
from voiced.testing import FakeSTTEngine, FakeTTSEngine

requires_ffmpeg = pytest.mark.skipif(
    not ffmpeg.ffmpeg_available(), reason="no ffmpeg (it comes with imageio-ffmpeg)"
)


def sine_wav(
    seconds: float = 0.5, sample_rate: int = 16_000, channels: int = 1, freq: float = 440.0
) -> bytes:
    """A 16-bit PCM WAV file of a sine tone, built in memory."""
    n = int(seconds * sample_rate)
    t = np.arange(n) / sample_rate
    tone = (0.3 * np.sin(2 * np.pi * freq * t) * 32767).astype("<i2")
    frames = (
        np.repeat(tone[:, None], channels, axis=1).tobytes() if channels > 1 else tone.tobytes()
    )
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(frames)
    return buf.getvalue()


def pcm16(seconds: float = 0.5, sample_rate: int = 16_000) -> bytes:
    """Raw int16 mono PCM of a sine tone, as sent on the WebSocket."""
    n = int(seconds * sample_rate)
    t = np.arange(n) / sample_rate
    return (0.3 * np.sin(2 * np.pi * 440.0 * t) * 32767).astype("<i2").tobytes()


def _openai_client(app: FastAPI, api_key: str = "test-key") -> openai.AsyncOpenAI:
    transport = httpx.ASGITransport(app=app)
    return openai.AsyncOpenAI(
        base_url="http://voiced/v1",
        api_key=api_key,
        http_client=httpx.AsyncClient(transport=transport, base_url="http://voiced"),
        max_retries=0,
    )


@pytest.fixture(autouse=True)
def _no_silent_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The app's lifespan calls ``os._exit`` if shut down while the engine is
    still loading, which would kill pytest silently. Make that a loud failure."""

    def fake_exit(code: int) -> NoReturn:
        raise RuntimeError(f"exit_process({code}) called: lifespan closed before the engine loaded")

    monkeypatch.setattr(app_module, "exit_process", fake_exit)


@asynccontextmanager
async def loaded(app: FastAPI) -> AsyncIterator[FastAPI]:
    """Run the lifespan and wait for the background engine load."""
    async with app.router.lifespan_context(app):
        await app.state.load_task
        yield app


def sync_client(app: FastAPI) -> TestClient:
    """A Starlette TestClient whose lifespan has loaded the engine (for WebSocket tests)."""
    client = TestClient(app)
    client.__enter__()
    deadline = time.monotonic() + 5.0
    while client.get("/health").status_code != 200:
        if time.monotonic() > deadline:
            client.__exit__(None, None, None)
            raise AssertionError("engine did not load")
        time.sleep(0.005)
    return client


@pytest.fixture
async def openai_stt(stt_app: FastAPI) -> AsyncIterator[openai.AsyncOpenAI]:
    """The official client pointed at the STT app, with the engine loaded."""
    async with loaded(stt_app):
        client = _openai_client(stt_app)
        try:
            yield client
        finally:
            await client.close()


@pytest.fixture
async def openai_tts(tts_app: FastAPI) -> AsyncIterator[openai.AsyncOpenAI]:
    async with loaded(tts_app):
        client = _openai_client(tts_app)
        try:
            yield client
        finally:
            await client.close()


class GatedTTSEngine(FakeTTSEngine):
    """Blocks inside ``synthesize`` until ``gate`` is set: deterministic queue tests."""

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()

    async def synthesize(
        self, text: str, voice: str, speed: float = 1.0
    ) -> AsyncIterator[PCMChunk]:
        await self.gate.wait()
        async for chunk in super().synthesize(text, voice, speed):
            yield chunk


class GatedSTTEngine(FakeSTTEngine):
    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()

    async def transcribe(self, audio: AudioInput, language: str | None = None) -> Transcript:
        await self.gate.wait()
        return await super().transcribe(audio, language)


@pytest.fixture
def fake_decode(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Replace ffmpeg decoding with one second of silence so tests run without ffmpeg."""
    seen: list[int] = []

    async def decode(  # type: ignore[type-arg]
        data: bytes, sample_rate: int = 16_000, *, max_seconds: float | None = None
    ) -> np.ndarray:
        seen.append(len(data))
        return np.ones(sample_rate, dtype=np.float32) * 0.1

    monkeypatch.setattr(ffmpeg, "decode", decode)
    return seen


async def wait_for_depth(app: FastAPI, depth: int) -> None:
    """Yield to the loop until the inference queue reaches ``depth``."""
    for _ in range(1000):
        if app.state.ctx.queue.depth >= depth:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"queue depth {app.state.ctx.queue.depth}, expected {depth}")
