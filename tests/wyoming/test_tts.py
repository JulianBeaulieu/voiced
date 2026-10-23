from __future__ import annotations

import asyncio
import socket
import struct
import time
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
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
from voiced.config import Settings, config_path
from voiced.engines.base import PCMChunk, Voice
from voiced.inference import ClientTimeout
from voiced.testing import FakeTTSEngine
from voiced.testing.fakes import FAKE_VOICES, PIECE_GAP_S, STEP_S, SteppedTTSEngine
from voiced.tts import build_tts_app
from voiced.wyoming import WYOMING_TTS_SERVICE, handlers
from voiced.wyoming.server import WyomingListener
from voiced.wyoming.tts_handler import TTSEventHandler

from .conftest import client, read_until, stub_writer, wait_for


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


async def test_speed_is_read_from_the_config_file_for_each_request(
    tts_app: FastAPI, tts_listener: WyomingListener
) -> None:
    engine: FakeTTSEngine = tts_app.state.ctx.engine
    speeds: list[float] = []
    original = engine.synthesize

    def spy(text: str, voice: str, speed: float = 1.0) -> AsyncIterator[PCMChunk]:
        speeds.append(speed)
        return original(text, voice, speed)

    engine.synthesize = spy  # type: ignore[method-assign]
    async with client(tts_listener) as c:
        await c.write_event(Synthesize(text="Hi.").event())
        await read_until(c, "audio-stop")
        config_path().write_text("[tts]\nspeed = 1.3\n")  # no restart
        await c.write_event(Synthesize(text="Hi.").event())
        await read_until(c, "audio-stop")
    assert speeds == [1.0, 1.3]


async def test_text_is_normalized_for_the_voice_language(
    tts_app: FastAPI, tts_listener: WyomingListener
) -> None:
    """Plain and streamed requests: a German voice keeps the digits and its
    ordinals, a Chinese one splits at the ideographic full stop, an English
    one gets English words."""
    engine: FakeTTSEngine = tts_app.state.ctx.engine
    tts_app.state.ctx.settings.tts.languages = ["en", "de", "zh"]
    engine.languages = ["en", "de", "zh"]
    german = Voice(id="de_anna", name="Anna", language="de-DE", gender="female")
    chinese = Voice(id="zh_li", name="Li", language="zh-CN", gender="female")
    seen: list[tuple[str, str]] = []
    original = engine.synthesize

    async def voices() -> list[Voice]:
        return [*FAKE_VOICES, german, chinese]

    def spy(text: str, voice: str, speed: float = 1.0) -> AsyncIterator[PCMChunk]:
        seen.append((voice, text))
        return original(text, "af_heart", speed)

    engine.voices = voices  # type: ignore[method-assign]
    engine.synthesize = spy  # type: ignore[method-assign]
    async with client(tts_listener) as c:
        await c.write_event(
            Synthesize(text="20 Euro", voice=SynthesizeVoice(name="de_anna")).event()
        )
        await read_until(c, "audio-stop")
        await c.write_event(SynthesizeStart(voice=SynthesizeVoice(name="de_anna")).event())
        await c.write_event(SynthesizeChunk(text="Am 3. Oktober kostet es 3,5 Euro. Gut").event())
        await c.write_event(SynthesizeStop().event())
        await read_until(c, "synthesize-stopped")
        await c.write_event(SynthesizeStart(voice=SynthesizeVoice(name="zh_li")).event())
        await c.write_event(SynthesizeChunk(text="下雨了。我们走吧").event())
        await c.write_event(SynthesizeStop().event())
        await read_until(c, "synthesize-stopped")
        await c.write_event(
            Synthesize(text="20 euros", voice=SynthesizeVoice(name="af_heart")).event()
        )
        await read_until(c, "audio-stop")
    assert seen == [
        ("de_anna", "20 Euro."),
        ("de_anna", "Am 3. Oktober kostet es 3,5 Euro."),
        ("de_anna", "Gut."),
        ("zh_li", "下雨了。"),
        ("zh_li", "我们走吧."),
        ("af_heart", "twenty euros."),
    ]


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


async def test_streaming_text_over_max_input_chars_errors_once_and_closes_at_stop(
    tts_app: FastAPI, tts_listener: WyomingListener
) -> None:
    ctx = tts_listener.ctx
    ctx.settings.tts.max_input_chars = 10
    async with client(tts_listener) as c:
        await c.write_event(SynthesizeStart().event())
        await c.write_event(SynthesizeChunk(text="one two ").event())
        await c.write_event(SynthesizeChunk(text="three four five").event())  # total over 10
        event = await c.read_event()
        assert event is not None and Error.is_type(event.type)
        error = Error.from_event(event)
        assert error.code == "invalid_request_error" and error.text.startswith("EngineError")
        # That one error is all: more text and the stop get nothing back, and
        # the stop closes.
        await c.write_event(SynthesizeChunk(text="six seven").event())
        await c.write_event(SynthesizeStop().event())
        assert await read_until(c) == []
    assert ctx.health.status == "ok"


async def test_an_unknown_voice_is_spoken_in_the_configured_one(
    tts_listener: WyomingListener,
) -> None:
    """Home Assistant keeps sending its pipeline's voice after the engine
    changes; refusing it would leave the assistant silent (ADR 0022)."""
    async with client(tts_listener) as c:
        await c.write_event(Synthesize(text="Hi.", voice=SynthesizeVoice(name="nope")).event())
        event = await c.read_event()
        assert event is not None and AudioStart.is_type(event.type)
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
            await c.write_event(SynthesizeChunk(text="Hello. And").event())
            events = await read_until(c, "error")
            assert events and Error.from_event(events[-1]).code == "server_busy"
            await c.write_event(SynthesizeStop().event())
            assert await c.read_event() is None  # closed at the stop
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
            await c.write_event(SynthesizeChunk(text="One. T").event())
            early = await read_until(c, "audio-chunk")
            assert [e.type for e in early] == ["audio-start", "audio-chunk"]
            await wait_for(lambda: ctx.queue.depth == 0)  # sentence one released the slot
            holders = [asyncio.create_task(hold()) for _ in range(ctx.queue.capacity)]
            await wait_for(lambda: ctx.queue.depth == ctx.queue.capacity)
            await c.write_event(SynthesizeChunk(text="wo. Three").event())
            events = await read_until(c, "error")
            assert events[-1].type == "error"
            assert Error.from_event(events[-1]).code == "server_busy"
            await c.write_event(SynthesizeStop().event())
            assert await c.read_event() is None  # closed at the stop
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


@pytest.mark.parametrize("reset", [False, True], ids=["close", "reset"])
async def test_a_hang_up_frees_the_slot_at_the_next_step_not_the_next_chunk(
    settings: Settings, reset: bool
) -> None:
    """The engine's audio pieces are a second apart; a client that closes the
    connection (or resets it) after the first one frees the slot at the
    engine's next step, and the next request gets it at once. The hang-up is
    still a disconnect."""
    engine = SteppedTTSEngine()
    listener = await _listener(settings, engine)
    ctx = listener.ctx
    async with listener:
        async with client(listener) as c:
            await c.write_event(Synthesize(text="Hello there.").event())
            await read_until(c, "audio-chunk")
            if reset:
                _reset_on_close(c)
        hung_up = time.perf_counter()
        await wait_for(lambda: ctx.queue.depth == 0)
        assert time.perf_counter() - hung_up < PIECE_GAP_S / 4
        assert engine.steps < 10, "the synthesis stopped at its next step"

        engine.steps_per_piece = 1  # this one is read to the end
        async with client(listener) as c:
            asked = time.perf_counter()
            await c.write_event(Synthesize(text="Hi.").event())
            await read_until(c, "audio-chunk")
            # A synthesis left running would hold the model to its next piece.
            assert time.perf_counter() - asked < PIECE_GAP_S / 4
            await read_until(c, "audio-stop")
    counts = ctx.metrics.snapshot()["operations"]["speech"]["counts"]
    assert counts["disconnect"] == 1 and counts["ok"] == 1
    assert ctx.health.status == "ok"


async def test_a_stream_closed_while_its_text_arrives_frees_the_slot_at_the_next_step(
    settings: Settings,
) -> None:
    """No SynthesizeStop yet: the loop is reading, sees the close itself, and
    ends the sentence being spoken as a disconnect."""
    engine = SteppedTTSEngine()
    listener = await _listener(settings, engine)
    ctx = listener.ctx
    async with listener:
        async with client(listener) as c:
            await c.write_event(SynthesizeStart().event())
            await c.write_event(SynthesizeChunk(text="One. Two. Three. ").event())
            await read_until(c, "audio-chunk")
        hung_up = time.perf_counter()
        await wait_for(lambda: ctx.queue.depth == 0)
        assert time.perf_counter() - hung_up < PIECE_GAP_S / 4
        await wait_for(lambda: not listener._handlers)
    assert engine.steps < 10, "neither the rest of the sentence nor the next was spoken"
    counts = ctx.metrics.snapshot()["operations"]["speech"]["counts"]
    assert counts["disconnect"] == 1 and counts["ok"] == 0


async def test_a_stream_closed_after_synthesize_stop_frees_the_slot_at_the_next_step(
    settings: Settings,
) -> None:
    """Home Assistant sends SynthesizeStop once its text is written, so most of
    a stream is spoken after it, while the handler waits for the worker."""
    engine = SteppedTTSEngine()
    listener = await _listener(settings, engine)
    ctx = listener.ctx
    async with listener:
        async with client(listener) as c:
            await c.write_event(SynthesizeStart().event())
            await c.write_event(SynthesizeChunk(text="One. Two. Three.").event())
            await c.write_event(SynthesizeStop().event())
            await read_until(c, "audio-chunk")
        hung_up = time.perf_counter()
        await wait_for(lambda: ctx.queue.depth == 0)
        assert time.perf_counter() - hung_up < PIECE_GAP_S / 4
        await wait_for(lambda: not listener._handlers)
    assert engine.steps < 10, "neither the rest of the sentence nor the next was spoken"
    counts = ctx.metrics.snapshot()["operations"]["speech"]["counts"]
    assert counts["disconnect"] == 1 and counts["ok"] == 0
    assert ctx.health.status == "ok"


async def test_a_synthesis_longer_than_the_idle_bound_is_not_a_hang_up(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The idle bound on the next event runs from the end of the request, so a
    client that waits quietly for a long synthesis gets all of it."""
    engine = SteppedTTSEngine(steps_per_piece=5)
    monkeypatch.setattr(handlers, "CONNECTION_IDLE_S", 5 * STEP_S / 2)
    listener = await _listener(settings, engine)
    async with listener, client(listener) as c:
        await c.write_event(Synthesize(text="Hello there.").event())
        events = await read_until(c, "audio-stop")
        assert [e.type for e in events].count("audio-chunk") == 2
        assert await c.read_event() is None, "then closed for being idle"
    counts = listener.ctx.metrics.snapshot()["operations"]["speech"]["counts"]
    assert counts["ok"] == 1 and counts["disconnect"] == 0


def _reset_on_close(c: AsyncTcpClient) -> None:
    """Make closing ``c`` send a reset instead of end of file."""
    sock = c._writer.get_extra_info("socket")  # type: ignore[union-attr]
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))


async def _listener(settings: Settings, engine: SteppedTTSEngine) -> WyomingListener:
    """A TTS listener on ``engine``, loaded."""
    settings.server.zeroconf = False
    ctx = build_tts_app(settings, engine).state.ctx
    await engine.load()
    ctx.health.mark_loaded()
    return WyomingListener(
        ctx, "127.0.0.1", 0, handler_cls=TTSEventHandler, service_name=WYOMING_TTS_SERVICE
    )


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


async def test_write_timeout_is_treated_as_disconnect(tts_app: FastAPI) -> None:
    ctx = tts_app.state.ctx
    await ctx.engine.load()
    ctx.settings.server.write_timeout_s = 0.1
    handler = TTSEventHandler(ctx, asyncio.StreamReader(), stub_writer())
    with pytest.raises(ClientTimeout):
        await handler.write_event(Describe().event())
    assert ctx.health.last_error is None  # a stuck client is not an engine failure


async def test_write_to_closing_transport_does_not_raise_type_error(tts_app: FastAPI) -> None:
    ctx = tts_app.state.ctx
    await ctx.engine.load()
    handler = TTSEventHandler(ctx, asyncio.StreamReader(), stub_writer(closing=True))
    with pytest.raises(ConnectionResetError):
        await handler.write_event(Describe().event())
    # send_error must swallow the disconnect, not raise TypeError on the closed
    # transport (the CPython 3.12 writelines behaviour the review flagged).
    await handler.send_error("boom", "engine_error")
    assert ctx.health.last_error is None


async def test_disconnect_while_worker_waits_for_text_ends_the_worker(
    tts_listener: WyomingListener,
) -> None:
    """No sentence end and no SynthesizeStop: the worker is idle on its queue
    when the client goes away, and teardown must still end it."""
    async with client(tts_listener) as c:
        await c.write_event(SynthesizeStart().event())
        await c.write_event(SynthesizeChunk(text="still thinking").event())
        await wait_for(lambda: bool(tts_listener._handlers))
        (handler,) = tts_listener._handlers.values()
        assert isinstance(handler, TTSEventHandler)
        await wait_for(lambda: handler._stream is not None)
        stream = handler._stream
        assert stream is not None and stream.worker is not None
        worker = stream.worker
        assert not worker.done()
    await wait_for(worker.done)
    assert tts_listener.ctx.queue.depth == 0
    assert tts_listener.ctx.metrics.snapshot()["operations"] == {}, "nothing was spoken"


async def test_synthesis_before_the_model_loads_is_busy(tts_app: FastAPI) -> None:
    ctx = tts_app.state.ctx  # engine deliberately not loaded
    ctx.settings.server.zeroconf = False
    listener = WyomingListener(
        ctx, "127.0.0.1", 0, handler_cls=TTSEventHandler, service_name=WYOMING_TTS_SERVICE
    )
    async with listener, client(listener) as c:
        await c.write_event(Synthesize(text="Hi.").event())
        events = await read_until(c)
    assert [e.type for e in events] == ["error"]
    assert Error.from_event(events[0]).code == "server_busy"
    assert ctx.queue.depth == 0
