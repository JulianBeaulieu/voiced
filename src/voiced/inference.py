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
still releases the slot. A block that a client's hang-up should end at its
next await, and count as a disconnect, is given a :class:`HangUp`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager, nullcontext
from types import TracebackType
from typing import TypeVar

from starlette.requests import ClientDisconnect
from starlette.websockets import WebSocketDisconnect
from uvicorn.protocols.utils import ClientDisconnected

from voiced.config import Settings
from voiced.context import ServiceContext
from voiced.engines.base import STT_SAMPLE_RATE, EngineError, EngineNotLoaded
from voiced.metrics import Operation, Outcome
from voiced.models import ModelNotDownloaded
from voiced.queue import QueueFull

T = TypeVar("T")

log = logging.getLogger(__name__)


class ClientTimeout(ConnectionError):
    """The peer did not send or accept data within the bound (see :func:`bounded`)."""


class ClientHungUp(ConnectionError):
    """The client went away while a :class:`HangUp` block was running.

    voiced's own, transport-neutral disconnect (Wyoming must not depend on
    Starlette's ``ClientDisconnect``), raised only by :class:`HangUp`."""


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
    if isinstance(exc, QueueFull | EngineNotLoaded | ModelNotDownloaded):
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


class HangUp:
    """Ends the ``inference()`` block it is given to, the wait for the slot
    included, at the block's next await once the client is gone::

        hang_up = HangUp()
        async with inference(ctx, Operation.SPEECH, hang_up):
            ...

    Whoever notices that the client went away, usually another task, calls
    :meth:`set`. The block's current await is cancelled and
    :class:`ClientHungUp` raised in its place, which ``inference()`` counts as
    a disconnect, where cancelling the task would record nothing. An engine's
    ``run_blocking`` or ``ModelThread`` call still finishes first, so the slot
    is free after the engine's current step. :meth:`set` before the block
    raises :class:`ClientHungUp` on entry, so no engine call starts; after
    the block it does nothing. One block per instance.

    ``asyncio.timeout`` does the cancelling: it already turns its own cancel
    into an exception and tells it apart from any other cancel of the task.
    """

    def __init__(self) -> None:
        self._over = False
        """Set, or its block has ended: nothing left for :meth:`set` to do."""
        self._timeout: asyncio.Timeout | None = None

    def set(self) -> None:
        """The client is gone."""
        if self._over:
            return
        self._over = True
        if self._timeout is not None:
            self._timeout.reschedule(asyncio.get_running_loop().time())

    async def __aenter__(self) -> HangUp:
        assert self._timeout is None, "a HangUp guards one block"
        if self._over:
            raise ClientHungUp("client went away")
        self._timeout = asyncio.timeout(None)
        await self._timeout.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self._timeout is not None
        self._over = True
        try:
            await self._timeout.__aexit__(exc_type, exc, tb)
        except TimeoutError:
            if not self._timeout.expired():
                raise  # raised by the block itself
            raise ClientHungUp("client went away") from None


@asynccontextmanager
async def inference(
    ctx: ServiceContext, operation: Operation, hang_up: HangUp | None = None
) -> AsyncIterator[None]:
    """Hold ``ctx.queue``'s slot for the block and record how it ended.

    A stream session shares the slot with the queue's other stream sessions
    (``stt.streams``, ADR 0020); anything else holds it alone. Raises
    :class:`QueueFull` immediately if the queue is at capacity. With
    ``hang_up``, the block, or the wait for the slot, ends as a disconnect
    once ``hang_up`` is set (:class:`HangUp`). A model unloaded while idle
    is loaded again once the slot is held, before the block runs.
    """
    started = time.perf_counter()
    try:
        async with (
            hang_up if hang_up is not None else nullcontext(),
            ctx.queue.slot(shared=operation is Operation.STREAM),
        ):
            try:
                # A model unloaded while idle loads again here, before the
                # timing starts (ADR 0033).
                await ctx.residency.ensure_loaded()
                started = time.perf_counter()
                yield
            finally:
                ctx.residency.touch()
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


def utterance_limit_s(settings: Settings) -> float:
    """Longest audio one streamed utterance may carry, in seconds:
    ``stt.max_upload_mb`` read as 16-bit 16 kHz samples. The one place tests
    shorten the bound."""
    return settings.stt.max_audio_s


class UtteranceBudget:
    """The bound on one streamed utterance, the same on every transport (a
    Wyoming utterance, a WebSocket session): at most :func:`utterance_limit_s`
    of audio, and open at most that long plus ``server.session_idle_s``, so a
    client trickling audio cannot hold the slot forever. Both are client
    errors::

        async with UtteranceBudget(settings) as budget:
            ...
            budget.add(len(samples))
    """

    def __init__(self, settings: Settings) -> None:
        self.limit_s = utterance_limit_s(settings)
        self._max_samples = int(self.limit_s * STT_SAMPLE_RATE)
        self._samples = 0
        self.open_s = self.limit_s + settings.server.session_idle_s
        self._deadline = asyncio.timeout(self.open_s)

    def add(self, samples: int) -> None:
        """Count ``samples`` (at 16 kHz); over the limit is an :class:`EngineError`."""
        self._samples += samples
        if self._samples > self._max_samples:
            raise EngineError(f"more than {self.limit_s:g} s of audio")

    async def __aenter__(self) -> UtteranceBudget:
        await self._deadline.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            await self._deadline.__aexit__(exc_type, exc, tb)
        except TimeoutError:
            if not self._deadline.expired():
                raise
            raise EngineError(f"session open longer than {self.open_s:g} s") from None
