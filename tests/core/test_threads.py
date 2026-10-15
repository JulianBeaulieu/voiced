from __future__ import annotations

import asyncio
import threading

import pytest

from voiced.queue import InferenceQueue
from voiced.threads import ModelThread, run_blocking


async def test_returns_and_raises_like_to_thread() -> None:
    assert await run_blocking(lambda a, b: a * b, 6, 7) == 42
    with pytest.raises(ZeroDivisionError):
        await run_blocking(lambda: 1 / 0)


async def test_cancel_holds_slot_until_thread_ends() -> None:
    q = InferenceQueue(capacity=2)
    release = threading.Event()
    started = threading.Event()
    lock = threading.Lock()
    active = peak = 0

    def model_call() -> None:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        started.set()
        release.wait(5)
        with lock:
            active -= 1

    async def request() -> None:
        async with q.slot():
            await run_blocking(model_call)

    first = asyncio.create_task(request())
    await asyncio.to_thread(started.wait, 5)
    first.cancel()
    await asyncio.sleep(0.02)
    # The caller was cancelled but the thread still runs: the slot is held,
    # so a second request waits instead of starting a concurrent call.
    assert not first.done()
    second = asyncio.create_task(request())
    await asyncio.sleep(0.02)
    assert q.depth == 2
    assert active == 1

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert peak == 1
    assert q.depth == 0


async def test_failure_after_cancel_still_cancels() -> None:
    started = threading.Event()
    release = threading.Event()

    def boom() -> None:
        started.set()
        release.wait(5)
        raise RuntimeError("late")

    task = asyncio.create_task(run_blocking(boom))
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0.01)
    task.cancel()  # cancelling twice must not detach the thread either
    await asyncio.sleep(0.01)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_model_thread_runs_every_call_on_one_thread() -> None:
    """MLX arrays belong to the thread that created them; the default pool may
    hand consecutive calls to different threads once something else is busy."""
    thread = ModelThread("test-model")
    busy = threading.Event()
    hold = asyncio.create_task(run_blocking(busy.wait, 5))  # occupies a default-pool thread
    seen = {await thread.run(threading.get_ident) for _ in range(20)}
    busy.set()
    await hold
    assert len(seen) == 1
    assert seen != {threading.get_ident()}


async def test_model_thread_cancel_waits_for_the_call() -> None:
    thread = ModelThread("test-model")
    started = threading.Event()
    release = threading.Event()

    def call() -> None:
        started.set()
        release.wait(5)

    task = asyncio.create_task(thread.run(call))
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await thread.run(lambda: 42) == 42


async def test_model_thread_shutdown_ends_the_thread() -> None:
    thread = ModelThread("shutdown-test")
    name = await thread.run(lambda: threading.current_thread().name)
    thread.shutdown()
    assert not any(t.name == name for t in threading.enumerate())
