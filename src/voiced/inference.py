"""The one inference policy every transport goes through.

HTTP routes and Wyoming handlers wrap each engine call in :func:`inference`::

    async with inference(ctx, Operation.TRANSCRIBE):
        transcript = await ctx.stt_engine.transcribe(audio, language)

which takes the queue slot, times the block, records the outcome in health
and metrics, and re-raises whatever the block raised. It is the only writer
of metrics. Transports turn exceptions into wire errors with one
``except Exception`` and :func:`classify`, never their own ``except`` ladder.
The outcome table, the timing rule and the transport rules are in
docs/decisions/0005-shared-inference-and-audio-primitives.md.

Inside an :func:`inference` block, raise only :class:`EngineError`
subclasses for client mistakes: any other exception, including a transport's
own request errors, is classified as a server ``failure``.

Cancellation: ``CancelledError`` (and ``GeneratorExit`` when a streaming
generator is closed early) passes through untouched, records nothing, and
still releases the slot.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from typing import TypeVar

from starlette.requests import ClientDisconnect
from starlette.websockets import WebSocketDisconnect
from uvicorn.protocols.utils import ClientDisconnected

from voiced.context import ServiceContext
from voiced.engines.base import EngineError, EngineNotLoaded
from voiced.metrics import Operation, Outcome
from voiced.queue import QueueFull

T = TypeVar("T")

log = logging.getLogger(__name__)


class ClientTimeout(ConnectionError):
    """The peer did not send or accept data within the bound (see :func:`bounded`)."""


DISCONNECT_ERRORS: tuple[type[Exception], ...] = (
    # ConnectionError covers ClientTimeout, resets, aborts and BrokenPipeError.
    # A BrokenPipeError from an engine's own subprocess is therefore also
    # counted as a disconnect; engines should wrap those in their own error.
    ConnectionError,
    asyncio.IncompleteReadError,
    WebSocketDisconnect,
    ClientDisconnect,
    ClientDisconnected,
)


def classify(exc: Exception) -> Outcome:
    """Map an exception to the outcome a transport reports on the wire."""
    if isinstance(exc, QueueFull | EngineNotLoaded):
        return Outcome.BUSY
    if isinstance(exc, EngineError):
        return Outcome.CLIENT_ERROR
    if isinstance(exc, DISCONNECT_ERRORS):
        return Outcome.DISCONNECT
    return Outcome.FAILURE


async def bounded(aw: Awaitable[T], timeout_s: float) -> T:
    """Await a socket read or write; raise :class:`ClientTimeout` after ``timeout_s``.

    Use it for every socket await made while holding a slot, with
    ``server.session_idle_s`` for reads and ``server.write_timeout_s`` for
    writes, so a half-open peer cannot pin the GPU. A ``TimeoutError`` that
    ``aw`` raises on its own is re-raised unchanged.
    """
    timeout = asyncio.timeout(timeout_s)
    try:
        async with timeout:
            return await aw
    except TimeoutError:
        if not timeout.expired():
            raise  # raised by ``aw`` itself: not ours to reinterpret
        raise ClientTimeout(f"no progress from the client in {timeout_s:g} s") from None


@asynccontextmanager
async def inference(ctx: ServiceContext, operation: Operation) -> AsyncIterator[None]:
    """Hold ``ctx.queue``'s slot for the block and record how it ended.

    Raises :class:`QueueFull` immediately if the queue is at capacity.
    """
    started = time.perf_counter()
    try:
        async with ctx.queue.slot():
            started = time.perf_counter()
            yield
    except Exception as exc:
        outcome = classify(exc)
        detail = f"{type(exc).__name__}: {exc}"
        if outcome is Outcome.FAILURE:
            ctx.health.record_error(exc)
            # The one place every transport's failures reach the log; /health
            # and the status page may withhold the message (public_health).
            log.error("%s failed: %s", operation, detail, exc_info=exc)
        elif outcome is Outcome.DISCONNECT:
            log.debug("%s: client went away: %r", operation, exc)
        ctx.metrics.record(operation, outcome, _elapsed_ms(started), detail)
        raise
    duration_ms = _elapsed_ms(started)
    ctx.health.record_success(duration_ms)
    ctx.metrics.record(operation, Outcome.OK, duration_ms)


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000
