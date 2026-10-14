from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import pytest
from pydantic import ValidationError
from starlette.requests import ClientDisconnect
from starlette.websockets import WebSocketDisconnect
from uvicorn.protocols.utils import ClientDisconnected

from voiced.config import Settings
from voiced.context import ServiceContext
from voiced.engines.base import EngineNotLoaded, UnknownVoice, UnsupportedLanguage
from voiced.health import HealthState
from voiced.inference import ClientTimeout, bounded, classify, inference
from voiced.metrics import Operation, Outcome
from voiced.queue import InferenceQueue, QueueFull
from voiced.testing import FakeSTTEngine, FakeTTSEngine

OP = Operation.TRANSCRIBE


@pytest.fixture
def ctx(settings: Settings) -> ServiceContext:
    engine = FakeSTTEngine()
    c = ServiceContext(
        service="stt",
        settings=settings,
        engine=engine,
        queue=InferenceQueue(1),
        health=HealthState(service="stt", engine="fake", model="m", version="0", queue_capacity=1),
    )
    c.health.mark_loaded()
    return c


def counts(ctx: ServiceContext) -> dict[str, int]:
    ops = ctx.metrics.snapshot()["operations"]
    if OP.value not in ops:
        return {}
    return {k: v for k, v in ops[OP.value]["counts"].items() if v}


@pytest.mark.parametrize(
    ("exc", "outcome"),
    [
        (QueueFull(1), Outcome.BUSY),
        (EngineNotLoaded("x"), Outcome.BUSY),
        (UnknownVoice("x"), Outcome.CLIENT_ERROR),
        (ConnectionResetError(), Outcome.DISCONNECT),
        (asyncio.IncompleteReadError(b"", 4), Outcome.DISCONNECT),
        (WebSocketDisconnect(1000), Outcome.DISCONNECT),
        (ClientDisconnect(), Outcome.DISCONNECT),
        (ClientDisconnected(), Outcome.DISCONNECT),
        (ClientTimeout("idle"), Outcome.DISCONNECT),
        (TimeoutError(), Outcome.FAILURE),
        (FileNotFoundError("model.bin"), Outcome.FAILURE),
        (RuntimeError(), Outcome.FAILURE),
    ],
)
def test_classify(exc: Exception, outcome: Outcome) -> None:
    assert classify(exc) is outcome


async def test_bounded_raises_client_timeout() -> None:
    assert await bounded(asyncio.sleep(0, "x"), 1) == "x"
    with pytest.raises(ClientTimeout):
        await bounded(asyncio.Event().wait(), 0.01)


async def test_bounded_keeps_the_awaitables_own_timeout() -> None:
    async def engine_side() -> None:
        raise TimeoutError("inner")

    with pytest.raises(TimeoutError, match="inner") as info:
        await bounded(engine_side(), 10)
    assert not isinstance(info.value, ClientTimeout)


async def test_success_records_health_and_latency(ctx: ServiceContext) -> None:
    async with inference(ctx, OP):
        assert ctx.queue.depth == 1
        await asyncio.sleep(0.01)
    assert ctx.queue.depth == 0
    assert ctx.health.status == "ok"
    assert ctx.health.last_inference_ms is not None and ctx.health.last_inference_ms >= 10
    assert counts(ctx) == {"ok": 1}


@pytest.mark.parametrize(
    ("exc", "outcome"),
    [
        (UnknownVoice("nope"), "client_error"),
        (WebSocketDisconnect(1000), "disconnect"),
        (ClientTimeout("idle"), "disconnect"),
    ],
)
async def test_client_side_endings_leave_health_alone(
    ctx: ServiceContext, exc: Exception, outcome: str
) -> None:
    with pytest.raises(type(exc)):
        async with inference(ctx, OP):
            raise exc
    assert ctx.health.status == "ok"
    assert counts(ctx) == {outcome: 1}


async def test_failure_degrades_health(ctx: ServiceContext) -> None:
    with pytest.raises(RuntimeError):
        async with inference(ctx, OP):
            raise RuntimeError("gpu wedged")
    assert ctx.health.status == "degraded"
    assert ctx.health.last_error == "RuntimeError: gpu wedged"
    assert counts(ctx) == {"failure": 1}
    assert ctx.queue.depth == 0


async def test_queue_full_is_busy(ctx: ServiceContext) -> None:
    gate = asyncio.Event()

    async def holder() -> None:
        async with inference(ctx, OP):
            await gate.wait()

    task = asyncio.create_task(holder())
    await asyncio.sleep(0)
    with pytest.raises(QueueFull):
        async with inference(ctx, OP):
            pass
    gate.set()
    await task
    assert counts(ctx) == {"busy": 1, "ok": 1}


async def test_nested_inference_raises(ctx: ServiceContext) -> None:
    ctx.queue = InferenceQueue(2)
    with pytest.raises(RuntimeError, match="already holds"):
        async with inference(ctx, OP), inference(ctx, OP):
            pass
    assert ctx.queue.depth == 0


async def test_cancellation_passes_through_and_releases_slot(ctx: ServiceContext) -> None:
    started = asyncio.Event()

    async def job() -> None:
        async with inference(ctx, OP):
            started.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(job())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ctx.queue.depth == 0
    assert ctx.health.status == "ok"
    assert counts(ctx) == {}


async def test_generator_closed_early_is_a_disconnect(ctx: ServiceContext) -> None:
    async def stream() -> AsyncGenerator[int]:
        async with inference(ctx, OP):
            for i in range(10):
                yield i

    gen = stream()
    assert await anext(gen) == 0
    await gen.aclose()  # the consumer stopped early: GeneratorExit
    assert ctx.queue.depth == 0
    assert counts(ctx) == {"disconnect": 1}
    assert ctx.health.last_inference_at is None and ctx.health.last_error is None
    assert ctx.metrics.snapshot()["recent_errors"] == [], "disconnects are counted, not logged"

    exhausted = stream()
    assert [i async for i in exhausted] == list(range(10))
    await exhausted.aclose()  # no-op on a finished generator: still ok
    assert counts(ctx) == {"disconnect": 1, "ok": 1}


async def test_event_driven_session_records_failure_then_cleanup(ctx: ServiceContext) -> None:
    """The ADR 0005 pattern for sessions spanning several events (Wyoming STT).

    The session task owns the inference block and consumes events from a
    queue; the connection handler only feeds events and, on teardown,
    cancels the task. A failure followed by cleanup stays a failure, and an
    idle teardown records nothing (no 30 s "success").
    """
    events: asyncio.Queue[str] = asyncio.Queue()

    async def session() -> None:
        async with inference(ctx, Operation.STREAM):
            while (event := await events.get()) != "stop":
                if event == "boom":
                    raise RuntimeError("decoder failed")

    task = asyncio.create_task(session())
    await events.put("chunk")
    await events.put("boom")
    with pytest.raises(RuntimeError):
        await task
    task.cancel()  # the handler's cleanup runs anyway
    assert ctx.health.status == "degraded"
    stream = ctx.metrics.snapshot()["operations"]["stream"]["counts"]
    assert (stream["failure"], stream["ok"]) == (1, 0)

    idle = asyncio.create_task(session())
    await asyncio.sleep(0)
    idle.cancel()  # client went quiet; handler tears down
    with pytest.raises(asyncio.CancelledError):
        await idle
    stream = ctx.metrics.snapshot()["operations"]["stream"]["counts"]
    assert (stream["failure"], stream["ok"]) == (1, 0)
    assert ctx.queue.depth == 0


def _tts_ctx(settings: Settings, engine: FakeTTSEngine) -> ServiceContext:
    return ServiceContext(
        service="tts",
        settings=settings,
        engine=engine,
        queue=InferenceQueue(1),
        health=HealthState(service="tts", engine="f", model="m", version="0", queue_capacity=1),
    )


@pytest.mark.parametrize(
    ("configured", "engine_langs", "languages"),
    [
        (["en"], ["en"], ["en"]),
        (["en-gb"], ["en"], ["en-gb"]),  # narrower configured tag wins
        (["en"], ["en-US", "en-GB"], ["en-US", "en-GB"]),  # narrower engine tags win
        (["en_GB"], ["en"], ["en-GB"]),
        (["de"], ["en"], []),
    ],
)
async def test_context_languages(
    settings: Settings, configured: list[str], engine_langs: list[str], languages: list[str]
) -> None:
    engine = FakeTTSEngine()
    engine.languages = engine_langs
    await engine.load()
    settings.tts.languages = configured
    c = _tts_ctx(settings, engine)
    assert c.languages == languages
    # A voice is served exactly when a language is advertised.
    assert bool(await c.voices()) == bool(languages)


async def test_check_language(settings: Settings) -> None:
    engine = FakeTTSEngine()
    settings.tts.languages = ["en-gb"]
    c = _tts_ctx(settings, engine)
    assert c.check_language(None) is None
    assert c.check_language("en") == "en"
    assert c.check_language("EN_us") == "en"  # returns the engine's own code
    with pytest.raises(UnsupportedLanguage):
        c.check_language("de")
    with pytest.raises(EngineNotLoaded):
        c.check_loaded()


@pytest.mark.parametrize(
    ("hint", "engine_langs", "configured", "resolved"),
    [
        ("en-GB", ["en-US", "en-GB"], ["en"], "en-GB"),  # exact tag beats same-base
        ("EN_gb", ["en-US", "en-GB"], ["en"], "en-GB"),  # normalized exact match
        ("en", ["en-US", "en-GB"], ["en-gb"], "en-GB"),  # the advertised narrower tag
        ("en-US", ["en-US", "en-GB"], ["en"], "en-US"),
        ("en", ["en"], ["en"], "en"),
    ],
)
def test_check_language_prefers_exact_region(
    settings: Settings,
    hint: str,
    engine_langs: list[str],
    configured: list[str],
    resolved: str,
) -> None:
    engine = FakeTTSEngine()
    engine.languages = engine_langs
    settings.tts.languages = configured
    assert _tts_ctx(settings, engine).check_language(hint) == resolved


def test_languages_setting_may_not_be_empty() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({"tts": {"languages": []}})
