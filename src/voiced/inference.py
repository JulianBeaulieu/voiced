"""The one inference policy every transport goes through.

HTTP routes and Wyoming handlers wrap each engine call in :func:`inference`::

    async with inference(ctx, "transcribe"):
        transcript = await ctx.stt_engine.transcribe(audio, language)

which takes a queue slot, times the block, records the outcome in health and
metrics, and re-raises whatever the block raised. Transports turn exceptions
into wire errors with :func:`classify` instead of their own ``except`` ladder;
see docs/decisions/0005-shared-inference-and-audio-primitives.md.

Recording rules (``voiced.engines.base`` defines what an ``EngineError`` is):

=================  ======================  =====================  ============
outcome            raised                  health                 metrics
=================  ======================  =====================  ============
``ok``             nothing                 ``record_success``     latency
``busy``           ``QueueFull``           untouched              error ring
``client_error``   ``EngineError``         untouched              error ring
``disconnect``     ``ConnectionError``     untouched              count only
``failure``        any other ``Exception`` ``record_error``       error ring
=================  ======================  =====================  ============

``disconnect`` covers ``ConnectionError`` (reset, broken pipe, aborted) and
``asyncio.IncompleteReadError``, not every ``OSError``: an engine that fails
to open a model file must still count as a failure.

Timing covers the whole block while the slot is held, not the wait for the
slot. For streaming (an STT session, a TTS sentence loop) the block is the
whole stream, so the duration is the full session.

Cancellation: ``CancelledError`` (and ``GeneratorExit`` when a streaming
generator is closed early) passes through untouched, records nothing, and
still releases the slot.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol

from voiced.engines.base import EngineError
from voiced.health import HealthState
from voiced.metrics import Metrics, Outcome
from voiced.queue import InferenceQueue, QueueFull

log = logging.getLogger(__name__)

_RECORDED = "_voiced_recorded"


class InferenceContext(Protocol):
    """What :func:`inference` needs. ``voiced.api.context.ServiceContext`` fits."""

    queue: InferenceQueue
    health: HealthState
    metrics: Metrics


def classify(exc: Exception) -> Outcome:
    """Map an exception to the outcome a transport reports on the wire."""
    if isinstance(exc, QueueFull):
        return Outcome.BUSY
    if isinstance(exc, EngineError):
        return Outcome.CLIENT_ERROR
    if isinstance(exc, ConnectionError | asyncio.IncompleteReadError):
        return Outcome.DISCONNECT
    return Outcome.FAILURE


def record_exception(
    ctx: InferenceContext, operation: str, exc: Exception, duration_ms: float = 0.0
) -> Outcome:
    """Record ``exc`` in health and metrics once, and return its outcome.

    Idempotent per exception object: :func:`inference` records what it sees,
    and a transport's last-resort handler can call this again on the same
    exception without counting it twice. Exceptions raised outside an
    :func:`inference` block are recorded the first time this sees them.
    """
    outcome = classify(exc)
    if getattr(exc, _RECORDED, False):
        return outcome
    setattr(exc, _RECORDED, True)
    if outcome is Outcome.FAILURE:
        ctx.health.record_error(exc)
    elif outcome is Outcome.DISCONNECT:
        log.debug("%s: client went away: %r", operation, exc)
    ctx.metrics.record(operation, outcome, duration_ms, f"{type(exc).__name__}: {exc}")
    return outcome


@asynccontextmanager
async def inference(ctx: InferenceContext, operation: str) -> AsyncIterator[None]:
    """Hold ``ctx.queue``'s slot for the block and record how it ended.

    Raises :class:`QueueFull` immediately if the queue is at capacity.
    """
    started = time.perf_counter()
    try:
        async with ctx.queue.slot():
            started = time.perf_counter()
            yield
    except Exception as exc:
        record_exception(ctx, operation, exc, _elapsed_ms(started))
        raise
    duration_ms = _elapsed_ms(started)
    ctx.health.record_success(duration_ms)
    ctx.metrics.record(operation, Outcome.OK, duration_ms)


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000
