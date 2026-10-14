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
from voiced.context import ServiceContext
from voiced.engines.base import STTStreamSession
from voiced.testing import FakeSTTEngine
from voiced.wyoming import WYOMING_STT_SERVICE, stt_handler
from voiced.wyoming.server import WyomingListener
from voiced.wyoming.stt_handler import STTEventHandler

from .conftest import client, read_until, stub_writer, wait_for


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


async def _after_error(c: AsyncTcpClient, audio: bytes | None = None) -> list[Event]:
    """Carry on as a misbehaving client would after an error (more audio, then
    AudioStop) and return whatever still arrives. The server has closed the
    connection, so writes or reads may hit a reset; that counts as nothing.
    (Writing to a transport asyncio has already closed raises TypeError from
    ``writelines`` on CPython 3.12, hence the second suppressed type.)"""
    events: list[Event] = []
    with contextlib.suppress(ConnectionError, TypeError):
        for _ in range(3):
            await c.write_event(chunk16k(pcm16(0.5) if audio is None else audio).event())
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


async def _hold_slot(ctx: ServiceContext, gate: asyncio.Event) -> asyncio.Task[None]:
    async def hold() -> None:
        async with ctx.queue.slot():
            await gate.wait()

    task = asyncio.create_task(hold())
    await wait_for(lambda: ctx.queue.depth == 1)
    return task


async def test_client_stuck_behind_a_busy_slot_gets_server_busy(
    stt_app: FastAPI, stt_listener: WyomingListener, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session cannot drain its queue while another request holds the
    slot. Once the queue is full and session_idle_s passes, the server is the
    slow side: the client is told server_busy instead of being dropped."""
    monkeypatch.setattr(stt_handler, "_QUEUE_CHUNKS", 2)
    engine: FakeSTTEngine = stt_app.state.ctx.engine
    ctx = stt_listener.ctx
    ctx.settings.server.session_idle_s = 0.3
    gate = asyncio.Event()
    holder = await _hold_slot(ctx, gate)
    try:
        async with client(stt_listener) as c:
            await c.write_event(AudioStart(rate=16_000, width=2, channels=1).event())
            for _ in range(3):  # two fill the queue, the third waits for room
                await c.write_event(chunk16k(pcm16(0.064)).event())
            events = await read_until(c)
        assert [e.type for e in events] == ["error"]
        assert Error.from_event(events[0]).code == "server_busy"
        assert engine.calls == 0  # the session never got the slot
        await wait_for(lambda: ctx.queue.depth == 1)  # only the holder is left
    finally:
        gate.set()
        await holder
    assert ctx.health.status == "ok"


async def test_oversized_chunks_are_refused_while_the_slot_is_busy(
    stt_app: FastAPI, stt_listener: WyomingListener
) -> None:
    """The queue bound is in chunks, so each chunk is capped in bytes: queued
    audio stays under _QUEUE_CHUNKS * MAX_CHUNK_BYTES per connection."""
    engine: FakeSTTEngine = stt_app.state.ctx.engine
    ctx = stt_listener.ctx
    gate = asyncio.Event()
    holder = await _hold_slot(ctx, gate)
    try:
        async with client(stt_listener) as c:
            await c.write_event(AudioStart(rate=16_000, width=2, channels=1).event())
            await c.write_event(chunk16k(bytes(stt_handler.MAX_CHUNK_BYTES + 2)).event())
            event = await c.read_event()
            assert event is not None and Error.is_type(event.type)
            error = Error.from_event(event)
            assert error.code == "invalid_request_error"
            assert error.text.startswith("EngineError: audio chunk over")
            # More oversized audio gets nothing back: the connection is closed.
            assert await _after_error(c, bytes(stt_handler.MAX_CHUNK_BYTES + 2)) == []
        assert engine.calls == 0
        await wait_for(lambda: ctx.queue.depth == 1)
    finally:
        gate.set()
        await holder
    assert ctx.health.status == "ok"


@pytest.mark.parametrize(
    "event",
    [
        AudioStart(rate=16_000, width=2, channels=1).event(),
        AudioChunk(rate=16_000, width=2, channels=1, audio=bytes(320)).event(),
        AudioStop().event(),
    ],
    ids=["audio-start", "audio-chunk", "audio-stop"],
)
async def test_audio_after_a_failed_session_never_reaches_a_session(
    stt_app: FastAPI, event: Event
) -> None:
    """The session task has failed (and called stop()), but events already
    buffered still reach handle(); none may restart or feed a session."""
    ctx = stt_app.state.ctx
    await ctx.engine.load()
    ctx.health.mark_loaded()
    engine: FakeSTTEngine = ctx.engine
    handler = STTEventHandler(ctx, asyncio.StreamReader(), stub_writer())

    async def failed() -> bool:
        return False

    ended = asyncio.create_task(failed())
    await ended
    handler._task = ended
    handler._queue = asyncio.Queue()
    assert await handler.handle(event) is False
    assert handler._task is ended  # no new session was started
    assert handler._queue.empty()  # nothing was fed to the dead one
    assert engine.calls == 0


async def test_rejected_language_hint_clears_the_previous_one(
    stt_app: FastAPI, stt_listener: WyomingListener
) -> None:
    engine: FakeSTTEngine = stt_app.state.ctx.engine
    seen: list[str | None] = []
    original = engine.stream

    async def spy(language: str | None = None) -> STTStreamSession:
        seen.append(language)
        return await original(language)

    engine.stream = spy  # type: ignore[method-assign]
    async with client(stt_listener) as c:
        await c.write_event(Transcribe(language="en").event())
        await c.write_event(Transcribe(language="xx").event())
        event = await c.read_event()
        assert event is not None and Error.is_type(event.type)
        await c.write_event(AudioStart(rate=16_000, width=2, channels=1).event())
        await c.write_event(chunk16k(pcm16(0.1)).event())
        await c.write_event(AudioStop().event())
        event = await c.read_event()
    assert event is not None and Transcript.is_type(event.type)
    assert seen == [None]  # not the earlier "en"


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


async def test_cancelling_a_connection_during_finalize_closes_its_socket(
    stt_app: FastAPI, stt_listener: WyomingListener
) -> None:
    """Cancellation escapes the handler's cleanup, so wyoming's run() never
    reaches writer.close(); the listener must close it before wait_closed()
    or the connection task never finishes."""
    engine: FakeSTTEngine = stt_app.state.ctx.engine
    async with client(stt_listener) as c:
        await c.write_event(AudioStart(rate=16_000, width=2, channels=1).event())
        await c.write_event(chunk16k(pcm16(0.1)).event())
        await wait_for(lambda: bool(stt_listener._handlers))
        engine.latency_s = 5.0  # finalize sleeps this long
        await c.write_event(AudioStop().event())
        await asyncio.sleep(0.1)  # now inside finalize
        (connection,) = list(stt_listener._handlers)
        connection.cancel()
        await asyncio.wait_for(asyncio.gather(connection, return_exceptions=True), 2.0)
        assert await read_until(c) == []  # the socket was closed
    await wait_for(lambda: stt_listener.ctx.queue.depth == 0)
