"""``POST /v1/audio/transcriptions`` through the official openai client and raw httpx.

Only the tests that really decode audio need ffmpeg; the rest stub
``ffmpeg.decode`` through the ``fake_decode`` fixture so they run anywhere.
"""

from __future__ import annotations

import asyncio

import openai
import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from tests.api.conftest import (
    GatedSTTEngine,
    _openai_client,
    loaded,
    requires_ffmpeg,
    sine_wav,
    wait_for_depth,
)
from voiced.api import ffmpeg
from voiced.config import Settings
from voiced.engines import registry
from voiced.stt import build_stt_app
from voiced.testing import FakeSTTEngine, lifespan_client


@requires_ffmpeg
async def test_json_default(openai_stt: openai.AsyncOpenAI, stt_engine: FakeSTTEngine) -> None:
    result = await openai_stt.audio.transcriptions.create(
        file=("tone.wav", sine_wav(), "audio/wav"), model="whisper-1"
    )
    assert isinstance(result, openai.types.audio.Transcription)
    assert result.text == "hello world"
    assert stt_engine.calls == 1


@requires_ffmpeg
async def test_text(openai_stt: openai.AsyncOpenAI) -> None:
    result = await openai_stt.audio.transcriptions.create(
        file=("tone.wav", sine_wav(), "audio/wav"), model="whisper-1", response_format="text"
    )
    assert result == "hello world\n"


@requires_ffmpeg
async def test_verbose_json_with_granularities(openai_stt: openai.AsyncOpenAI) -> None:
    result = await openai_stt.audio.transcriptions.create(
        file=("tone.wav", sine_wav(seconds=1.0), "audio/wav"),
        model="whisper-1",
        language="en",
        response_format="verbose_json",
        timestamp_granularities=["segment"],
    )
    assert isinstance(result, openai.types.audio.TranscriptionVerbose)
    assert result.text == "hello world"
    assert result.language == "en"
    assert result.duration == pytest.approx(1.0, abs=0.01)
    assert result.segments is not None and len(result.segments) == 1
    assert result.segments[0].start == 0.0
    assert result.segments[0].end == pytest.approx(1.0, abs=0.01)
    assert result.segments[0].text == "hello world"


@requires_ffmpeg
@pytest.mark.parametrize(("fmt", "first_line"), [("srt", "1"), ("vtt", "WEBVTT")])
async def test_srt_and_vtt(openai_stt: openai.AsyncOpenAI, fmt: str, first_line: str) -> None:
    result = await openai_stt.audio.transcriptions.create(
        file=("tone.wav", sine_wav(), "audio/wav"),
        model="whisper-1",
        response_format=fmt,  # type: ignore[arg-type]
    )
    assert isinstance(result, str)
    assert result.splitlines()[0] == first_line
    assert "hello world" in result
    assert ("00:00:00,000 -->" if fmt == "srt" else "00:00:00.000 -->") in result


async def test_vtt_media_type(stt_client: AsyncClient, fake_decode: list[int]) -> None:
    r = await stt_client.post(
        "/v1/audio/transcriptions",
        files={"file": ("tone.wav", sine_wav(), "audio/wav")},
        data={"response_format": "vtt"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/vtt")
    assert fake_decode == [len(sine_wav())]


@requires_ffmpeg
async def test_health_and_metrics_record_inference(stt_client: AsyncClient) -> None:
    r = await stt_client.post(
        "/v1/audio/transcriptions", files={"file": ("tone.wav", sine_wav(), "audio/wav")}
    )
    assert r.status_code == 200
    health = (await stt_client.get("/health")).json()
    assert health["status"] == "ok"
    assert health["last_inference_at"] is not None
    assert health["last_inference_ms"] >= 0
    op = (await stt_client.get("/metrics.json")).json()["metrics"]["operations"]["transcribe"]
    assert op["counts"]["ok"] == 1
    assert op["latency"]["samples"] == 1


async def test_language_hint_is_passed_and_validated(
    stt_client: AsyncClient, fake_decode: list[int]
) -> None:
    ok = await stt_client.post(
        "/v1/audio/transcriptions",
        files={"file": ("tone.wav", sine_wav(), "audio/wav")},
        data={"language": "en", "response_format": "verbose_json"},
    )
    assert ok.status_code == 200 and ok.json()["language"] == "en"
    bad = await stt_client.post(
        "/v1/audio/transcriptions",
        files={"file": ("tone.wav", sine_wav(), "audio/wav")},
        data={"language": "xx"},
    )
    assert bad.status_code == 400
    assert bad.json()["error"]["type"] == "invalid_request_error"
    assert bad.json()["error"]["message"].startswith("UnsupportedLanguage: ")


async def test_configured_languages_are_enforced(
    settings: Settings, fake_decode: list[int]
) -> None:
    """The engine speaks ``en``; the operator configured ``de`` only."""
    settings.stt.languages = ["de"]
    app = build_stt_app(settings, FakeSTTEngine())
    async with lifespan_client(app) as c:
        (model,) = (await c.get("/v1/models")).json()["data"]
        assert model["languages"] == []
        r = await c.post(
            "/v1/audio/transcriptions",
            files={"file": ("tone.wav", sine_wav(), "audio/wav")},
            data={"language": "en"},
        )
        assert r.status_code == 400
        assert r.json()["error"]["type"] == "invalid_request_error"
        assert "'en' is not served" in r.json()["error"]["message"]
        assert fake_decode == [], "rejected before decoding"
        # No hint at all is still fine: the engine picks its default.
        r = await c.post(
            "/v1/audio/transcriptions", files={"file": ("tone.wav", sine_wav(), "audio/wav")}
        )
        assert r.status_code == 200

    settings.stt.languages = ["en-GB", "de"]
    app = build_stt_app(settings, FakeSTTEngine())
    async with lifespan_client(app) as c:
        (model,) = (await c.get("/v1/models")).json()["data"]
        assert model["languages"] == ["en-GB"], "the narrower tag of the matching pair"


async def test_model_is_ignored_unless_it_names_another_engine(
    settings: Settings, fake_decode: list[int]
) -> None:
    class OtherSTT(FakeSTTEngine):
        name = "other"
        model_id = "someone/other-stt"

    registry.register_stt_engine("other", OtherSTT)
    try:
        app = build_stt_app(settings, FakeSTTEngine())  # the map is built here, once
    finally:
        registry._local_stt.pop("other", None)
    # Other installed engines (the real ones, when present) are in the map too.
    assert {
        "other": "other",
        "someone/other-stt": "other",
    }.items() <= app.state.foreign_models.items()
    assert "fake" not in app.state.foreign_models

    async with loaded(app):
        client = _openai_client(app)
        ok = await client.audio.transcriptions.create(
            file=("tone.wav", sine_wav(), "audio/wav"), model="gpt-4o-transcribe"
        )
        assert ok.text == "hello world"
        with pytest.raises(openai.BadRequestError) as exc:
            await client.audio.transcriptions.create(
                file=("tone.wav", sine_wav(), "audio/wav"), model="someone/other-stt"
            )
        assert "other" in exc.value.message
        assert exc.value.body["type"] == "model_not_served"  # type: ignore[index]
        await client.close()


@requires_ffmpeg
async def test_invalid_audio_is_400(
    openai_stt: openai.AsyncOpenAI, stt_client: AsyncClient
) -> None:
    with pytest.raises(openai.BadRequestError) as exc:
        await openai_stt.audio.transcriptions.create(
            file=("x.bin", b"definitely not audio" * 50, "application/octet-stream"),
            model="whisper-1",
        )
    assert exc.value.body == {  # type: ignore[comparison-overlap]
        "message": "the upload is not an audio file ffmpeg can decode",
        "type": "invalid_audio",
    }, "ffmpeg's stderr (temp file names) stays out of the response"
    # Rejected before any inference: nothing reaches metrics (ADR 0005).
    assert (await stt_client.get("/metrics.json")).json()["metrics"]["recent_errors"] == []


async def test_missing_ffmpeg_is_500(
    stt_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ffmpeg, "ffmpeg_path", lambda: None)
    r = await stt_client.post(
        "/v1/audio/transcriptions", files={"file": ("tone.wav", sine_wav(), "audio/wav")}
    )
    assert r.status_code == 500
    assert r.json()["error"]["type"] == "ffmpeg_missing"
    assert "brew install ffmpeg" in r.json()["error"]["message"]


async def test_malformed_multipart_is_400_in_the_envelope(stt_client: AsyncClient) -> None:
    """Starlette's own HTTPException(400) for a broken body uses the shared envelope."""
    r = await stt_client.post(
        "/v1/audio/transcriptions",
        content=b"--xyz\r\nnot a part header\r\n",
        headers={"Content-Type": "multipart/form-data"},  # no boundary
    )
    assert r.status_code == 400
    assert r.json()["error"]["type"] == "invalid_request_error"
    assert set(r.json()) == {"error"}


async def test_unknown_route_uses_the_envelope(stt_client: AsyncClient) -> None:
    r = await stt_client.get("/v1/nope")
    assert r.status_code == 404
    assert r.json() == {"error": {"message": "Not Found", "type": "invalid_request_error"}}


async def test_bad_response_format_and_missing_file(stt_client: AsyncClient) -> None:
    r = await stt_client.post(
        "/v1/audio/transcriptions",
        files={"file": ("tone.wav", sine_wav(), "audio/wav")},
        data={"response_format": "yaml"},
    )
    assert r.status_code == 400 and "response_format" in r.json()["error"]["message"]
    r = await stt_client.post("/v1/audio/transcriptions", data={"model": "whisper-1"})
    assert r.status_code == 400 and "file" in r.json()["error"]["message"]


async def test_oversize_upload_is_413_before_decoding(
    settings: Settings, fake_decode: list[int]
) -> None:
    settings.stt.max_upload_mb = 1
    engine = FakeSTTEngine()
    app = build_stt_app(settings, engine)
    big = sine_wav(seconds=40.0)  # about 1.3 MB
    async with lifespan_client(app) as c:
        r = await c.post("/v1/audio/transcriptions", files={"file": ("big.wav", big, "audio/wav")})
        assert r.status_code == 413
        assert r.json()["error"]["type"] == "payload_too_large"

        # Same without a Content-Length header (chunked body): the bounded reader catches it.
        async def body():  # type: ignore[no-untyped-def]
            boundary = b"--xyz"
            yield (
                boundary + b'\r\nContent-Disposition: form-data; name="file"; filename="b.wav"\r\n'
            )
            yield b"Content-Type: audio/wav\r\n\r\n"
            for i in range(0, len(big), 65536):
                yield big[i : i + 65536]
            yield b"\r\n" + boundary + b"--\r\n"

        r = await c.post(
            "/v1/audio/transcriptions",
            content=body(),
            headers={"Content-Type": "multipart/form-data; boundary=xyz"},
        )
        assert r.status_code == 413
    assert fake_decode == []
    assert engine.calls == 0


async def test_file_size_limit_is_exact(settings: Settings, fake_decode: list[int]) -> None:
    """The body cap allows multipart framing slack; the file itself is checked exactly."""
    settings.stt.max_upload_mb = 1
    app = build_stt_app(settings, FakeSTTEngine())
    limit = 1024 * 1024
    async with lifespan_client(app) as c:
        over = await c.post(
            "/v1/audio/transcriptions",
            files={"file": ("a.wav", b"\x01" * (limit + 1000), "audio/wav")},
        )
        assert over.status_code == 413, "within the body slack, but the file is too big"
        at = await c.post(
            "/v1/audio/transcriptions", files={"file": ("a.wav", b"\x01" * limit, "audio/wav")}
        )
        assert at.status_code == 200
    assert fake_decode == [limit]


async def test_queue_full_is_503(settings: Settings, fake_decode: list[int]) -> None:
    engine = GatedSTTEngine()
    app = build_stt_app(settings, engine)  # queue_depth is 2 in the test settings
    async with lifespan_client(app) as c:

        async def one() -> int:
            r = await c.post(
                "/v1/audio/transcriptions", files={"file": ("t.wav", sine_wav(), "audio/wav")}
            )
            return r.status_code

        first = asyncio.create_task(one())
        second = asyncio.create_task(one())
        await wait_for_depth(app, 2)
        third = await one()
        assert third == 503
        engine.gate.set()
        assert await asyncio.gather(first, second) == [200, 200]
    counts = app.state.ctx.metrics.snapshot()["operations"]["transcribe"]["counts"]
    assert counts["busy"] == 1 and counts["ok"] == 2


async def test_api_key_required(stt_app: FastAPI, fake_decode: list[int]) -> None:
    stt_app.state.ctx.settings.server.api_key = "s3cret"
    async with loaded(stt_app):
        wrong = _openai_client(stt_app, api_key="nope")
        with pytest.raises(openai.AuthenticationError):
            await wrong.audio.transcriptions.create(
                file=("t.wav", sine_wav(), "audio/wav"), model="whisper-1"
            )
        right = _openai_client(stt_app, api_key="s3cret")
        result = await right.audio.transcriptions.create(
            file=("t.wav", sine_wav(), "audio/wav"), model="whisper-1"
        )
        assert result.text == "hello world"
        await wrong.close()
        await right.close()


async def test_engine_failure_is_500_and_degrades_health(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, fake_decode: list[int]
) -> None:
    engine = FakeSTTEngine()
    app = build_stt_app(settings, engine)

    async def boom(*a, **k):  # type: ignore[no-untyped-def]
        raise RuntimeError("metal wedged")

    async with lifespan_client(app, raise_app_exceptions=False) as c:
        monkeypatch.setattr(engine, "transcribe", boom)
        r = await c.post(
            "/v1/audio/transcriptions", files={"file": ("t.wav", sine_wav(), "audio/wav")}
        )
        assert r.status_code == 500
        assert r.json()["error"]["type"] == "server_error"
        health = (await c.get("/health")).json()
        assert health["status"] == "degraded"
        assert "metal wedged" in health["last_error"]


async def test_hang_up_mid_upload_is_quiet(
    stt_app: FastAPI, fake_decode: list[int], caplog: pytest.LogCaptureFixture
) -> None:
    """A client that drops halfway through the upload: nothing escapes the app (the
    server would log a traceback), nothing is logged at ERROR, nothing is decoded."""
    head = (
        b'--xyz\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\n'
        b"Content-Type: audio/wav\r\n\r\n"
    )
    messages = [
        {"type": "http.request", "body": head + b"\x00" * 4096, "more_body": True},
        {"type": "http.disconnect"},
    ]

    async def receive() -> dict[str, object]:
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        pass

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/audio/transcriptions",
        "raw_path": b"/v1/audio/transcriptions",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"voiced"), (b"content-type", b"multipart/form-data; boundary=xyz")],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8881),
    }
    async with loaded(stt_app):
        await stt_app(scope, receive, send)  # must not raise
    assert fake_decode == []
    assert [r.getMessage() for r in caplog.records if r.levelname == "ERROR"] == []
