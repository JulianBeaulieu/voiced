from __future__ import annotations

import asyncio
import functools
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


async def _occupied(thread: ModelThread) -> tuple[threading.Event, asyncio.Task[None]]:
    """A call that holds ``thread`` until the returned event is set."""
    started, release = threading.Event(), threading.Event()

    def block() -> None:
        started.set()
        release.wait(5)

    task = asyncio.create_task(thread.run(block))
    await asyncio.to_thread(started.wait, 5)
    return release, task


async def test_ranked_calls_run_lowest_rank_first_then_in_order() -> None:
    thread = ModelThread("test-model")
    release, blocker = await _occupied(thread)
    order: list[str] = []
    calls = []
    for name, rank in [("late a", 2), ("behind", 1), ("late b", 2), ("waited", 0)]:
        ranked = thread.run_ranked(functools.partial(int, rank), order.append, name)
        calls.append(asyncio.create_task(ranked))
        await asyncio.sleep(0)  # queued in this order
    release.set()
    await blocker
    await asyncio.gather(*calls)
    assert order == ["waited", "behind", "late a", "late b"]
    thread.shutdown()


async def test_a_rank_is_read_when_the_thread_picks_its_next_call() -> None:
    """A session that finalizes while its call waits moves up at once."""
    thread = ModelThread("test-model")
    release, blocker = await _occupied(thread)
    order: list[str] = []
    rank = {"a": 2, "b": 2}
    first = asyncio.create_task(thread.run_ranked(lambda: rank["a"], order.append, "a"))
    await asyncio.sleep(0)
    second = asyncio.create_task(thread.run_ranked(lambda: rank["b"], order.append, "b"))
    await asyncio.sleep(0)
    rank["b"] = 0
    release.set()
    await asyncio.gather(blocker, first, second)
    assert order == ["b", "a"]
    thread.shutdown()


async def test_a_ranked_call_cancelled_before_it_starts_never_runs() -> None:
    thread = ModelThread("test-model")
    release, blocker = await _occupied(thread)
    ran: list[str] = []
    task = asyncio.create_task(thread.run_ranked(lambda: 0, ran.append, "withdrawn"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task  # at once: it never started
    assert not blocker.done()
    release.set()
    await blocker
    assert await thread.run(lambda: 42) == 42
    assert ran == []
    thread.shutdown()


async def test_a_plain_call_cancelled_before_it_starts_still_runs() -> None:
    """``run`` keeps run_blocking's rule: a cancelled caller waits for its call."""
    thread = ModelThread("test-model")
    release, blocker = await _occupied(thread)
    ran: list[str] = []
    task = asyncio.create_task(thread.run(ran.append, "kept"))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0.02)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ran == ["kept"]
    await blocker
    thread.shutdown()


async def test_a_ranked_call_raises_like_a_plain_one() -> None:
    thread = ModelThread("test-model")

    def fail() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await thread.run_ranked(lambda: 0, fail)
    assert await thread.run_ranked(lambda: 0, lambda: 7) == 7
    thread.shutdown()


async def test_a_call_after_shutdown_is_refused() -> None:
    thread = ModelThread("test-model")
    thread.shutdown()
    with pytest.raises(RuntimeError):
        await thread.run(lambda: 1)
    assert thread._waiting == []


async def test_a_stoppable_call_is_asked_to_stop_once_and_still_waited_for() -> None:
    thread = ModelThread("test-model")
    started = threading.Event()
    stop = threading.Event()
    release = threading.Event()
    asked: list[int] = []

    def call() -> str:
        started.set()
        stop.wait(5)
        release.wait(5)
        return "stopped early"

    def ask() -> None:
        asked.append(1)
        stop.set()

    task = asyncio.create_task(thread.run_stoppable(ask, call))
    await asyncio.to_thread(started.wait, 5)
    task.cancel()
    await asyncio.sleep(0.02)
    task.cancel()  # a second cancel while it still waits
    await asyncio.sleep(0.02)
    assert not task.done(), "the cancel waits for the call"
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert asked == [1]
    assert await thread.run_stoppable(ask, lambda: 42) == 42
    assert asked == [1], "a call that is not cancelled is never asked to stop"
