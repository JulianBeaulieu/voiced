from __future__ import annotations

import asyncio

import numpy as np
import pytest
from fastapi import FastAPI
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.error import Error
from wyoming.info import Describe, Info

from voiced import __version__
from voiced.testing import FakeSTTEngine
from voiced.wyoming.server import WyomingListener

from .conftest import client, read_until, wait_for


def pcm16(seconds: float, rate: int = 16_000, channels: int = 1) -> bytes:
    t = np.arange(int(seconds * rate)) / rate
    mono = (np.sin(2 * np.pi * 440 * t) * 10_000).astype("<i2")
    return np.repeat(mono, channels).tobytes()


async def test_describe_returns_asr_program(stt_listener: WyomingListener) -> None:
    async with client(stt_listener) as c:
        await c.write_event(Describe().event())
        event = await c.read_event()
    assert event is not None and Info.is_type(event.type)
    info = Info.from_event(event)
    assert info.tts == [] and info.satellite is None
    (program,) = info.asr
    assert program.name == "voiced"
    assert program.installed is True
    assert program.version == __version__
    assert program.attribution.name == "voiced"
    assert program.requires_external_vad is True
    (model,) = program.models
    assert model.name == "fake/stt"
    assert model.installed is True
    assert model.languages == ["en"]


async def test_languages_intersect_engine_and_config(
    stt_app: FastAPI, stt_listener: WyomingListener
) -> None:
    stt_app.state.ctx.settings.stt.languages = ["de"]
    async with client(stt_listener) as c:
        await c.write_event(Describe().event())
        event = await c.read_event()
    assert event is not None
    assert Info.from_event(event).asr[0].models[0].languages == []


async def test_transcribe_round_trip(stt_listener: WyomingListener) -> None:
    ctx = stt_listener.ctx
    async with client(stt_listener) as c:
        await c.write_event(Transcribe(language="en").event())
        await c.write_event(AudioStart(rate=16_000, width=2, channels=1).event())
        audio = pcm16(1.0)
        for i in range(0, len(audio), 3200):
            await c.write_event(
                AudioChunk(rate=16_000, width=2, channels=1, audio=audio[i : i + 3200]).event()
            )
        await c.write_event(AudioStop().event())
        event = await c.read_event()
    assert event is not None and Transcript.is_type(event.type)
    transcript = Transcript.from_event(event)
    assert transcript.text == "hello world"
    assert transcript.language == "en"
    assert ctx.queue.depth == 0
    assert ctx.health.last_inference_at is not None
    assert ctx.health.status == "ok"


async def test_accepts_other_rates_and_stereo(stt_listener: WyomingListener) -> None:
    async with client(stt_listener) as c:
        await c.write_event(AudioStart(rate=48_000, width=2, channels=2).event())
        await c.write_event(
            AudioChunk(rate=48_000, width=2, channels=2, audio=pcm16(0.5, 48_000, 2)).event()
        )
        await c.write_event(AudioStop().event())
        event = await c.read_event()
    assert event is not None and Transcript.is_type(event.type)
    assert Transcript.from_event(event).text == "hello world"


async def test_regional_language_maps_to_advertised(stt_listener: WyomingListener) -> None:
    async with client(stt_listener) as c:
        await c.write_event(Transcribe(language="en-US").event())
        await c.write_event(AudioStart(rate=16_000, width=2, channels=1).event())
        await c.write_event(AudioChunk(rate=16_000, width=2, channels=1, audio=pcm16(0.1)).event())
        await c.write_event(AudioStop().event())
        event = await c.read_event()
    assert event is not None and Transcript.is_type(event.type)


async def test_unsupported_language_is_an_error(stt_listener: WyomingListener) -> None:
    async with client(stt_listener) as c:
        await c.write_event(Transcribe(language="xx").event())
        event = await c.read_event()
    assert event is not None and Error.is_type(event.type)
    assert Error.from_event(event).code == "UnsupportedLanguage"
    assert stt_listener.ctx.health.status == "ok"


async def test_stop_without_audio_gives_empty_transcript(stt_listener: WyomingListener) -> None:
    async with client(stt_listener) as c:
        await c.write_event(AudioStop().event())
        event = await c.read_event()
    assert event is not None and Transcript.from_event(event).text == ""


async def test_queue_full_sends_error_and_closes(stt_listener: WyomingListener) -> None:
    ctx = stt_listener.ctx
    gate = asyncio.Event()

    async def hold() -> None:
        async with ctx.queue.slot():
            await gate.wait()

    holders = [asyncio.create_task(hold()) for _ in range(ctx.queue.capacity)]
    await wait_for(lambda: ctx.queue.depth == ctx.queue.capacity)
    try:
        async with client(stt_listener) as c:
            await c.write_event(AudioStart(rate=16_000, width=2, channels=1).event())
            events = await read_until(c, "error")
            assert events and Error.is_type(events[-1].type)
            assert Error.from_event(events[-1]).code == "server_busy"
            assert await c.read_event() is None  # server closed the connection
    finally:
        gate.set()
        await asyncio.gather(*holders)
    assert ctx.queue.depth == 0
    assert ctx.health.status == "ok"  # the client's problem, not the engine's


async def test_disconnect_mid_stream_releases_slot_and_stays_healthy(
    stt_app: FastAPI, stt_listener: WyomingListener
) -> None:
    engine: FakeSTTEngine = stt_app.state.ctx.engine
    ctx = stt_listener.ctx
    async with client(stt_listener) as c:
        await c.write_event(AudioStart(rate=16_000, width=2, channels=1).event())
        await c.write_event(AudioChunk(rate=16_000, width=2, channels=1, audio=pcm16(0.2)).event())
        await wait_for(lambda: ctx.queue.depth == 1)
    # Dropping the client releases the slot and is not an engine failure.
    await wait_for(lambda: ctx.queue.depth == 0)
    assert engine.calls == 1  # the session was opened once and never finalized
    assert ctx.health.status == "ok"
    assert ctx.health.last_error is None


async def test_idle_session_times_out_and_releases_slot(stt_listener: WyomingListener) -> None:
    ctx = stt_listener.ctx
    ctx.settings.server.session_idle_s = 0.2
    async with client(stt_listener) as c:
        await c.write_event(AudioStart(rate=16_000, width=2, channels=1).event())
        await c.write_event(AudioChunk(rate=16_000, width=2, channels=1, audio=pcm16(0.1)).event())
        await wait_for(lambda: ctx.queue.depth == 1)
        # The satellite goes quiet while holding the slot: the server closes it.
        assert await c.read_event() is None
    await wait_for(lambda: ctx.queue.depth == 0)
    assert ctx.health.status == "ok"
    assert ctx.health.last_error is None


async def test_engine_failure_marks_degraded(
    stt_app: FastAPI, stt_listener: WyomingListener
) -> None:
    engine: FakeSTTEngine = stt_app.state.ctx.engine

    async def boom(language: str | None = None) -> None:
        raise RuntimeError("metal wedged")

    engine.stream = boom  # type: ignore[method-assign]
    async with client(stt_listener) as c:
        await c.write_event(AudioStart(rate=16_000, width=2, channels=1).event())
        event = await c.read_event()
        assert event is not None and Error.from_event(event).code == "engine_error"
        assert await c.read_event() is None
    assert stt_listener.ctx.health.status == "degraded"
    assert stt_listener.ctx.queue.depth == 0


async def test_utterance_over_cap_is_rejected(
    stt_app: FastAPI, stt_listener: WyomingListener
) -> None:
    ctx = stt_listener.ctx
    ctx.settings.stt.max_upload_mb = 1  # ~0.5M int16 samples, ~32s at 16 kHz
    async with client(stt_listener) as c:
        await c.write_event(AudioStart(rate=16_000, width=2, channels=1).event())
        # Two seconds over the cap in one chunk.
        await c.write_event(AudioChunk(rate=16_000, width=2, channels=1, audio=pcm16(40.0)).event())
        event = await c.read_event()
    assert event is not None and Error.is_type(event.type)
    await wait_for(lambda: ctx.queue.depth == 0)
    assert ctx.health.status == "ok"


@pytest.mark.parametrize("width", [3, 8])
async def test_unsupported_width_is_rejected(stt_listener: WyomingListener, width: int) -> None:
    async with client(stt_listener) as c:
        await c.write_event(AudioStart(rate=16_000, width=width, channels=1).event())
        await c.write_event(
            AudioChunk(rate=16_000, width=width, channels=1, audio=bytes(width * 10)).event()
        )
        event = await c.read_event()
    assert event is not None and Error.is_type(event.type)
    assert stt_listener.ctx.queue.depth == 0
    assert stt_listener.ctx.health.status == "ok"
