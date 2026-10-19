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

Several callers can share one :class:`ModelThread` (Parakeet's concurrent
stream sessions do): :meth:`ModelThread.run_ranked` lets each call say how
urgent it is, read again whenever the thread picks its next call.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import itertools
import logging
import threading
from collections.abc import Awaitable, Callable
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Generic, ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")

log = logging.getLogger(__name__)

Rank = Callable[[], int]
"""How urgent a queued call is: lower runs first. Read on the model thread,
under its lock, each time the thread picks its next call."""


async def run_blocking(fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    """``await asyncio.to_thread(fn, ...)``, except cancellation waits for ``fn`` to return.

    The caller still sees ``CancelledError``, only later: once the thread is
    done. Whatever ``fn`` returned or raised after the cancel is discarded.
    """
    return await _run(None, functools.partial(fn, *args, **kwargs))


class ModelThread:
    """One dedicated thread for every call into one model.

    ``await thread.run(fn, ...)`` behaves like :func:`run_blocking` but always
    runs ``fn`` on the same thread. Calls run one at a time: whenever the
    thread is free it takes the waiting call of lowest rank, the earliest of
    equal rank. :meth:`run` calls have rank 0; :meth:`run_ranked` calls rank
    themselves, and a ranked call cancelled before it starts never runs.
    """

    def __init__(self, name: str) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=name)
        self._lock = threading.Lock()
        self._waiting: list[_Call[Any]] = []
        self._order = itertools.count()

    async def run(self, fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
        call = self._submit(_first, functools.partial(fn, *args, **kwargs), withdrawable=False)
        return await self._wait(call)

    def run_ranked(self, rank: Rank, fn: Callable[..., T], /, *args: Any) -> Awaitable[T]:
        """Queue ``fn(*args)`` at once, before the first await, so it is in line
        behind whatever is queued now; await the result to get its value."""
        call = self._submit(rank, functools.partial(fn, *args), withdrawable=True)
        return self._wait(call)

    def shutdown(self) -> None:
        """Wait for queued calls, then end the thread (and its MLX stream)."""
        self._executor.shutdown(wait=True)

    def _submit(self, rank: Rank, fn: Callable[[], T], *, withdrawable: bool) -> _Call[T]:
        bound = functools.partial(contextvars.copy_context().run, fn)
        call: _Call[T] = _Call(next(self._order), rank, bound, withdrawable)
        with self._lock:
            self._waiting.append(call)
        try:
            # One turn per call: each turn runs whichever call is first by then.
            self._executor.submit(self._turn)
        except RuntimeError:  # shut down
            self._withdraw(call)
            raise
        return call

    def _turn(self) -> None:
        with self._lock:
            if not self._waiting:
                return  # its call was withdrawn
            call = min(self._waiting, key=_key)
            self._waiting.remove(call)
        if not call.future.set_running_or_notify_cancel():
            return
        try:
            result = call.fn()
        except BaseException as exc:
            call.future.set_exception(exc)
        else:
            call.future.set_result(result)

    def _withdraw(self, call: _Call[Any]) -> bool:
        """Take ``call`` out of line if it has not started; whether it was."""
        with self._lock:
            if call not in self._waiting:
                return False
            self._waiting.remove(call)
        return call.future.cancel()

    async def _wait(self, call: _Call[T]) -> T:
        future = asyncio.wrap_future(call.future)
        cancelled = False
        while not future.done():
            try:
                await asyncio.wait({future})  # never cancels ``future`` itself
            except asyncio.CancelledError:
                if call.withdrawable and self._withdraw(call):
                    raise
                cancelled = True  # keep waiting: the thread cannot be stopped
        if cancelled:
            if not future.cancelled():
                future.exception()  # mark as retrieved; the cancel wins
            raise asyncio.CancelledError
        return future.result()


@dataclass(eq=False, slots=True)
class _Call(Generic[T]):
    order: int
    rank: Rank
    fn: Callable[[], T]
    withdrawable: bool
    future: Future[T] = field(default_factory=Future)


def _first() -> int:
    return 0


def _key(call: _Call[Any]) -> tuple[int, int]:
    try:
        return call.rank(), call.order
    except Exception:
        log.exception("ranking a model-thread call failed; running it first")
        return 0, call.order


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
