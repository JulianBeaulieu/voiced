from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest

from voiced.engines.base import UnknownVoice
from voiced.health import HealthState
from voiced.inference import classify, inference, record_exception
from voiced.metrics import Metrics, Outcome
from voiced.queue import InferenceQueue, QueueFull


@dataclass
class Ctx:
    queue: InferenceQueue = field(default_factory=lambda: InferenceQueue(1))
    health: HealthState = field(
        default_factory=lambda: HealthState(
            service="stt", engine="fake", model="m", version="0", queue_capacity=1
        )
    )
    metrics: Metrics = field(default_factory=Metrics)

    def counts(self, op: str = "op") -> dict[str, int]:
        counts: dict[str, int] = self.metrics.snapshot()["operations"][op]["counts"]
        return {k: v for k, v in counts.items() if v}


@pytest.fixture
def ctx() -> Ctx:
    c = Ctx()
    c.health.mark_loaded()
    return c


def test_classify() -> None:
    assert classify(QueueFull(1)) is Outcome.BUSY
    assert classify(UnknownVoice("x")) is Outcome.CLIENT_ERROR
    assert classify(ConnectionResetError()) is Outcome.DISCONNECT
    assert classify(asyncio.IncompleteReadError(b"", 4)) is Outcome.DISCONNECT
    assert classify(FileNotFoundError("model.bin")) is Outcome.FAILURE
    assert classify(RuntimeError()) is Outcome.FAILURE


async def test_success_records_health_and_latency(ctx: Ctx) -> None:
    async with inference(ctx, "op"):
        assert ctx.queue.depth == 1
        await asyncio.sleep(0.01)
    assert ctx.queue.depth == 0
    assert ctx.health.status == "ok"
    assert ctx.health.last_inference_ms is not None and ctx.health.last_inference_ms >= 10
    assert ctx.counts() == {"ok": 1}


async def test_client_error_leaves_health_alone(ctx: Ctx) -> None:
    with pytest.raises(UnknownVoice):
        async with inference(ctx, "op"):
            raise UnknownVoice("nope")
    assert ctx.health.status == "ok"
    assert ctx.counts() == {"client_error": 1}


async def test_disconnect_leaves_health_alone(ctx: Ctx) -> None:
    with pytest.raises(BrokenPipeError):
        async with inference(ctx, "op"):
            raise BrokenPipeError
    assert ctx.health.status == "ok"
    assert ctx.counts() == {"disconnect": 1}
    assert ctx.metrics.snapshot()["recent_errors"] == []


async def test_failure_degrades_health(ctx: Ctx) -> None:
    with pytest.raises(RuntimeError):
        async with inference(ctx, "op"):
            raise RuntimeError("gpu wedged")
    assert ctx.health.status == "degraded"
    assert ctx.health.last_error == "RuntimeError: gpu wedged"
    assert ctx.counts() == {"failure": 1}
    assert ctx.queue.depth == 0


async def test_queue_full_is_busy(ctx: Ctx) -> None:
    async with inference(ctx, "op"):
        with pytest.raises(QueueFull):
            async with inference(ctx, "op"):
                pass
    assert ctx.counts() == {"busy": 1, "ok": 1}


async def test_cancellation_passes_through_and_releases_slot(ctx: Ctx) -> None:
    started = asyncio.Event()

    async def job() -> None:
        async with inference(ctx, "op"):
            started.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(job())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ctx.queue.depth == 0
    assert ctx.health.status == "ok"
    assert ctx.metrics.snapshot()["operations"] == {}


async def test_generator_closed_early_records_nothing(ctx: Ctx) -> None:
    async def stream() -> AsyncIterator[int]:
        async with inference(ctx, "op"):
            for i in range(10):
                yield i

    gen = stream()
    assert await anext(gen) == 0
    await gen.aclose()  # type: ignore[attr-defined]
    assert ctx.queue.depth == 0
    assert ctx.metrics.snapshot()["operations"] == {}


async def test_record_exception_is_idempotent(ctx: Ctx) -> None:
    exc = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        async with inference(ctx, "op"):
            raise exc
    # A transport's last-resort handler sees the same exception again.
    assert record_exception(ctx, "/v1/x", exc) is Outcome.FAILURE
    assert ctx.counts() == {"failure": 1}
    assert "/v1/x" not in ctx.metrics.snapshot()["operations"]
