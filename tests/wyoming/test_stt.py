from __future__ import annotations

import asyncio
import contextlib

import numpy as np
import pytest
from fastapi import FastAPI
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.error import Error
from wyoming.event import Event
from wyoming.info import Describe, Info

from voiced import __version__
from voiced.testing import FakeSTTEngine
from voiced.wyoming import WYOMING_STT_SERVICE, stt_handler
from voiced.wyoming.server import WyomingListener
from voiced.wyoming.stt_handler import STTEventHandler

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
    error = Error.from_event(event)
    assert error.code == "invalid_request_error"
    assert error.text.startswith("UnsupportedLanguage")
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


async def _after_error(c: AsyncTcpClient) -> list[Event]:
    """Carry on as a misbehaving client would after an error (more audio, then
    AudioStop) and return whatever still arrives. The server has closed the
    connection, so writes or reads may hit a reset; that counts as nothing."""
    events: list[Event] = []
    with contextlib.suppress(ConnectionError):
        for _ in range(3):
            await c.write_event(chunk16k(pcm16(0.5)).event())
        await c.write_event(AudioStop().event())
        events = await read_until(c)
    return events


def chunk16k(audio: bytes) -> AudioChunk:
    return AudioChunk(rate=16_000, width=2, channels=1, audio=audio)


async def test_utterance_over_cap_closes_and_never_restarts(
    stt_app: FastAPI, stt_listener: WyomingListener
) -> None:
    engine: FakeSTTEngine = stt_app.state.ctx.engine
    ctx = stt_listener.ctx
    ctx.settings.stt.max_upload_mb = 1  # 500k int16 samples, about 31 s at 16 kHz
    async with client(stt_listener) as c:
        await c.write_event(AudioStart(rate=16_000, width=2, channels=1).event())
        await c.write_event(chunk16k(pcm16(40.0)).event())  # over the cap in one chunk
        event = await c.read_event()
        assert event is not None and Error.is_type(event.type)
        assert Error.from_event(event).text.startswith("EngineError")
        # The connection is closed: no second session, no transcript of the
        # leftover audio, no fresh size budget.
        assert await _after_error(c) == []
    assert engine.calls == 1
    await wait_for(lambda: ctx.queue.depth == 0)
    assert ctx.health.status == "ok"


async def test_format_change_mid_stream_closes_without_transcript(
    stt_app: FastAPI, stt_listener: WyomingListener
) -> None:
    engine: FakeSTTEngine = stt_app.state.ctx.engine
    async with client(stt_listener) as c:
        await c.write_event(AudioStart(rate=16_000, width=2, channels=1).event())
        await c.write_event(chunk16k(pcm16(0.1)).event())
        await c.write_event(
            AudioChunk(rate=48_000, width=2, channels=1, audio=pcm16(0.1, 48_000)).event()
        )
        event = await c.read_event()
        assert event is not None and Error.is_type(event.type)
        assert Error.from_event(event).text.startswith("UnsupportedAudioFormat")
        assert await _after_error(c) == []
    assert engine.calls == 1
    await wait_for(lambda: stt_listener.ctx.queue.depth == 0)


@pytest.mark.parametrize("width", [3, 8])
async def test_unsupported_width_is_rejected(stt_listener: WyomingListener, width: int) -> None:
    async with client(stt_listener) as c:
        await c.write_event(AudioStart(rate=16_000, width=width, channels=1).event())
        event = await c.read_event()
        assert event is not None and Error.is_type(event.type)
        assert Error.from_event(event).text.startswith("UnsupportedAudioFormat")
        assert await _after_error(c) == []
    await wait_for(lambda: stt_listener.ctx.queue.depth == 0)
    assert stt_listener.ctx.health.status == "ok"


async def test_client_outrunning_a_busy_slot_is_pushed_back(
    stt_app: FastAPI, stt_listener: WyomingListener
) -> None:
    """While the slot is busy the session cannot drain its queue; the handler
    must not buffer without limit. It stops reading at the queue bound and,
    after session_idle_s without progress, drops the client."""
    engine: FakeSTTEngine = stt_app.state.ctx.engine
    ctx = stt_listener.ctx
    ctx.settings.server.session_idle_s = 0.3
    gate = asyncio.Event()

    async def hold() -> None:
        async with ctx.queue.slot():
            await gate.wait()

    holder = asyncio.create_task(hold())
    await wait_for(lambda: ctx.queue.depth == 1)
    try:
        async with client(stt_listener) as c:
            await c.write_event(AudioStart(rate=16_000, width=2, channels=1).event())
            with contextlib.suppress(ConnectionError):
                for _ in range(200):  # far more than the queue holds
                    await c.write_event(chunk16k(pcm16(0.064)).event())
            # Closed with nothing sent; the close may arrive as a reset because
            # the server leaves the client's unread audio behind.
            events: list[Event] = []
            with contextlib.suppress(ConnectionError):
                events = await read_until(c)
            assert events == []
        assert engine.calls == 0  # the session never got the slot
        await wait_for(lambda: ctx.queue.depth == 1)  # only the holder is left
    finally:
        gate.set()
        await holder
    assert ctx.queue.depth == 0
    assert ctx.health.status == "ok"


async def test_session_wall_clock_is_bounded(
    stt_listener: WyomingListener, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tiny chunks that each arrive inside session_idle_s still cannot hold
    the slot past max_utterance_s + session_idle_s."""
    monkeypatch.setattr(stt_handler, "max_utterance_s", lambda settings: 0.2)
    ctx = stt_listener.ctx
    ctx.settings.server.session_idle_s = 0.3
    async with client(stt_listener) as c:
        await c.write_event(AudioStart(rate=16_000, width=2, channels=1).event())

        async def trickle() -> None:
            with contextlib.suppress(ConnectionError):
                while True:
                    await c.write_event(chunk16k(b"\x00\x00").event())
                    await asyncio.sleep(0.1)

        trickler = asyncio.create_task(trickle())
        try:
            events = await read_until(c)
        finally:
            trickler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await trickler
    assert [e.type for e in events] == ["error"]
    assert "utterance longer than" in Error.from_event(events[0]).text
    await wait_for(lambda: ctx.queue.depth == 0)


async def test_audio_before_the_model_loads_is_busy(stt_app: FastAPI) -> None:
    ctx = stt_app.state.ctx  # engine deliberately not loaded
    ctx.settings.server.zeroconf = False
    listener = WyomingListener(
        ctx, "127.0.0.1", 0, handler_cls=STTEventHandler, service_name=WYOMING_STT_SERVICE
    )
    async with listener, client(listener) as c:
        await c.write_event(AudioStart(rate=16_000, width=2, channels=1).event())
        events = await read_until(c)
    assert [e.type for e in events] == ["error"]
    assert Error.from_event(events[0]).code == "server_busy"
    assert ctx.queue.depth == 0
