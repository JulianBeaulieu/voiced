"""Idle unload and load on demand (ADR 0033), with the fake engines."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.event import Event

from tests.api.conftest import sine_wav
from tests.wyoming.conftest import client, read_until
from voiced.config import Settings
from voiced.context import ServiceContext
from voiced.engines.base import EngineNotLoaded, STTEngine
from voiced.models import ModelNotDownloaded
from voiced.stt import build_stt_app
from voiced.testing import FakeSTTEngine, FakeTTSEngine, lifespan_client
from voiced.tts import build_tts_app
from voiced.wyoming import WYOMING_STT_SERVICE
from voiced.wyoming.server import WyomingListener
from voiced.wyoming.stt_handler import STTEventHandler

IDLE_S = 0.05


async def until(check: Callable[[], bool], timeout_s: float = 3.0) -> None:
    async with asyncio.timeout(timeout_s):
        while not check():  # noqa: ASYNC110 - polls state the code under test owns
            await asyncio.sleep(0.005)


def idle(app: FastAPI, idle_s: float = IDLE_S) -> ServiceContext:
    """The app's context, unloading after ``idle_s`` (set before the lifespan
    starts the first load, which starts the timer)."""
    ctx: ServiceContext = app.state.ctx
    ctx.residency.idle_unload_s = idle_s
    return ctx


async def transcribe(c: AsyncClient) -> int:
    files = {"file": ("tone.wav", sine_wav(), "audio/wav")}
    r = await c.post("/v1/audio/transcriptions", files=files)
    return r.status_code


def chunk() -> Event:
    return AudioChunk(rate=16_000, width=2, channels=1, audio=b"\x10\x00" * 3200).event()


@asynccontextmanager
async def idle_listener(
    app: FastAPI,
) -> AsyncIterator[tuple[ServiceContext, FakeSTTEngine, WyomingListener]]:
    """The STT app running, its model already unloaded, with a Wyoming listener."""
    ctx = idle(app)
    engine = ctx.engine
    assert isinstance(engine, FakeSTTEngine)
    ctx.settings.server.zeroconf = False
    async with lifespan_client(app):
        await until(lambda: engine.unloads == 1)
        listener = WyomingListener(
            ctx, "127.0.0.1", 0, handler_cls=STTEventHandler, service_name=WYOMING_STT_SERVICE
        )
        async with listener:
            yield ctx, engine, listener


@pytest.fixture
async def stt_idle(
    stt_app: FastAPI, stt_engine: FakeSTTEngine
) -> AsyncIterator[tuple[AsyncClient, ServiceContext]]:
    ctx = idle(stt_app)
    async with lifespan_client(stt_app, raise_app_exceptions=False) as c:
        await until(lambda: stt_engine.unloads == 1)
        yield c, ctx


# --- the timer -----------------------------------------------------------------


async def test_unloads_after_idle_and_health_says_idle(
    stt_idle: tuple[AsyncClient, ServiceContext], stt_engine: FakeSTTEngine
) -> None:
    c, _ = stt_idle
    r = await c.get("/health")
    assert r.status_code == 200, "an idle service takes requests"
    assert r.json()["status"] == "idle"
    assert r.json()["model_loaded"] is False
    assert stt_engine.loaded is False
    await asyncio.sleep(IDLE_S * 4)
    assert stt_engine.unloads == 1, "nothing to unload twice"


async def test_zero_never_unloads(stt_client: AsyncClient, stt_engine: FakeSTTEngine) -> None:
    await asyncio.sleep(0.1)
    assert stt_engine.unloads == 0
    assert (await stt_client.get("/health")).json()["status"] == "ok"


async def test_each_request_starts_the_idle_time_again(
    stt_app: FastAPI, stt_engine: FakeSTTEngine
) -> None:
    idle(stt_app, 0.3)
    async with lifespan_client(stt_app) as c:
        for _ in range(5):
            await asyncio.sleep(0.1)
            assert await transcribe(c) == 200
        assert stt_engine.unloads == 0
        await until(lambda: stt_engine.unloads == 1)


async def test_never_unloads_while_a_stream_is_open(stt_app: FastAPI) -> None:
    async with idle_listener(stt_app) as (_, engine, listener), client(listener) as c:
        await c.write_event(AudioStart(rate=16_000, width=2, channels=1).event())
        await c.write_event(chunk())
        await until(lambda: engine.loaded)
        await asyncio.sleep(IDLE_S * 5)
        assert engine.loaded, "the open session holds the slot"
        assert engine.unloads == 1
        await c.write_event(AudioStop().event())
        events = await read_until(c, "transcript", "error")
        assert events[-1].type == "transcript"
        await until(lambda: engine.unloads == 2)


# --- loading again ---------------------------------------------------------------


async def test_http_request_loads_the_model_again(
    stt_idle: tuple[AsyncClient, ServiceContext], stt_engine: FakeSTTEngine
) -> None:
    c, ctx = stt_idle
    assert await transcribe(c) == 200
    assert stt_engine.loads == 2
    assert stt_engine.calls == 1
    assert ctx.metrics.snapshot()["operations"]["transcribe"]["counts"]["ok"] == 1


async def test_a_stream_that_arrives_while_loading_waits(stt_app: FastAPI) -> None:
    """Wyoming: the session opens once the load is done; health says
    ``loading`` meanwhile, and the transcript arrives."""
    async with idle_listener(stt_app) as (ctx, engine, listener), client(listener) as c:
        engine.latency_s = 0.3  # the next load takes a while
        await c.write_event(AudioStart(rate=16_000, width=2, channels=1).event())
        await until(lambda: ctx.health.status == "loading")
        await c.write_event(chunk())
        await c.write_event(AudioStop().event())
        events = await read_until(c, "transcript", "error")
        assert events[-1].type == "transcript"
        assert engine.loads == 2
        assert ctx.health.status == "ok"


async def test_tts_lists_voices_while_unloaded_and_speaks(
    tts_app: FastAPI, tts_engine: FakeTTSEngine
) -> None:
    idle(tts_app)
    async with lifespan_client(tts_app) as c:
        await until(lambda: tts_engine.unloads == 1)
        voices = await c.get("/v1/audio/voices")
        assert voices.status_code == 200
        assert [v["id"] for v in voices.json()["data"]] == ["af_heart", "am_adam", "bf_emma"]
        assert tts_engine.loads == 1, "listing voices does not load the model"
        r = await c.post(
            "/v1/audio/speech",
            json={"input": "Hello.", "voice": "af_heart", "response_format": "pcm"},
        )
        assert r.status_code == 200
        assert r.content
        assert tts_engine.loads == 2


async def test_a_failed_load_fails_the_request_and_the_next_tries_again(
    stt_idle: tuple[AsyncClient, ServiceContext],
    stt_engine: FakeSTTEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c, _ = stt_idle
    load = stt_engine.load

    async def broken() -> None:
        raise RuntimeError("Metal said no")

    monkeypatch.setattr(stt_engine, "load", broken)
    assert await transcribe(c) == 500
    health = (await c.get("/health")).json()
    assert health["status"] == "degraded"
    assert "Metal said no" in health["last_error"]
    monkeypatch.setattr(stt_engine, "load", load)
    assert await transcribe(c) == 200
    assert (await c.get("/health")).json()["status"] == "ok"


async def test_a_model_deleted_while_idle_is_missing_model(
    stt_idle: tuple[AsyncClient, ServiceContext],
    stt_engine: FakeSTTEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c, _ = stt_idle

    async def gone() -> None:
        raise ModelNotDownloaded("fake/stt", "abc", ("fake/stt",))

    monkeypatch.setattr(stt_engine, "load", gone)
    files = {"file": ("tone.wav", sine_wav(), "audio/wav")}
    r = await c.post("/v1/audio/transcriptions", files=files)
    assert r.status_code == 503
    assert r.json()["error"]["type"] == "model_not_downloaded"
    health = await c.get("/health")
    assert health.status_code == 503
    assert health.json()["status"] == "missing_model"


async def test_a_request_during_an_unload_waits_then_loads_again(stt_app: FastAPI) -> None:
    """An HTTP request and a Wyoming stream arrive while ``unload()`` runs:
    both wait for it, then for one reload, and both are answered."""
    async with idle_listener(stt_app) as (ctx, engine, listener):
        c = AsyncClient(transport=ASGITransport(app=stt_app), base_url="http://voiced")
        assert await transcribe(c) == 200, "loaded again"
        gate = engine.unload_gate = asyncio.Event()
        unloading = asyncio.create_task(ctx.residency._unload())
        await until(lambda: ctx.health.status == "idle" and not gate.is_set())
        upload = asyncio.create_task(transcribe(c))
        async with client(listener) as w:
            await w.write_event(AudioStart(rate=16_000, width=2, channels=1).event())
            await w.write_event(chunk())
            await asyncio.sleep(0.05)
            assert engine.loads == 2, "nothing loads while the unload runs"
            seen: list[str] = []
            engine.latency_s = 0.1
            gate.set()
            await until(lambda: seen.append(ctx.health.status) or ctx.health.status == "ok")
            await w.write_event(AudioStop().event())
            events = await read_until(w, "transcript", "error")
        assert events[-1].type == "transcript"
        assert await upload == 200
        await unloading
        assert "loading" in seen
        assert engine.loads == 3 and engine.unloads == 2
        await c.aclose()


async def test_a_cancelled_reload_still_finishes(
    stt_idle: tuple[AsyncClient, ServiceContext], stt_engine: FakeSTTEngine
) -> None:
    """The client hangs up while the model loads again: the load completes
    and health says so, and the next request does not load a second copy."""
    c, ctx = stt_idle
    stt_engine.latency_s = 0.2
    hung_up = asyncio.create_task(transcribe(c))
    await until(lambda: ctx.health.status == "loading")
    hung_up.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await hung_up
    await until(lambda: ctx.health.status == "ok")
    assert stt_engine.loaded and stt_engine.loads == 2
    assert await transcribe(c) == 200
    assert stt_engine.loads == 2


async def test_two_streams_share_one_load(stt_app: FastAPI, settings: Settings) -> None:
    """Shared stream slots (``stt.streams = 2``): both sessions wait on the
    same reload."""
    async with idle_listener(stt_app) as (ctx, engine, listener):
        assert ctx.queue.max_shared == 2
        engine.latency_s = 0.2
        async with client(listener) as a, client(listener) as b:
            for w in (a, b):
                await w.write_event(AudioStart(rate=16_000, width=2, channels=1).event())
                await w.write_event(chunk())
            await until(lambda: ctx.queue.depth == 2)
            for w in (a, b):
                await w.write_event(AudioStop().event())
            results = [await read_until(w, "transcript", "error") for w in (a, b)]
        assert [r[-1].type for r in results] == ["transcript", "transcript"]
        assert engine.loads == 2


# --- engines that cannot unload, and the first load ------------------------------


class Stubborn(FakeSTTEngine):
    """An engine that keeps the base class's ``unload()``."""

    unload = STTEngine.unload


async def test_an_engine_without_unload_stays_loaded(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    engine = Stubborn()
    app = build_stt_app(settings, engine)
    idle(app)
    with caplog.at_level(logging.WARNING, logger="voiced.residency"):
        async with lifespan_client(app) as c:
            await until(lambda: "cannot unload" in caplog.text)
            await asyncio.sleep(IDLE_S * 3)
            assert (await c.get("/health")).json()["status"] == "ok"
            assert await transcribe(c) == 200
    assert caplog.text.count("cannot unload") == 1


async def test_before_the_first_load_requests_are_refused_not_loaded(
    stt_app: FastAPI,
) -> None:
    ctx: ServiceContext = stt_app.state.ctx
    with pytest.raises(EngineNotLoaded):
        await ctx.residency.ensure_loaded()


def test_engine_base_unload_is_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        asyncio.run(Stubborn().unload())


async def test_tts_app_builds_with_idle_setting(settings: Settings) -> None:
    settings.tts.idle_unload_s = 300
    app = build_tts_app(settings, FakeTTSEngine())
    assert app.state.ctx.residency.idle_unload_s == 300
