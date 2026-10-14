from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from voiced.api.app import Ctx
from voiced.queue import InferenceQueue, QueueFull


async def test_serializes_and_bounds() -> None:
    q = InferenceQueue(capacity=2)
    running = 0
    peak = 0

    async def job() -> None:
        nonlocal running, peak
        async with q.slot():
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.02)
            running -= 1

    first = asyncio.create_task(job())
    second = asyncio.create_task(job())
    await asyncio.sleep(0.005)
    assert q.depth == 2
    with pytest.raises(QueueFull):
        async with q.slot():
            pass
    await asyncio.gather(first, second)
    assert peak == 1
    assert q.depth == 0


async def test_run_in_thread() -> None:
    q = InferenceQueue(capacity=1)
    assert await q.run(lambda a, b: a + b, 2, 3) == 5
    assert q.depth == 0


async def test_cancel_while_waiting_releases_slot() -> None:
    q = InferenceQueue(capacity=2)
    gate = asyncio.Event()

    async def holder() -> None:
        async with q.slot():
            await gate.wait()

    async def waiter() -> None:
        async with q.slot():
            pass

    h = asyncio.create_task(holder())
    await asyncio.sleep(0)
    w = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    assert q.depth == 2
    w.cancel()
    with pytest.raises(asyncio.CancelledError):
        await w
    assert q.depth == 1
    gate.set()
    await h
    assert q.depth == 0


async def test_overflow_maps_to_503(stt_app: FastAPI) -> None:
    gate = asyncio.Event()

    async def slow(ctx: Ctx) -> dict[str, bool]:
        async with ctx.queue.slot():
            await gate.wait()
            return {"ok": True}

    stt_app.add_api_route("/slow", slow, methods=["GET"])
    async with stt_app.router.lifespan_context(stt_app):
        async with AsyncClient(transport=ASGITransport(app=stt_app), base_url="http://t") as c:
            t1 = asyncio.create_task(c.get("/slow"))
            t2 = asyncio.create_task(c.get("/slow"))
            await asyncio.sleep(0.02)
            third = await c.get("/slow")  # capacity is 2 in the test settings
            assert third.status_code == 503
            assert third.headers["retry-after"] == "1"
            assert third.json()["error"]["type"] == "server_busy"
            health = await c.get("/health")
            assert health.json()["queue_depth"] == 2
            gate.set()
            r1, r2 = await asyncio.gather(t1, t2)
            assert r1.status_code == 200 and r2.status_code == 200
