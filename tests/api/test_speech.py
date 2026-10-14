"""``POST /v1/audio/speech`` and ``GET /v1/audio/voices`` through the openai client."""

from __future__ import annotations

import asyncio
import json
import struct
import time
from collections.abc import Awaitable, Callable

import openai
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from starlette.requests import ClientDisconnect

from tests.api.conftest import (
    GatedTTSEngine,
    _openai_client,
    loaded,
    requires_ffmpeg,
    wait_for_depth,
)
from voiced.api import audio
from voiced.config import Settings
from voiced.testing import FakeTTSEngine, lifespan_client
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
    settings.tts.speed = 1.5
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
    assert calls == [("Hi.", "bf_emma", 1.5), ("Yo.", "am_adam", 0.5)]


async def test_unknown_voice_is_400(
    openai_tts: openai.AsyncOpenAI, tts_client: AsyncClient
) -> None:
    with pytest.raises(openai.BadRequestError) as exc:
        await openai_tts.audio.speech.create(model="tts-1", voice="nobody", input="Hi.")
    assert exc.value.body["type"] == "invalid_request_error"  # type: ignore[index]
    assert exc.value.body["message"] == "UnknownVoice: nobody"  # type: ignore[index]
    health = (await tts_client.get("/health")).json()
    assert health["status"] == "ok"  # client error, not an engine failure
    errors = (await tts_client.get("/metrics.json")).json()["metrics"]["recent_errors"]
    assert errors and errors[0]["outcome"] == "client_error" and errors[0]["operation"] == "speech"


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
    from voiced.engines import registry

    class OtherTTS(FakeTTSEngine):
        name = "other"
        model_id = "someone/other-tts"

    registry.register_tts_engine("other", OtherTTS)
    try:
        app = build_tts_app(settings, FakeTTSEngine())  # the map is built here, once
    finally:
        registry._local_tts.pop("other", None)
    assert app.state.foreign_models == {"other": "other", "someone/other-tts": "other"}
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
    settings: Settings, spec_version: str, fmt: str
) -> None:
    """Under spec 2.4 Starlette leaves the body loop through ClientDisconnect without
    aclose(); ClosingStreamingResponse must still release the slot (and ffmpeg)."""
    if fmt == "mp3" and not audio.ffmpeg_available():
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
        try:
            run = await asyncio.wait_for(task, 2.0)
            assert len([b for b in run.bodies if b]) < 6, "stopped early"
        except ClientDisconnect:
            pass  # the spec 2.4 path raises it from send()
        assert app.state.ctx.queue.depth == 0
        # The next request gets the slot immediately.
        assert (await _asgi_post(app, {"input": "Hi."})).status == 200


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
    settings.tts.languages = ["en-US", "de"]
    app = build_tts_app(settings, FakeTTSEngine())
    async with lifespan_client(app) as c:
        ids = [v["id"] for v in (await c.get("/v1/audio/voices")).json()["data"]]
        assert ids == ["af_heart", "am_adam", "bf_emma"], "en-GB voices match en too"
        (model,) = (await c.get("/v1/models")).json()["data"]
        assert model["languages"] == ["en-US"], "the narrower tag of the matching pair"


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
