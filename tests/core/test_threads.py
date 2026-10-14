from __future__ import annotations

import asyncio
import threading

import pytest

from voiced.queue import InferenceQueue
from voiced.threads import run_blocking


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

    first = asyncio.create_task(q.run(model_call))
    await asyncio.to_thread(started.wait, 5)
    first.cancel()
    await asyncio.sleep(0.02)
    # The caller was cancelled but the thread still runs: the slot is held,
    # so a second request waits instead of starting a concurrent call.
    assert not first.done()
    second = asyncio.create_task(q.run(model_call))
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
