"""Run blocking model calls without letting cancellation outrun the GPU.

``asyncio.to_thread`` returns control as soon as the awaiting task is
cancelled, but the thread keeps running. Inside ``queue.slot()`` that releases
the slot while the model is still busy, and the next request starts a second,
concurrent MLX call. :func:`run_blocking` waits for the thread to finish
before letting ``CancelledError`` propagate.

Engines must use it for every blocking model call (instead of
``asyncio.to_thread``) so a slot is never released while GPU work is in flight.

Engines whose state holds MLX arrays across calls run every model call,
``load()`` included, through their own :class:`ModelThread`. MLX keeps a GPU
stream per thread, and arrays created on one thread (entering a streaming
context, for one) cannot be evaluated on another: "There is no Stream(gpu,
N) in current thread". The default executor is a pool shared by the whole
process, so once anything else grows it, consecutive calls for one model land
on different threads. Parakeet streaming needs this; Kokoro forces evaluation
in every call and was probed safe across threads (ADR 0008).
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
from collections.abc import Callable
from concurrent.futures import Executor, ThreadPoolExecutor
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")


async def run_blocking(fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    """``await asyncio.to_thread(fn, ...)``, except cancellation waits for ``fn`` to return.

    The caller still sees ``CancelledError``, only later: once the thread is
    done. Whatever ``fn`` returned or raised after the cancel is discarded.
    """
    return await _run(None, functools.partial(fn, *args, **kwargs))


class ModelThread:
    """One dedicated thread for every call into one model.

    ``await thread.run(fn, ...)`` behaves like :func:`run_blocking` but always
    runs ``fn`` on the same thread. Calls queue in order behind each other.
    """

    def __init__(self, name: str) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=name)

    async def run(self, fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
        return await _run(self._executor, functools.partial(fn, *args, **kwargs))

    def shutdown(self) -> None:
        """Wait for queued calls, then end the thread (and its MLX stream)."""
        self._executor.shutdown(wait=True)


async def _run(executor: Executor | None, call: Callable[[], T]) -> T:
    context = contextvars.copy_context()
    future = asyncio.get_running_loop().run_in_executor(executor, context.run, call)
    cancelled = False
    while not future.done():
        try:
            await asyncio.wait({future})  # never cancels ``future`` itself
        except asyncio.CancelledError:
            cancelled = True  # keep waiting: the thread cannot be stopped
    if cancelled:
        if not future.cancelled():
            future.exception()  # mark as retrieved; the cancel wins
        raise asyncio.CancelledError
    return future.result()
