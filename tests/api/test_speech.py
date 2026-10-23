"""``POST /v1/audio/speech`` and ``GET /v1/audio/voices`` through the openai client."""

from __future__ import annotations

import asyncio
import json
import struct
import time
from collections.abc import AsyncIterator, Awaitable, Callable

import openai
import psutil
import pytest
import uvicorn
from fastapi import FastAPI
from httpx import AsyncClient

from tests.api.conftest import (
    GatedTTSEngine,
    _openai_client,
    loaded,
    requires_ffmpeg,
    wait_for_depth,
)
from voiced.api import ffmpeg
from voiced.cli import uvicorn_config
from voiced.config import Settings, config_path
from voiced.engines import registry
from voiced.engines.base import EngineError, PCMChunk, Voice
from voiced.testing import FakeTTSEngine, lifespan_client
from voiced.testing.fakes import PIECE_GAP_S, STEPS_PER_PIECE, SteppedTTSEngine
from voiced.tts import build_tts_app

TEXT = "Hello there. This is a test."
# FakeTTSEngine: 100 ms per 10 characters per sentence -> 12 + 15 chars -> 0.12 s + 0.15 s
EXPECTED_SAMPLES = int(0.12 * 24_000) + int(0.15 * 24_000)


async def test_wav_default_streams_with_unknown_length_header(
    openai_tts: openai.AsyncOpenAI, tts_engine: FakeTTSEngine
) -> None:
    async with openai_tts.audio.speech.with_streaming_response.create(
        model="tts-1", voice="af_heart", input=TEXT
    ) as response:
        assert response.http_response.headers["content-type"] == "audio/wav"
        chunks = [c async for c in response.iter_bytes()]
    body = b"".join(chunks)
    assert body[:4] == b"RIFF" and body[8:12] == b"WAVE"
    assert struct.unpack("<I", body[40:44]) == (0xFFFFFFFF,)
    assert struct.unpack("<IHHII", body[16:32])[2:] == (1, 24_000, 48_000)
    assert len(body) - 44 == EXPECTED_SAMPLES * 2
    assert tts_engine.calls == 1


async def test_pcm_via_openai_client(openai_tts: openai.AsyncOpenAI) -> None:
    async with openai_tts.audio.speech.with_streaming_response.create(
        model="tts-1", voice="af_heart", input=TEXT, response_format="pcm"
    ) as response:
        assert response.http_response.headers["content-type"] == "audio/pcm"
        chunks = [c async for c in response.iter_bytes()]
    assert sum(len(c) for c in chunks) == EXPECTED_SAMPLES * 2


@pytest.mark.parametrize("fmt", ["pcm", "wav"])
async def test_streams_chunk_by_chunk_before_synthesis_finishes(
    settings: Settings, fmt: str
) -> None:
    """httpx's ASGITransport buffers whole responses, so drive the ASGI app directly."""
    engine = FakeTTSEngine(latency_s=0.15)  # per sentence
    app = build_tts_app(settings, engine)
    async with loaded(app):
        started = time.perf_counter()
        run = await _asgi_post(app, {"input": "One. Two. Three.", "response_format": fmt})
    assert run.status == 200
    bodies = [b for b in run.bodies if b]
    assert len(bodies) >= 3, "one body message per engine chunk"
    first_at = run.body_times[0] - started
    last_at = run.body_times[-1] - started
    assert first_at < last_at - 0.1, "first bytes left before the last sentence was synthesized"
    payload = b"".join(bodies)
    if fmt == "wav":
        assert bodies[0][:4] == b"RIFF" and payload[:4] == b"RIFF"
        payload = payload[44:]
    # "One." and "Two." are 4 characters, "Three." is 6: 0.04 + 0.04 + 0.06 s of audio.
    assert len(payload) == (int(0.04 * 24_000) * 2 + int(0.06 * 24_000)) * 2


async def test_non_streaming_create_works_too(openai_tts: openai.AsyncOpenAI) -> None:
    result = await openai_tts.audio.speech.create(model="tts-1", voice="am_adam", input="Hi.")
    assert result.content[:4] == b"RIFF"
    assert len(result.content) == 44 + int(0.03 * 24_000) * 2  # "Hi." is 3 chars -> 0.03 s


@requires_ffmpeg
@pytest.mark.parametrize(
    ("fmt", "magic", "media"), [("mp3", b"ID3", "audio/mpeg"), ("opus", b"OggS", "audio/ogg")]
)
async def test_mp3_and_opus_via_ffmpeg(
    openai_tts: openai.AsyncOpenAI, fmt: str, magic: bytes, media: str
) -> None:
    async with openai_tts.audio.speech.with_streaming_response.create(
        model="tts-1",
        voice="af_heart",
        input=TEXT,
        response_format=fmt,  # type: ignore[arg-type]
    ) as response:
        assert response.http_response.headers["content-type"] == media
        body = b"".join([c async for c in response.iter_bytes()])
    assert body.startswith(magic)
    assert len(body) > 200


async def test_default_voice_and_speed_from_settings(settings: Settings) -> None:
    settings.tts.default_voice = "bf_emma"
    config_path().write_text("[tts]\nspeed = 1.5\n")
    calls: list[tuple[str, str, float]] = []

    class Recording(FakeTTSEngine):
        def synthesize(self, text: str, voice: str, speed: float = 1.0):  # type: ignore[no-untyped-def, override]
            calls.append((text, voice, speed))
            return super().synthesize(text, voice, speed)

    app = build_tts_app(settings, Recording())
    async with lifespan_client(app) as c:
        r = await c.post("/v1/audio/speech", json={"input": "Hi."})
        assert r.status_code == 200
        assert r.headers["content-type"] == "audio/wav"
        r = await c.post(
            "/v1/audio/speech", json={"input": "Yo.", "voice": "am_adam", "speed": 0.5}
        )
        assert r.status_code == 200
        # The default speed is read per request: a change needs no restart.
        config_path().write_text("[tts]\nspeed = 0.8  # slower, and longer\n")
        r = await c.post("/v1/audio/speech", json={"input": "Hey."})
        assert r.status_code == 200
        # An unreadable file keeps the last good reading.
        config_path().write_text("[tts\n")
        r = await c.post("/v1/audio/speech", json={"input": "Ho."})
        assert r.status_code == 200
    assert calls == [
        ("Hi.", "bf_emma", 1.5),
        ("Yo.", "am_adam", 0.5),
        ("Hey.", "bf_emma", 0.8),
        ("Ho.", "bf_emma", 0.8),
    ]


async def test_text_is_normalized_for_the_voice_language(settings: Settings) -> None:
    """Numbers become English words for an English voice only; a German voice,
    named or the configured default (Home Assistant's path), gets the digits,
    which it reads in German."""
    settings.tts.languages = ["en", "de"]
    settings.tts.default_voice = "de_anna"
    texts: dict[str, str] = {}

    class Bilingual(FakeTTSEngine):
        languages = ["en", "de"]

        async def voices(self) -> list[Voice]:
            german = Voice(id="de_anna", name="Anna", language="de-DE", gender="female")
            return [*await super().voices(), german]

        async def synthesize(  # type: ignore[override]
            self, text: str, voice: str, speed: float = 1.0
        ) -> AsyncIterator[PCMChunk]:
            texts[voice] = text
            yield PCMChunk(data=bytes(480), sample_rate=24_000)

    app = build_tts_app(settings, Bilingual())
    async with lifespan_client(app) as c:
        for voice in ("de_anna", "af_heart"):
            r = await c.post("/v1/audio/speech", json={"input": "20 Euro", "voice": voice})
            assert r.status_code == 200
        assert texts == {"de_anna": "20 Euro.", "af_heart": "twenty Euro."}
        r = await c.post("/v1/audio/speech", json={"input": "3,5 Euro"})
        assert r.status_code == 200
        assert texts["de_anna"] == "3,5 Euro."
        # A voice that is not served falls back to the German default, and
        # the text is normalized for German, the fallback's language.
        texts.clear()
        r = await c.post("/v1/audio/speech", json={"input": "20 Euro", "voice": "nobody"})
        assert r.status_code == 200
    assert texts == {"de_anna": "20 Euro."}


async def test_an_unknown_voice_gets_the_configured_one(
    openai_tts: openai.AsyncOpenAI, tts_client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """An OpenAI client's ``alloy``, or Home Assistant's voice of the engine
    before a switch, is spoken in ``tts.default_voice`` with a warning
    (ADR 0022), not refused."""
    speech = await openai_tts.audio.speech.create(model="tts-1", voice="nobody", input="Hi.")
    assert len(speech.content) > 0
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert "voice 'nobody' is not served by fake; using 'af_heart' (see /v1/audio/voices)" in (
        warnings
    )
    assert (await tts_client.get("/health")).json()["status"] == "ok"


async def test_input_too_long_is_413(openai_tts: openai.AsyncOpenAI, settings: Settings) -> None:
    with pytest.raises(openai.APIStatusError) as exc:
        await openai_tts.audio.speech.create(
            model="tts-1", voice="af_heart", input="x" * (settings.tts.max_input_chars + 1)
        )
    assert exc.value.status_code == 413
    assert exc.value.body["type"] == "payload_too_large"  # type: ignore[index]


async def test_empty_input_and_bad_format(tts_client: AsyncClient) -> None:
    r = await tts_client.post("/v1/audio/speech", json={"input": "   "})
    assert r.status_code == 400
    r = await tts_client.post("/v1/audio/speech", json={"input": "Hi.", "response_format": "flac"})
    assert r.status_code == 400  # body validation is 400 with the shared envelope, as OpenAI
    assert r.json()["error"]["type"] == "invalid_request_error"
    r = await tts_client.post("/v1/audio/speech", json={"input": "Hi.", "speed": 0})
    assert r.status_code == 400


async def test_model_naming_other_engine_is_400(settings: Settings) -> None:
    class OtherTTS(FakeTTSEngine):
        name = "other"
        model_id = "someone/other-tts"

    registry.register_tts_engine("other", OtherTTS)
    try:
        app = build_tts_app(settings, FakeTTSEngine())  # the map is built here, once
    finally:
        registry._local_tts.pop("other", None)
    # Other installed engines (Kokoro on main) are in the map too.
    expected = {"other": "other", "someone/other-tts": "other"}
    assert expected.items() <= app.state.foreign_models.items()
    assert "fake" not in app.state.foreign_models
    async with loaded(app):
        client = _openai_client(app)
        ok = await client.audio.speech.create(model="tts-1-hd", voice="af_heart", input="Hi.")
        assert ok.content
        with pytest.raises(openai.BadRequestError) as exc:
            await client.audio.speech.create(
                model="someone/other-tts", voice="af_heart", input="Hi."
            )
        assert exc.value.body["type"] == "model_not_served"  # type: ignore[index]
        await client.close()


async def test_queue_full_is_503(settings: Settings) -> None:
    engine = GatedTTSEngine()
    app = build_tts_app(settings, engine)  # queue_depth is 2 in the test settings
    async with loaded(app):
        client = _openai_client(app)

        async def one() -> int:
            try:
                await client.audio.speech.create(model="tts-1", voice="af_heart", input="Hi.")
                return 200
            except openai.APIStatusError as exc:
                return exc.status_code

        first = asyncio.create_task(one())
        second = asyncio.create_task(one())
        await wait_for_depth(app, 2)
        assert await one() == 503
        engine.gate.set()
        assert await asyncio.gather(first, second) == [200, 200]
        await client.close()
    assert app.state.ctx.queue.depth == 0
    counts = app.state.ctx.metrics.snapshot()["operations"]["speech"]["counts"]
    assert counts["busy"] == 1 and counts["ok"] == 2


@pytest.mark.parametrize("spec_version", ["2.0", "2.4"])
@pytest.mark.parametrize("fmt", ["pcm", "mp3"])
async def test_client_disconnect_mid_stream_releases_slot(
    settings: Settings, spec_version: str, fmt: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A hang-up releases the slot (and ffmpeg), is counted as a disconnect, and is
    not an error: nothing escapes the app and nothing is logged at ERROR.

    Under spec 2.0 Starlette cancels the streaming task; under 2.4 it leaves the
    body loop through ClientDisconnect without aclose(). Both must count."""
    if fmt == "mp3" and not ffmpeg.ffmpeg_available():
        pytest.skip("ffmpeg is not installed")
    engine = FakeTTSEngine(latency_s=0.05)
    app = build_tts_app(settings, engine)
    async with loaded(app):
        disconnect = asyncio.Event()
        got_first = asyncio.Event()

        async def on_body(body: bytes) -> None:
            if body:
                got_first.set()

        task = asyncio.create_task(
            _asgi_post(
                app,
                {
                    "input": "One. Two. Three. Four. Five. Six. Seven. Eight.",
                    "response_format": fmt,
                },
                disconnect=disconnect,
                on_body=on_body,
                spec_version=spec_version,
            )
        )
        await asyncio.wait_for(got_first.wait(), 2.0)
        assert app.state.ctx.queue.depth == 1
        disconnect.set()
        run = await asyncio.wait_for(task, 2.0)  # ClientDisconnect no longer escapes
        assert len([b for b in run.bodies if b]) < 6, "stopped early"
        # The synthesis ends at the next chunk and frees the slot then.
        await _wait_until(lambda: app.state.ctx.queue.depth == 0)
        counts = app.state.ctx.metrics.snapshot()["operations"]["speech"]["counts"]
        assert counts["disconnect"] == 1 and counts["ok"] == 0
        assert app.state.ctx.health.snapshot(0).status == "ok", "a hang-up leaves health alone"
        assert [r.getMessage() for r in caplog.records if r.levelname == "ERROR"] == []
        # The next request gets the slot immediately.
        assert (await _asgi_post(app, {"input": "Hi."})).status == 200


async def test_a_hang_up_frees_the_slot_at_the_next_step_not_the_next_chunk(
    settings: Settings,
) -> None:
    """The engine's audio pieces are a second apart; a client that hangs up
    after the first one frees the slot at the engine's next step, and the
    next request gets it at once. The hang-up is still a disconnect."""
    engine = SteppedTTSEngine()
    app = build_tts_app(settings, engine)
    async with loaded(app):
        disconnect = asyncio.Event()

        async def on_body(body: bytes) -> None:
            if body:
                disconnect.set()  # hang up after the first audio

        first = asyncio.create_task(
            _asgi_post(app, {"input": "Hello there."}, disconnect=disconnect, on_body=on_body)
        )
        await asyncio.wait_for(disconnect.wait(), 2.0)
        hung_up = time.perf_counter()
        await _wait_until(lambda: app.state.ctx.queue.depth == 0)
        assert time.perf_counter() - hung_up < PIECE_GAP_S / 4
        await asyncio.wait_for(first, 2.0)
        steps = engine.steps
        assert steps < 10, "the synthesis stopped at its next step"
        engine.steps_per_piece = 1  # this one is read to the end

        engine_started = time.perf_counter()
        second = await _asgi_post(app, {"input": "Hi.", "response_format": "pcm"})
        assert second.status == 200 and second.body_times[0] - engine_started < 0.2
        counts = app.state.ctx.metrics.snapshot()["operations"]["speech"]["counts"]
        assert counts["disconnect"] == 1 and counts["ok"] == 1
        assert app.state.ctx.health.snapshot(0).status == "ok"


@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
async def test_a_hang_up_before_the_first_audio_frees_the_slot_at_the_next_step(
    settings: Settings, spec_version: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The first audio piece is two seconds of steps away; a client that hangs
    up before it frees the slot at the engine's next step, gets no response
    started, and is counted as one disconnect, not an error."""
    engine = SteppedTTSEngine(steps_before_first=STEPS_PER_PIECE)
    app = build_tts_app(settings, engine)
    async with loaded(app):
        disconnect = asyncio.Event()
        first = asyncio.create_task(
            _asgi_post(
                app, {"input": "Hello there."}, disconnect=disconnect, spec_version=spec_version
            )
        )
        await _wait_until(lambda: engine.steps > 0)
        disconnect.set()
        hung_up = time.perf_counter()
        await _wait_until(lambda: app.state.ctx.queue.depth == 0)
        assert time.perf_counter() - hung_up < PIECE_GAP_S / 4
        run = await asyncio.wait_for(first, 2.0)
        # Under 2.4 the send to the closed connection raises and nothing is recorded.
        assert run.status in (0, 499) and not any(run.bodies)
        assert engine.steps < 10, "the synthesis stopped at its next step"
        counts = app.state.ctx.metrics.snapshot()["operations"]["speech"]["counts"]
        assert counts["disconnect"] == 1 and counts["ok"] == 0
        assert app.state.ctx.health.snapshot(0).status == "ok"
    assert [r.getMessage() for r in caplog.records if r.levelname == "ERROR"] == []


async def test_a_hang_up_before_the_first_audio_over_a_real_socket(settings: Settings) -> None:
    """The same hang-up through uvicorn as ``voiced serve`` runs it: the
    server, not a test double, has to report the closed socket from
    ``receive()``."""
    settings.server.bind = "127.0.0.1"
    engine = SteppedTTSEngine(steps_before_first=STEPS_PER_PIECE)
    app = build_tts_app(settings, engine)
    server = uvicorn.Server(uvicorn_config(app, settings, port=0))
    serving = asyncio.create_task(server.serve())
    try:
        await _wait_until(lambda: server.started)
        await asyncio.wait_for(app.state.load_task, 5.0)
        port = server.servers[0].sockets[0].getsockname()[1]
        body = json.dumps({"input": "Hello there."}).encode()
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(
            b"POST /v1/audio/speech HTTP/1.1\r\nHost: voiced\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: %d\r\n\r\n%s" % (len(body), body)
        )
        await writer.drain()
        await _wait_until(lambda: engine.steps > 0)
        writer.close()
        await writer.wait_closed()
        hung_up = time.perf_counter()
        await _wait_until(lambda: app.state.ctx.queue.depth == 0)
        assert time.perf_counter() - hung_up < PIECE_GAP_S / 4
        assert engine.steps < 10
        counts = app.state.ctx.metrics.snapshot()["operations"]["speech"]["counts"]
        assert counts["disconnect"] == 1 and counts["ok"] == 0
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 5.0)


async def test_a_queued_request_that_hangs_up_leaves_the_queue(settings: Settings) -> None:
    """A request waiting for the slot is gone from the queue as soon as its
    client hangs up, without an engine call; the one holding the slot goes on."""
    engine = GatedTTSEngine()
    app = build_tts_app(settings, engine)
    async with loaded(app):
        running = asyncio.create_task(_asgi_post(app, {"input": "Hi.", "response_format": "pcm"}))
        await wait_for_depth(app, 1)
        disconnect = asyncio.Event()
        queued = asyncio.create_task(_asgi_post(app, {"input": "Hi."}, disconnect=disconnect))
        await wait_for_depth(app, 2)
        disconnect.set()
        await _wait_until(lambda: app.state.ctx.queue.depth == 1)
        assert (await asyncio.wait_for(queued, 2.0)).status == 499
        engine.gate.set()
        assert (await asyncio.wait_for(running, 2.0)).status == 200
        counts = app.state.ctx.metrics.snapshot()["operations"]["speech"]["counts"]
        assert counts["disconnect"] == 1 and counts["ok"] == 1


async def test_an_engine_error_before_any_audio_is_400(settings: Settings) -> None:
    class Refuses(FakeTTSEngine):
        async def synthesize(self, text, voice, speed=1.0):  # type: ignore[no-untyped-def, override]
            raise EngineError("cannot say that")
            yield

    app = build_tts_app(settings, Refuses())
    async with loaded(app):
        run = await _asgi_post(app, {"input": "Hi."})
        assert run.status == 400 and b"cannot say that" in b"".join(run.bodies)
        counts = app.state.ctx.metrics.snapshot()["operations"]["speech"]["counts"]
        assert counts["client_error"] == 1 and counts["disconnect"] == 0


@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
@pytest.mark.parametrize("fmt", ["pcm", "mp3"])
async def test_client_that_stops_reading_is_released_after_the_write_timeout(
    settings: Settings, fmt: str, spec_version: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A client that reads the headers and a little audio, then stops reading.

    The response gives up after server.write_timeout_s, so the task, the queued
    audio and any ffmpeg process are released. The input is long enough that
    ffmpeg's output backs up while the client stalls, which is the case that
    used to leave the response waiting on the ffmpeg process forever."""
    if fmt == "mp3" and not ffmpeg.ffmpeg_available():
        pytest.skip("ffmpeg is not installed")
    settings.server.write_timeout_s = 0.3
    app = build_tts_app(settings, FakeTTSEngine())
    text = "Sentence number. " * 230  # about 37 s of audio, 600 KB as mp3
    payload = json.dumps({"input": text, "response_format": fmt}).encode()
    delivered = 0
    sent_body = False

    async def receive() -> dict[str, object]:
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await asyncio.Event().wait()  # never reports a disconnect
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        nonlocal delivered
        if message["type"] == "http.response.body":
            delivered += 1
            if delivered > 2:
                await asyncio.Event().wait()  # the client stopped reading

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/audio/speech",
        "raw_path": b"/v1/audio/speech",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"voiced"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8880),
    }
    async with loaded(app):
        started = time.perf_counter()
        await asyncio.wait_for(app(scope, receive, send), 3.0)  # returns, nothing escapes
        assert time.perf_counter() - started < 2.0, "gave up after the write timeout"
        assert delivered == 3, "it was the stalled send that timed out"
        await _wait_until(lambda: app.state.ctx.queue.depth == 0)
        await _wait_until(lambda: not psutil.Process().children(recursive=True))
    assert [r.getMessage() for r in caplog.records if r.levelname == "ERROR"] == []


async def _wait_until(condition: Callable[[], bool]) -> None:
    """Poll for up to 2 s."""
    for _ in range(400):
        if condition():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not met within 2 s")


class _Run:
    def __init__(self) -> None:
        self.status = 0
        self.headers: dict[str, str] = {}
        self.bodies: list[bytes] = []
        self.body_times: list[float] = []


async def _asgi_post(
    app: FastAPI,
    body: dict[str, object],
    *,
    disconnect: asyncio.Event | None = None,
    on_body: Callable[[bytes], Awaitable[None]] | None = None,
    spec_version: str = "2.0",
) -> _Run:
    """POST /v1/audio/speech straight into the ASGI app, recording every body message.

    With ``disconnect`` set, the client goes away the way each ASGI spec
    version signals it: under 2.0 Starlette listens on ``receive``, which
    then reports ``http.disconnect``; under 2.4 it does not, and ``send``
    raises ``OSError`` like a server whose socket is gone. Raising from
    ``send`` under 2.0 as well would race the task-group cancellation and
    leak a bare ``OSError`` out of the app.
    """
    send_raises = tuple(int(x) for x in spec_version.split(".")) >= (2, 4)
    payload = json.dumps(body).encode()
    run = _Run()
    sent_body = False

    async def receive() -> dict[str, object]:
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": payload, "more_body": False}
        if disconnect is not None:
            await disconnect.wait()
        else:
            await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        if send_raises and disconnect is not None and disconnect.is_set():
            raise OSError("client gone")
        if message["type"] == "http.response.start":
            run.status = int(message["status"])  # type: ignore[call-overload]
            run.headers = {k.decode(): v.decode() for k, v in message["headers"]}  # type: ignore[attr-defined]
        elif message["type"] == "http.response.body":
            chunk = message.get("body", b"")
            assert isinstance(chunk, bytes)
            run.bodies.append(chunk)
            run.body_times.append(time.perf_counter())
            if on_body is not None:
                await on_body(chunk)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/audio/speech",
        "raw_path": b"/v1/audio/speech",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"voiced"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8880),
    }
    await app(scope, receive, send)
    return run


async def test_voices_listing(openai_tts: openai.AsyncOpenAI, tts_client: AsyncClient) -> None:
    r = await tts_client.get("/v1/audio/voices")
    assert r.status_code == 200
    doc = r.json()
    assert doc["object"] == "list"
    assert [v["id"] for v in doc["data"]] == ["af_heart", "am_adam", "bf_emma"]
    assert doc["data"][0] == {
        "object": "voice",
        "id": "af_heart",
        "name": "Heart",
        "language": "en-US",
        "gender": "female",
    }
    assert set(doc) == {"object", "data"}
    # Discovery through the openai client (generic GET) and the models listing.
    via_client = await openai_tts.get("/audio/voices", cast_to=dict)
    assert via_client["data"] == doc["data"]  # type: ignore[index]
    models = await openai_tts.models.list()
    assert [m.id for m in models.data] == ["fake/tts"]


async def test_voices_and_models_follow_configured_languages(settings: Settings) -> None:
    settings.tts.languages = ["de"]
    app = build_tts_app(settings, FakeTTSEngine())
    async with lifespan_client(app) as c:
        assert (await c.get("/v1/audio/voices")).json()["data"] == []
        (model,) = (await c.get("/v1/models")).json()["data"]
        assert model["languages"] == []
        # The engine knows af_heart, but English is not configured: with no
        # voice served there is nothing to fall back to.
        r = await c.post("/v1/audio/speech", json={"input": "Hi.", "voice": "af_heart"})
        assert r.status_code == 400
        assert r.json()["error"]["message"] == (
            "UnknownVoice: voice 'af_heart' is not served; see /v1/audio/voices"
        )
        # The configured default voice is held to the same rule.
        r = await c.post("/v1/audio/speech", json={"input": "Hi."})
        assert r.status_code == 400
        assert "'af_heart' (tts.default_voice) is not served" in r.json()["error"]["message"]
    settings.tts.languages = ["en-US", "de"]
    app = build_tts_app(settings, FakeTTSEngine())
    async with lifespan_client(app) as c:
        ids = [v["id"] for v in (await c.get("/v1/audio/voices")).json()["data"]]
        assert ids == ["af_heart", "am_adam", "bf_emma"], "en-GB voices match en too"
        (model,) = (await c.get("/v1/models")).json()["data"]
        assert model["languages"] == ["en-US"], "the narrower tag of the matching pair"
        r = await c.post("/v1/audio/speech", json={"input": "Hi.", "voice": "bf_emma"})
        assert r.status_code == 200


async def test_api_key_on_tts(tts_app: FastAPI) -> None:
    tts_app.state.ctx.settings.server.api_key = "s3cret"
    async with loaded(tts_app):
        wrong = _openai_client(tts_app, api_key="nope")
        with pytest.raises(openai.AuthenticationError):
            await wrong.audio.speech.create(model="tts-1", voice="af_heart", input="Hi.")
        right = _openai_client(tts_app, api_key="s3cret")
        assert (
            await right.audio.speech.create(model="tts-1", voice="af_heart", input="Hi.")
        ).content
        await wrong.close()
        await right.close()


async def test_engine_failure_mid_stream_degrades_health(settings: Settings) -> None:
    class Broken(FakeTTSEngine):
        async def synthesize(self, text, voice, speed=1.0):  # type: ignore[no-untyped-def, override]
            yield (await super().synthesize(text, voice, speed).__anext__())
            raise RuntimeError("metal wedged")

    app = build_tts_app(settings, Broken())
    async with lifespan_client(app) as c:
        with pytest.raises(RuntimeError):
            await c.post("/v1/audio/speech", json={"input": "One. Two.", "response_format": "pcm"})
        health = (await c.get("/health")).json()
        assert health["status"] == "degraded"
        assert app.state.ctx.queue.depth == 0
