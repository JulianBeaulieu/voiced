from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from voiced.api.context import Ctx
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


async def test_nested_slot_raises_instead_of_deadlocking() -> None:
    q = InferenceQueue(capacity=2)
    async with q.slot():
        with pytest.raises(RuntimeError, match="already holds"):
            async with q.slot():
                pass
        assert q.depth == 1
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


class _Holder:
    """A task holding a slot of ``queue`` until :meth:`release`."""

    def __init__(self, queue: InferenceQueue, *, shared: bool) -> None:
        self.entered = asyncio.Event()
        self._gate = asyncio.Event()
        self.task = asyncio.create_task(self._hold(queue, shared))

    async def _hold(self, queue: InferenceQueue, shared: bool) -> None:
        async with queue.slot(shared=shared):
            self.entered.set()
            await self._gate.wait()

    async def release(self) -> None:
        self._gate.set()
        await self.task


async def test_shared_slots_run_together_up_to_their_number() -> None:
    q = InferenceQueue(capacity=4, shared=2)
    a, b, c = (_Holder(q, shared=True) for _ in range(3))
    await asyncio.sleep(0)
    assert a.entered.is_set() and b.entered.is_set()
    assert not c.entered.is_set(), "beyond the shared slots a stream waits"
    assert q.depth == 3
    await a.release()
    await asyncio.sleep(0)
    assert c.entered.is_set()
    await b.release()
    await c.release()
    assert q.depth == 0


async def test_an_exclusive_slot_waits_for_the_shared_ones_and_holds_back_later_ones() -> None:
    q = InferenceQueue(capacity=4, shared=3)
    stream = _Holder(q, shared=True)
    batch = _Holder(q, shared=False)
    later = _Holder(q, shared=True)
    await asyncio.sleep(0)
    assert stream.entered.is_set()
    assert not batch.entered.is_set()
    assert not later.entered.is_set(), "admission is FIFO: no overtaking the batch"
    await stream.release()
    await asyncio.sleep(0)
    assert batch.entered.is_set() and not later.entered.is_set()
    await batch.release()
    await asyncio.sleep(0)
    assert later.entered.is_set()
    await later.release()


async def test_shared_slots_still_bound_the_queue() -> None:
    q = InferenceQueue(capacity=2, shared=2)
    holders = [_Holder(q, shared=True) for _ in range(2)]
    await asyncio.sleep(0)
    with pytest.raises(QueueFull):
        async with q.slot(shared=True):
            pass
    for holder in holders:
        await holder.release()


async def test_cancelling_the_head_of_the_line_lets_the_next_in() -> None:
    q = InferenceQueue(capacity=4, shared=2)
    stream = _Holder(q, shared=True)
    batch = _Holder(q, shared=False)
    later = _Holder(q, shared=True)
    await asyncio.sleep(0)
    assert not later.entered.is_set()
    batch.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await batch.task
    await asyncio.sleep(0)
    assert later.entered.is_set()
    assert q.depth == 2
    await stream.release()
    await later.release()
    assert q.depth == 0


async def test_cancelled_as_it_is_admitted_gives_the_slot_back() -> None:
    q = InferenceQueue(capacity=3, shared=1)
    first = _Holder(q, shared=True)
    second = _Holder(q, shared=True)
    third = _Holder(q, shared=True)
    await asyncio.sleep(0)
    first._gate.set()
    await first.task  # admits second, which has not resumed yet
    second.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second.task
    await asyncio.sleep(0)
    assert third.entered.is_set()
    await third.release()
    assert q.depth == 0


async def test_a_shared_holder_cannot_take_a_second_slot() -> None:
    q = InferenceQueue(capacity=3, shared=2)
    async with q.slot(shared=True):
        with pytest.raises(RuntimeError, match="already holds"):
            async with q.slot(shared=True):
                pass


def test_shared_must_fit_the_capacity() -> None:
    with pytest.raises(ValueError):
        InferenceQueue(capacity=2, shared=3)
    with pytest.raises(ValueError):
        InferenceQueue(capacity=2, shared=0)


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
