from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import cast

import pytest
from fastapi import FastAPI
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.error import Error
from wyoming.info import Describe, Info
from wyoming.tts import (
    Synthesize,
    SynthesizeChunk,
    SynthesizeStart,
    SynthesizeStop,
    SynthesizeStopped,
    SynthesizeVoice,
)

from voiced import __version__
from voiced.engines.base import PCMChunk
from voiced.inference import ClientTimeout
from voiced.testing import FakeTTSEngine
from voiced.testing.fakes import FAKE_VOICES
from voiced.wyoming.server import WyomingListener
from voiced.wyoming.tts_handler import TTSEventHandler

from .conftest import client, read_until, wait_for


async def test_describe_returns_tts_program(tts_listener: WyomingListener) -> None:
    async with client(tts_listener) as c:
        await c.write_event(Describe().event())
        event = await c.read_event()
    assert event is not None and Info.is_type(event.type)
    info = Info.from_event(event)
    assert info.asr == []
    (program,) = info.tts
    assert program.name == "voiced"
    assert program.installed is True
    assert program.version == __version__
    assert program.supports_synthesize_streaming is True
    assert [v.name for v in program.voices] == sorted(v.id for v in FAKE_VOICES)
    by_name = {v.name: v for v in program.voices}
    assert by_name["af_heart"].languages == ["en-US"]
    assert by_name["bf_emma"].languages == ["en-GB"]
    assert by_name["af_heart"].description == "Heart (female)"
    assert all(v.installed for v in program.voices)


async def test_voices_filtered_by_configured_languages(
    tts_app: FastAPI, tts_listener: WyomingListener
) -> None:
    tts_app.state.ctx.settings.tts.languages = ["fr"]
    async with client(tts_listener) as c:
        await c.write_event(Describe().event())
        event = await c.read_event()
    assert event is not None
    assert Info.from_event(event).tts[0].voices == []


async def test_plain_synthesize(tts_app: FastAPI, tts_listener: WyomingListener) -> None:
    engine: FakeTTSEngine = tts_app.state.ctx.engine
    async with client(tts_listener) as c:
        await c.write_event(
            Synthesize(
                text="Hello there. How are you?", voice=SynthesizeVoice(name="am_adam")
            ).event()
        )
        events = await read_until(c, "audio-stop")
    assert [e.type for e in events] == ["audio-start", "audio-chunk", "audio-chunk", "audio-stop"]
    start = AudioStart.from_event(events[0])
    assert (start.rate, start.width, start.channels) == (24_000, 2, 1)
    chunks = [AudioChunk.from_event(e) for e in events[1:3]]
    assert all((ch.rate, ch.width, ch.channels) == (24_000, 2, 1) for ch in chunks)
    assert sum(len(ch.audio) for ch in chunks) > 0
    assert engine.calls == 1
    assert tts_listener.ctx.health.last_inference_at is not None
    assert tts_listener.ctx.queue.depth == 0


async def test_default_and_named_voice_selection(
    tts_app: FastAPI, tts_listener: WyomingListener
) -> None:
    engine: FakeTTSEngine = tts_app.state.ctx.engine
    seen: list[str] = []
    original = engine.synthesize

    def spy(text: str, voice: str, speed: float = 1.0) -> AsyncIterator[PCMChunk]:
        seen.append(voice)
        return original(text, voice, speed)

    engine.synthesize = spy  # type: ignore[method-assign]
    async with client(tts_listener) as c:
        await c.write_event(Synthesize(text="Hi.").event())
        await read_until(c, "audio-stop")
        # Home Assistant only ever sends a voice name; a bare request uses the
        # configured default.
        await c.write_event(Synthesize(text="Hi.", voice=SynthesizeVoice(name="bf_emma")).event())
        await read_until(c, "audio-stop")
    assert seen == ["af_heart", "bf_emma"]


async def test_empty_text_yields_start_and_stop(tts_listener: WyomingListener) -> None:
    async with client(tts_listener) as c:
        await c.write_event(Synthesize(text="   ").event())
        events = await read_until(c, "audio-stop")
    assert [e.type for e in events] == ["audio-start", "audio-stop"]


async def test_text_over_max_input_chars_is_an_error(
    tts_app: FastAPI, tts_listener: WyomingListener
) -> None:
    ctx = tts_listener.ctx
    ctx.settings.tts.max_input_chars = 8
    async with client(tts_listener) as c:
        await c.write_event(Synthesize(text="this is well over the limit").event())
        event = await c.read_event()
        assert event is not None and Error.is_type(event.type)
        # Connection stays usable after a client fault.
        await c.write_event(Describe().event())
        event = await c.read_event()
        assert event is not None and Info.is_type(event.type)
    assert ctx.health.status == "ok"


async def test_streaming_text_over_max_input_chars_is_an_error(
    tts_app: FastAPI, tts_listener: WyomingListener
) -> None:
    ctx = tts_listener.ctx
    ctx.settings.tts.max_input_chars = 10
    async with client(tts_listener) as c:
        await c.write_event(SynthesizeStart().event())
        await c.write_event(SynthesizeChunk(text="one two ").event())
        await c.write_event(SynthesizeChunk(text="three four five").event())  # total over 10
        event = await read_until(c, "error")
    assert event and Error.from_event(event[-1]).code == "EngineError"
    assert ctx.health.status == "ok"


async def test_unknown_voice_is_an_error(tts_listener: WyomingListener) -> None:
    async with client(tts_listener) as c:
        await c.write_event(Synthesize(text="Hi.", voice=SynthesizeVoice(name="nope")).event())
        event = await c.read_event()
        assert event is not None and Error.from_event(event).code == "UnknownVoice"
        # Connection stays usable.
        await c.write_event(Describe().event())
        event = await c.read_event()
        assert event is not None and Info.is_type(event.type)
    assert tts_listener.ctx.health.status == "ok"


async def test_streaming_emits_audio_before_stop(
    tts_app: FastAPI, tts_listener: WyomingListener
) -> None:
    engine: FakeTTSEngine = tts_app.state.ctx.engine
    async with client(tts_listener) as c:
        await c.write_event(SynthesizeStart(voice=SynthesizeVoice(name="af_heart")).event())
        await c.write_event(SynthesizeChunk(text="The first sentence is done. The sec").event())
        # Audio for the first sentence arrives while the text is still open.
        early = await read_until(c, "audio-chunk")
        assert [e.type for e in early] == ["audio-start", "audio-chunk"]
        assert engine.calls == 1

        await c.write_event(SynthesizeChunk(text="ond one too").event())
        # Home Assistant repeats the whole message for non-streaming servers.
        await c.write_event(
            Synthesize(text="The first sentence is done. The second one too").event()
        )
        await c.write_event(SynthesizeStop().event())
        rest = await read_until(c, "synthesize-stopped")
    assert [e.type for e in rest] == ["audio-chunk", "audio-stop", "synthesize-stopped"]
    assert engine.calls == 2  # the compatibility Synthesize was ignored
    assert tts_listener.ctx.queue.depth == 0


async def test_streaming_with_no_text(tts_listener: WyomingListener) -> None:
    async with client(tts_listener) as c:
        await c.write_event(SynthesizeStart().event())
        await c.write_event(SynthesizeStop().event())
        events = await read_until(c, "synthesize-stopped")
    assert [e.type for e in events] == ["audio-start", "audio-stop", "synthesize-stopped"]


async def test_streaming_queue_full_is_an_error(tts_listener: WyomingListener) -> None:
    ctx = tts_listener.ctx
    gate = asyncio.Event()

    async def hold() -> None:
        async with ctx.queue.slot():
            await gate.wait()

    holders = [asyncio.create_task(hold()) for _ in range(ctx.queue.capacity)]
    await wait_for(lambda: ctx.queue.depth == ctx.queue.capacity)
    try:
        async with client(tts_listener) as c:
            await c.write_event(SynthesizeStart().event())
            await c.write_event(SynthesizeChunk(text="Hello. ").event())
            events = await read_until(c, "error")
            assert events and Error.from_event(events[-1]).code == "server_busy"
            assert await c.read_event() is None
    finally:
        gate.set()
        await asyncio.gather(*holders)
    assert ctx.queue.depth == 0
    assert ctx.health.status == "ok"


async def test_streaming_queue_full_mid_stream(
    tts_app: FastAPI, tts_listener: WyomingListener
) -> None:
    """Audio has already started; the slot fills up before sentence two."""
    ctx = tts_listener.ctx
    gate = asyncio.Event()

    async def hold() -> None:
        async with ctx.queue.slot():
            await gate.wait()

    holders: list[asyncio.Task[None]] = []
    try:
        async with client(tts_listener) as c:
            await c.write_event(SynthesizeStart(voice=SynthesizeVoice(name="af_heart")).event())
            await c.write_event(SynthesizeChunk(text="One. ").event())
            early = await read_until(c, "audio-chunk")
            assert [e.type for e in early] == ["audio-start", "audio-chunk"]
            await wait_for(lambda: ctx.queue.depth == 0)  # sentence one released the slot
            holders = [asyncio.create_task(hold()) for _ in range(ctx.queue.capacity)]
            await wait_for(lambda: ctx.queue.depth == ctx.queue.capacity)
            await c.write_event(SynthesizeChunk(text="Two. ").event())
            events = await read_until(c, "error")
            assert events[-1].type == "error"
            assert Error.from_event(events[-1]).code == "server_busy"
            assert await c.read_event() is None  # connection closed
    finally:
        gate.set()
        if holders:
            await asyncio.gather(*holders)
    await wait_for(lambda: ctx.queue.depth == 0)
    assert ctx.health.status == "ok"


async def test_disconnect_mid_stream_cancels_worker(
    tts_app: FastAPI, tts_listener: WyomingListener
) -> None:
    engine: FakeTTSEngine = tts_app.state.ctx.engine
    engine.latency_s = 0.2  # keep the worker busy
    ctx = tts_listener.ctx
    start = time.perf_counter()
    async with client(tts_listener) as c:
        await c.write_event(SynthesizeStart().event())
        await c.write_event(SynthesizeChunk(text="One. Two. Three. Four. Five. ").event())
        await wait_for(lambda: ctx.queue.depth == 1)
    await wait_for(lambda: ctx.queue.depth == 0)
    elapsed = time.perf_counter() - start
    # Only the first sentence was ever started; the worker was cancelled, not
    # left to grind through the remaining four. A disconnect is not a failure.
    assert engine.calls == 1
    assert elapsed < 5 * 0.2
    assert ctx.health.status == "ok"
    assert ctx.health.last_error is None


async def test_plain_disconnect_mid_synthesis_stays_healthy(
    tts_app: FastAPI, tts_listener: WyomingListener
) -> None:
    engine: FakeTTSEngine = tts_app.state.ctx.engine
    engine.latency_s = 0.2
    ctx = tts_listener.ctx
    async with client(tts_listener) as c:
        await c.write_event(Synthesize(text="One. Two. Three. Four. Five. Six.").event())
        await wait_for(lambda: ctx.queue.depth == 1)
    await wait_for(lambda: ctx.queue.depth == 0, within=3.0)
    assert engine.calls == 1
    assert ctx.health.status == "ok"  # dropping the client is not an engine failure
    assert ctx.health.last_error is None


async def test_synthesize_stopped_is_last(tts_listener: WyomingListener) -> None:
    async with client(tts_listener) as c:
        await c.write_event(SynthesizeStart().event())
        await c.write_event(SynthesizeChunk(text="Only one sentence").event())
        await c.write_event(SynthesizeStop().event())
        events = await read_until(c, "synthesize-stopped")
    assert events[-1].type == "synthesize-stopped"
    assert SynthesizeStopped.is_type(events[-1].type)
    assert AudioStop.is_type(events[-2].type)
    assert "audio-chunk" in [e.type for e in events]


class _StallWriter:
    """A stream writer whose ``drain`` never returns in time, to exercise the
    write timeout without a real socket."""

    def __init__(self, *, closing: bool = False, drain_delay: float = 10.0) -> None:
        self._closing = closing
        self._drain_delay = drain_delay

    def get_extra_info(self, name: str, default: object = None) -> tuple[str, int]:
        return ("203.0.113.1", 9999)

    def is_closing(self) -> bool:
        return self._closing

    def writelines(self, data: object) -> None:
        pass

    def write(self, data: object) -> None:
        pass

    async def drain(self) -> None:
        await asyncio.sleep(self._drain_delay)

    def close(self) -> None:
        self._closing = True

    async def wait_closed(self) -> None:
        pass


async def test_write_timeout_is_treated_as_disconnect(tts_app: FastAPI) -> None:
    ctx = tts_app.state.ctx
    await ctx.engine.load()
    ctx.settings.server.write_timeout_s = 0.1
    handler = TTSEventHandler(
        ctx, asyncio.StreamReader(), cast("asyncio.StreamWriter", _StallWriter())
    )
    with pytest.raises(ClientTimeout):
        await handler.write_event(Describe().event())
    assert ctx.health.last_error is None  # a stuck client is not an engine failure


async def test_write_to_closing_transport_does_not_raise_type_error(tts_app: FastAPI) -> None:
    ctx = tts_app.state.ctx
    await ctx.engine.load()
    handler = TTSEventHandler(
        ctx, asyncio.StreamReader(), cast("asyncio.StreamWriter", _StallWriter(closing=True))
    )
    with pytest.raises(ConnectionResetError):
        await handler.write_event(Describe().event())
    # send_error must swallow the disconnect, not raise TypeError on the closed
    # transport (the CPython 3.12 writelines behaviour the review flagged).
    await handler.send_error("boom", "engine_error")
    assert ctx.health.last_error is None
