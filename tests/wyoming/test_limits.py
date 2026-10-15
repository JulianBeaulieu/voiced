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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from wyoming.audio import AudioStart
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
from voiced.engines.base import Voice
from voiced.testing import FakeSTTEngine
from voiced.wyoming import handlers, server
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
        await async_write_event(Event("audio-start", fmt), writer)
        await async_write_event(Event("audio-chunk", fmt, bytes(3200)), writer)
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
        (Synthesize(text="Hi.", voice=SynthesizeVoice(name="no_such_voice")).event(), "Unknown"),
        (Event("synthesize", {"text": "Hi.", "voice": {"name": 7}}), "Malformed"),
        (Event("synthesize", {"text": 7}), "Malformed"),
        (Event("synthesize", {}), "Malformed"),
        (SynthesizeStart(voice=SynthesizeVoice(name="no_such_voice")).event(), "Unknown"),
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


async def test_tts_voice_outside_the_configured_languages_is_refused(
    tts_listener: WyomingListener, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wyoming serves the voices Describe lists, as HTTP does, not every
    voice the engine knows: a voice in a language that is not configured
    never reaches the engine. (Kokoro loads only its English voices, so this
    matters for engines that offer more languages.)"""
    ctx = tts_listener.ctx
    engine = ctx.tts_engine
    voices = [*await engine.voices(), Voice(id="jf_alpha", name="Alpha", language="ja-JP")]
    asked: list[str] = []

    async def with_japanese() -> list[Voice]:
        return voices

    def synthesize(text: str, voice: str, speed: float = 1.0) -> object:
        asked.append(voice)
        raise AssertionError("the engine was asked")

    monkeypatch.setattr(engine, "voices", with_japanese)
    monkeypatch.setattr(engine, "synthesize", synthesize)
    async with raw(tts_listener) as (reader, writer):
        request = Synthesize(text="Hi.", voice=SynthesizeVoice(name="jf_alpha"))
        await async_write_event(request.event(), writer)
        error = await read_error(reader)
    assert error.text.startswith("UnknownVoice")
    assert asked == []


@pytest.mark.parametrize(
    "start",
    [
        SynthesizeStart(voice=SynthesizeVoice(name="no_such_voice")).event(),
        Event("synthesize-start", {"voice": {"name": 7}}),
    ],
)
async def test_tts_stream_that_cannot_start_fails_once_and_closes(
    tts_listener: WyomingListener, start: Event
) -> None:
    """A rejected SynthesizeStart ends the stream: one Error, then the
    connection closes, rather than one error per chunk and for the stop."""
    async with raw(tts_listener) as (reader, writer):
        await async_write_event(start, writer)
        error = await read_error(reader)
        assert error.code == "invalid_request_error"
        # What a client streaming text would send next gets nothing back.
        for event in (
            SynthesizeChunk(text="Hello there. ").event(),
            SynthesizeChunk(text="More text. ").event(),
            SynthesizeStop().event(),
        ):
            with contextlib.suppress(ConnectionError, RuntimeError):
                await async_write_event(event, writer)
        assert await closed(reader)
