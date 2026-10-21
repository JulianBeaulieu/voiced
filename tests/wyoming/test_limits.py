"""What a misbehaving LAN client can make a Wyoming listener do.

The listeners have no authentication, so every bound here holds for anyone
who can reach the port: an event is refused from its header before its
payload is buffered, malformed events are client errors that leave health
alone, and idle or half-sent connections are closed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.error import Error
from wyoming.event import Event, async_read_event, async_write_event
from wyoming.tts import (
    Synthesize,
    SynthesizeChunk,
    SynthesizeStart,
    SynthesizeStop,
    SynthesizeVoice,
)

from tests.wyoming.conftest import wait_for
from voiced import inference
from voiced.engines.base import EngineError, PCMChunk, Voice
from voiced.testing import FakeSTTEngine, FakeTTSEngine
from voiced.wyoming import handlers, server, stt_handler
from voiced.wyoming.server import WyomingListener

Raw = tuple[asyncio.StreamReader, asyncio.StreamWriter]


@asynccontextmanager
async def raw(listener: WyomingListener) -> AsyncIterator[Raw]:
    reader, writer = await asyncio.open_connection("127.0.0.1", listener.port)
    try:
        yield reader, writer
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()


async def send_header(writer: asyncio.StreamWriter, header: dict[str, object]) -> None:
    writer.write(json.dumps(header).encode() + b"\n")
    await writer.drain()


async def read_error(reader: asyncio.StreamReader) -> Error:
    event = await asyncio.wait_for(async_read_event(reader), 5)
    assert event is not None and Error.is_type(event.type), event
    return Error.from_event(event)


async def events_until_eof(reader: asyncio.StreamReader) -> list[Event]:
    """Every event the server sends before it closes the connection."""
    events: list[Event] = []
    while (event := await asyncio.wait_for(async_read_event(reader), 5)) is not None:
        events.append(event)
    return events


async def closed(reader: asyncio.StreamReader) -> bool:
    with contextlib.suppress(ConnectionError):
        return await asyncio.wait_for(reader.read(), 5) == b""
    return True


@pytest.mark.parametrize(
    ("header", "message"),
    [
        ({"type": "audio-chunk", "payload_length": 2_000_000_000}, "payload_length"),
        ({"type": "describe", "data_length": 2_000_000_000}, "data_length"),
        ({"type": "describe", "payload_length": "12"}, "payload_length"),
        ({"type": "describe", "data_length": -1}, "data_length"),
        ({"data": {}}, "header must be an object with a type"),
        ({"type": "describe", "data": [1]}, "data must be an object"),
    ],
)
async def test_event_is_refused_from_its_header(
    stt_listener: WyomingListener, header: dict[str, object], message: str
) -> None:
    """The announced size is checked before reading the body: a client that
    announces 2 GB gets an error at once instead of the server buffering
    whatever it sends."""
    async with raw(stt_listener) as (reader, writer):
        await send_header(writer, header)
        error = await read_error(reader)
        assert error.code == "invalid_request_error"
        assert error.text.startswith("BadEvent") and message in error.text
        assert await closed(reader)
    assert stt_listener.ctx.health.status == "ok"


@pytest.mark.parametrize("line", [b"not json\n", b"[" * 60_000 + b"\n"])
async def test_header_that_is_not_json_is_refused(
    stt_listener: WyomingListener, line: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    """Deeply nested JSON raises RecursionError, not ValueError; it must be a
    refused event, not an unhandled exception logged per connection."""
    async with raw(stt_listener) as (reader, writer):
        writer.write(line)
        error = await read_error(reader)
        assert "header is not JSON" in error.text
        assert await closed(reader)
    assert "Unhandled exception" not in caplog.text


async def test_data_block_that_is_not_an_object_is_refused(stt_listener: WyomingListener) -> None:
    async with raw(stt_listener) as (reader, writer):
        await send_header(writer, {"type": "describe", "data_length": 3})
        writer.write(b"[1]")
        error = await read_error(reader)
        assert "data must be an object" in error.text


async def test_largest_allowed_payload_still_works(stt_listener: WyomingListener) -> None:
    ctx = stt_listener.ctx
    engine: FakeSTTEngine = ctx.engine  # type: ignore[assignment]
    audio = bytes(handlers.MAX_EVENT_BYTES)
    async with raw(stt_listener) as (reader, writer):
        await async_write_event(AudioStart(rate=16_000, width=2, channels=1).event(), writer)
        await async_write_event(
            Event("audio-chunk", {"rate": 16_000, "width": 2, "channels": 1}, audio), writer
        )
        await async_write_event(Event("audio-stop"), writer)
        event = await asyncio.wait_for(async_read_event(reader), 5)
    assert event is not None and event.type == "transcript"
    assert engine.calls == 1


@pytest.mark.parametrize(
    "fmt",
    [
        {"rate": "16000", "width": 2, "channels": 1},  # wrong type: was a TypeError
        {"rate": 1, "width": 2, "channels": 1},  # 16000 output samples per input sample
        {"rate": 16_000, "width": 2, "channels": 1_000_000},
        {"rate": 16_000, "width": True, "channels": 1},
        {"width": 2, "channels": 1},  # missing field: was a KeyError
    ],
)
async def test_bad_audio_format_is_a_client_error_and_health_stays_ok(
    stt_listener: WyomingListener, fmt: dict[str, object]
) -> None:
    """A format nobody can convert is the client's mistake. Before, a mistyped
    rate raised TypeError inside the slot, which marked the service degraded,
    and the watchdog restarts a service that stays degraded."""
    ctx = stt_listener.ctx
    engine: FakeSTTEngine = ctx.engine  # type: ignore[assignment]
    async with raw(stt_listener) as (reader, writer):
        # AudioStart alone: the session refuses its format at once. Writing
        # more after that would race the server's close.
        await async_write_event(Event("audio-start", fmt), writer)
        error = await read_error(reader)
    assert error.code == "invalid_request_error", error.text
    assert ctx.health.status == "ok"
    assert engine.calls == 0
    await wait_for(lambda: ctx.queue.depth == 0)


async def test_idle_connection_is_closed(
    stt_listener: WyomingListener, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handlers, "CONNECTION_IDLE_S", 0.2)
    async with raw(stt_listener) as (reader, _writer):
        assert await closed(reader)
    await wait_for(lambda: not stt_listener._handlers)


async def test_half_sent_event_is_closed_after_session_idle(
    stt_listener: WyomingListener,
) -> None:
    stt_listener.ctx.settings.server.session_idle_s = 0.2
    async with raw(stt_listener) as (reader, writer):
        await send_header(writer, {"type": "audio-chunk", "payload_length": 1000})
        writer.write(bytes(10))  # and never the rest
        await writer.drain()
        assert await closed(reader)
    assert stt_listener.ctx.health.status == "ok"


async def test_connections_over_the_cap_are_refused_and_counted_again_when_freed(
    stt_listener: WyomingListener, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Idle connections cannot take every file descriptor of the process,
    which the HTTP API and /health share."""
    monkeypatch.setattr(server, "MAX_CONNECTIONS", 2)
    async with raw(stt_listener), raw(stt_listener):
        await wait_for(lambda: len(stt_listener._handlers) == 2)
        async with raw(stt_listener) as (reader, _writer):
            assert await closed(reader)
    await wait_for(lambda: not stt_listener._handlers)
    async with raw(stt_listener) as (reader, writer):
        await async_write_event(Event("describe"), writer)
        event = await asyncio.wait_for(async_read_event(reader), 5)
        assert event is not None and event.type == "info"


@pytest.mark.parametrize(
    ("event", "kind"),
    [
        (Event("synthesize", {"text": "Hi.", "voice": {"name": 7}}), "Malformed"),
        (Event("synthesize", {"text": 7}), "Malformed"),
        (Event("synthesize", {}), "Malformed"),
    ],
)
async def test_tts_bad_voice_or_text_is_a_client_error(
    tts_listener: WyomingListener, event: Event, kind: str
) -> None:
    ctx = tts_listener.ctx
    async with raw(tts_listener) as (reader, writer):
        await async_write_event(event, writer)
        error = await read_error(reader)
    assert error.code == "invalid_request_error", error.text
    assert error.text.startswith(kind), error.text
    assert ctx.health.status == "ok"


async def test_tts_voice_outside_the_configured_languages_is_not_used(
    tts_listener: WyomingListener, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wyoming serves the voices Describe lists, as HTTP does, not every
    voice the engine knows: a voice in a language that is not configured
    never reaches the engine, which is asked for the configured voice
    instead. (Kokoro loads only its English voices, so this matters for
    engines that offer more languages.)"""
    ctx = tts_listener.ctx
    engine = ctx.tts_engine
    voices = [*await engine.voices(), Voice(id="jf_alpha", name="Alpha", language="ja-JP")]
    asked: list[str] = []

    async def with_japanese() -> list[Voice]:
        return voices

    def synthesize(text: str, voice: str, speed: float = 1.0) -> object:
        asked.append(voice)
        raise EngineError("stop here")

    monkeypatch.setattr(engine, "voices", with_japanese)
    monkeypatch.setattr(engine, "synthesize", synthesize)
    async with raw(tts_listener) as (reader, writer):
        request = Synthesize(text="Hi.", voice=SynthesizeVoice(name="jf_alpha"))
        await async_write_event(request.event(), writer)
        await read_error(reader)
    assert asked == [ctx.settings.tts.default_voice]


@pytest.mark.parametrize(
    "start",
    [Event("synthesize-start", {"voice": {"name": 7}})],
)
@pytest.mark.parametrize("gap_s", [0.0, 0.05, 0.3])
async def test_tts_stream_that_cannot_start_fails_once_and_closes(
    tts_listener: WyomingListener, start: Event, gap_s: float
) -> None:
    """A rejected SynthesizeStart gets exactly one Error. Home Assistant writes
    the whole stream (chunks, the compatibility Synthesize, the stop) before
    it reads, so the server drops the rest of the stream and closes at
    SynthesizeStop: closing at once would answer those writes with a reset,
    which can discard the Error before the client reads it."""
    async with raw(tts_listener) as (reader, writer):
        for event in (
            start,
            SynthesizeChunk(text="Hello there. ").event(),
            SynthesizeChunk(text="More text. ").event(),
            SynthesizeChunk(text="And more.").event(),
            Synthesize(text="Hello there. More text. And more.").event(),
            SynthesizeStop().event(),
        ):
            await async_write_event(event, writer)  # no reset: nothing is read yet
            await asyncio.sleep(gap_s)
        events = await events_until_eof(reader)  # a reset here fails the test
    assert [e.type for e in events] == ["error"]
    assert Error.from_event(events[0]).code == "invalid_request_error"
    assert tts_listener.ctx.health.status == "ok"


def chunk(seconds: float, rate: int = 16_000, width: int = 2) -> Event:
    audio = bytes(int(seconds * rate) * width)
    return AudioChunk(rate=rate, width=width, channels=1, audio=audio).event()


START = AudioStart(rate=16_000, width=2, channels=1).event()


async def write_stream_then_read(
    listener: WyomingListener, events: list[Event], gap_s: float
) -> list[Event]:
    """Write the whole stream, then read, as Home Assistant's wyoming STT
    client does. A reset on any write or read fails the test."""
    async with raw(listener) as (reader, writer):
        for event in events:
            await async_write_event(event, writer)
            await asyncio.sleep(gap_s)
        return await events_until_eof(reader)


async def _hold_slot(listener: WyomingListener, gate: asyncio.Event) -> asyncio.Task[None]:
    ctx = listener.ctx

    async def hold() -> None:
        async with ctx.queue.slot():
            await gate.wait()

    task = asyncio.create_task(hold())
    await wait_for(lambda: ctx.queue.depth == 1)
    return task


@pytest.mark.parametrize(
    ("kind", "code", "text"),
    [
        ("format-change", "invalid_request_error", "UnsupportedAudioFormat"),
        ("bad-width", "invalid_request_error", "UnsupportedAudioFormat"),
        ("utterance-limit", "invalid_request_error", "EngineError: more than"),
        ("server-busy", "server_busy", "QueueFull"),
    ],
)
@pytest.mark.parametrize("gap_s", [0.0, 0.05, 0.3])
async def test_stt_session_that_fails_reports_once_and_closes_at_audio_stop(
    stt_listener: WyomingListener,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    code: str,
    text: str,
    gap_s: float,
) -> None:
    """A failed STT session gets exactly one Error. Home Assistant writes every
    chunk and the AudioStop before it reads, so the server drops the rest of
    the session and closes at AudioStop: closing at once would answer those
    writes with a reset, which can discard the Error before it is read."""
    ctx = stt_listener.ctx
    engine: FakeSTTEngine = ctx.engine  # type: ignore[assignment]
    # The client is still writing when the session fails: its trailing audio
    # spans 0.75 s, longer than the 0.5 s the busy case waits for the slot.
    trailing = 3 if gap_s == 0 else math.ceil(0.75 / gap_s)
    after = [*(chunk(0.1) for _ in range(trailing)), AudioStop().event()]
    holder: asyncio.Task[None] | None = None
    gate = asyncio.Event()
    if kind == "format-change":
        events = [START, chunk(0.1), chunk(0.1, rate=48_000), *after]
    elif kind == "bad-width":
        events = [AudioStart(rate=16_000, width=3, channels=1).event(), *after]
    elif kind == "utterance-limit":
        ctx.settings.stt.max_upload_mb = 1  # about 31 s at 16 kHz
        events = [START, *(chunk(10.0) for _ in range(4)), *after]
    else:  # server-busy: the queue fills while another request holds the slot
        monkeypatch.setattr(stt_handler, "_QUEUE_CHUNKS", 2)
        ctx.settings.server.session_idle_s = 0.5
        holder = await _hold_slot(stt_listener, gate)
        events = [START, *(chunk(0.064) for _ in range(3)), *after]
    try:
        received = await write_stream_then_read(stt_listener, events, gap_s)
    finally:
        gate.set()
        if holder is not None:
            await holder
    assert [e.type for e in received] == ["error"]
    error = Error.from_event(received[0])
    assert error.code == code and error.text.startswith(text), error
    assert engine.calls == (0 if kind in ("bad-width", "server-busy") else 1)
    await wait_for(lambda: ctx.queue.depth == 0)
    assert ctx.health.status == "ok"


async def test_stt_session_over_its_wall_clock_reports_once_and_closes_at_audio_stop(
    stt_listener: WyomingListener, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wall-clock bound fires while the client is still trickling audio;
    its AudioStop, written before it reads, still finds the Error."""
    monkeypatch.setattr(inference, "utterance_limit_s", lambda settings: 0.2)
    ctx = stt_listener.ctx
    ctx.settings.server.session_idle_s = 0.5  # the session may stay open 0.7 s
    events = [START, *(chunk(0.001) for _ in range(9)), AudioStop().event()]
    received = await write_stream_then_read(stt_listener, events, 0.1)
    assert [e.type for e in received] == ["error"]
    assert "session open longer than 0.7 s" in Error.from_event(received[0]).text
    await wait_for(lambda: ctx.queue.depth == 0)
    assert ctx.health.status == "ok"


async def test_stt_failed_session_is_closed_after_session_idle_s_without_audio_stop(
    stt_listener: WyomingListener,
) -> None:
    """A client that keeps streaming after the Error and never sends AudioStop
    cannot hold the connection: the drop lasts session_idle_s."""
    ctx = stt_listener.ctx
    ctx.settings.server.session_idle_s = 0.3
    async with raw(stt_listener) as (reader, writer):
        for event in (START, chunk(0.1), chunk(0.1, rate=48_000)):
            await async_write_event(event, writer)
        error = await read_error(reader)
        assert error.text.startswith("UnsupportedAudioFormat")
        loop = asyncio.get_running_loop()
        started = loop.time()
        with contextlib.suppress(ConnectionError):
            while not reader.at_eof() and loop.time() - started < 3:
                await async_write_event(chunk(0.01), writer)
                await asyncio.sleep(0.05)
        assert loop.time() - started < 2
        assert await closed(reader)
    await wait_for(lambda: ctx.queue.depth == 0)


TTS_STREAM = [
    SynthesizeStart().event(),
    SynthesizeChunk(text="Hello there. ").event(),
    SynthesizeChunk(text="More text. ").event(),
    SynthesizeChunk(text="And more.").event(),
    Synthesize(text="Hello there. More text. And more.").event(),
    SynthesizeStop().event(),
]


@pytest.mark.parametrize(
    ("raised", "code", "health"),
    [
        (EngineError("the engine refused"), "invalid_request_error", "ok"),
        (RuntimeError("the engine broke"), "engine_error", "degraded"),
    ],
)
@pytest.mark.parametrize("gap_s", [0.0, 0.05, 0.3])
async def test_tts_stream_whose_engine_fails_mid_stream_reports_once_and_closes_at_stop(
    tts_listener: WyomingListener,
    monkeypatch: pytest.MonkeyPatch,
    raised: Exception,
    code: str,
    health: str,
    gap_s: float,
) -> None:
    """The engine speaks the first sentence and fails on the second. The
    stream gets exactly one Error; the rest of it, written before the client
    reads, is dropped and the connection closes at SynthesizeStop instead of
    answering those writes with a reset."""
    ctx = tts_listener.ctx
    engine: FakeTTSEngine = ctx.engine  # type: ignore[assignment]
    speak = engine.synthesize
    asked: list[str] = []

    async def fails_second(text: str, voice: str, speed: float = 1.0) -> AsyncIterator[PCMChunk]:
        asked.append(text)
        if len(asked) > 1:
            raise raised
        async for pcm in speak(text, voice, speed):
            yield pcm

    monkeypatch.setattr(engine, "synthesize", fails_second)
    received = await write_stream_then_read(tts_listener, TTS_STREAM, gap_s)
    assert [e.type for e in received] == ["audio-start", "audio-chunk", "error"]
    error = Error.from_event(received[-1])
    assert error.code == code and error.text.startswith(type(raised).__name__), error
    assert asked == ["Hello there.", "More text."]  # nothing after the failure
    await wait_for(lambda: ctx.queue.depth == 0)
    assert ctx.health.status == health


@pytest.mark.parametrize("gap_s", [0.0, 0.05, 0.3])
async def test_tts_stream_over_max_input_chars_reports_once_and_closes_at_stop(
    tts_listener: WyomingListener, gap_s: float
) -> None:
    """The third chunk takes the stream over the limit while the first
    sentence may still be speaking: that is cancelled, the stream gets one
    Error, and the rest of it is dropped until SynthesizeStop."""
    ctx = tts_listener.ctx
    ctx.settings.tts.max_input_chars = 30  # the chunks total 13, 24, 33
    received = await write_stream_then_read(tts_listener, TTS_STREAM, gap_s)
    types = [e.type for e in received]
    assert types[-1] == "error" and types.count("error") == 1, types
    assert set(types) <= {"audio-start", "audio-chunk", "error"}, types
    error = Error.from_event(received[-1])
    assert error.code == "invalid_request_error", error
    assert error.text.startswith("EngineError: text exceeds max_input_chars"), error
    await wait_for(lambda: ctx.queue.depth == 0)
    assert ctx.health.status == "ok"


async def test_tts_failed_stream_is_closed_after_session_idle_s_without_stop(
    tts_listener: WyomingListener,
) -> None:
    """A client that keeps sending text after the Error and never sends
    SynthesizeStop cannot hold the connection: the drop lasts session_idle_s."""
    ctx = tts_listener.ctx
    ctx.settings.server.session_idle_s = 0.3
    ctx.settings.tts.max_input_chars = 10
    async with raw(tts_listener) as (reader, writer):
        for event in (SynthesizeStart().event(), SynthesizeChunk(text="far over ten").event()):
            await async_write_event(event, writer)
        error = await read_error(reader)
        assert error.text.startswith("EngineError: text exceeds max_input_chars")
        loop = asyncio.get_running_loop()
        started = loop.time()
        with contextlib.suppress(ConnectionError):
            while not reader.at_eof() and loop.time() - started < 3:
                await async_write_event(SynthesizeChunk(text="more").event(), writer)
                await asyncio.sleep(0.05)
        assert loop.time() - started < 2
        assert await closed(reader)
    assert ctx.health.status == "ok"
