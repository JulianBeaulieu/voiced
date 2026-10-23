"""Keep a service's model loaded only while it is wanted (ADR 0033).

:class:`Residency` owns every ``load()`` and ``unload()`` of the engine. The
service loads its model at start, as it always has. With ``idle_unload_s``
set, the model is unloaded once no request has used it for that long, and
the next request loads it again before it runs: it waits for the load, it
is never refused. ``idle_unload_s = 0`` (the default) never unloads, and
neither does an engine that keeps the base class's ``unload()``.

Requests reach the engine only inside :func:`voiced.inference.inference`,
which calls :meth:`Residency.ensure_loaded` once it holds the slot and
:meth:`Residency.touch` when it lets go. An unload starts only while the
queue is empty, so it never runs alongside an inference; a request that
arrives during one waits for it to finish and then loads the model again.
One lock orders the loads and unloads. A reload runs as a task of its own
that every waiting request shields: a client that hangs up during it does
not cancel it, so the engine and health never disagree on whether the
model is in memory.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable

from voiced.engines.base import EngineNotLoaded, STTEngine, TTSEngine
from voiced.health import HealthState
from voiced.models import hub_missing_model
from voiced.queue import InferenceQueue

log = logging.getLogger(__name__)


BUSY_RECHECK_S = 5.0
"""How often the timer looks again while requests hold the queue."""


def can_unload(engine: STTEngine | TTSEngine) -> bool:
    """Whether ``engine`` implements ``unload()`` rather than keeping the
    base class's, which raises ``NotImplementedError``."""
    base = STTEngine if isinstance(engine, STTEngine) else TTSEngine
    return type(engine).unload is not base.unload


class Residency:
    """Loads and unloads one service's engine; see the module docstring."""

    def __init__(
        self,
        engine: STTEngine | TTSEngine,
        health: HealthState,
        queue: InferenceQueue,
        idle_unload_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.engine = engine
        self.health = health
        self.queue = queue
        self.idle_unload_s = idle_unload_s
        self._clock = clock
        self._unloads = idle_unload_s > 0 and can_unload(engine)
        """Whether this service ever unloads: a timeout is set and the engine
        implements ``unload()``."""
        if idle_unload_s > 0 and not self._unloads:
            log.warning(
                "%s: engine %s cannot unload its model; %s.idle_unload_s has no effect",
                health.service,
                engine.name,
                health.service,
            )
        self._lock = asyncio.Lock()
        self._loaded = asyncio.Event()
        """Set exactly while ``health.model_state`` is ``loaded``: set beside
        ``mark_loaded`` and cleared beside ``mark_unloaded``. The idle timer
        waits on it."""
        self._last_used = clock()
        self._reload: asyncio.Task[None] | None = None
        self._timer: asyncio.Task[None] | None = None

    async def load(self) -> None:
        """Load the engine and mark it loaded. Raises what ``load()`` raised;
        a model that is not downloaded as :class:`ModelNotDownloaded`, after
        marking health ``missing_model``."""
        try:
            await self.engine.load()
        except Exception as exc:
            missing = hub_missing_model(exc, self.engine.model_id)
            if missing is None:
                raise
            log.error("%s: %s", self.health.service, missing)
            self.health.mark_missing(missing)
            if missing is exc:
                raise
            raise missing from exc
        self.health.mark_loaded()
        self._loaded.set()
        self._last_used = self._clock()
        if self._unloads and self._timer is None:
            self._timer = asyncio.create_task(
                self._unload_when_idle(), name=f"voiced-{self.health.service}-idle-unload"
            )

    async def ensure_loaded(self) -> None:
        """Return once the model is loaded, loading it again if it was
        unloaded. Called with the slot held. Before the first load has
        finished it raises :class:`EngineNotLoaded` (that load is not this
        method's to start); a failed reload raises, and the next request
        tries again."""
        if self.health.model_loaded:
            return
        if not self.health.started:
            raise EngineNotLoaded("model is still loading")
        if self._reload is None:
            self._reload = asyncio.create_task(
                self._load_again(), name=f"voiced-{self.health.service}-reload"
            )
            self._reload.add_done_callback(_retrieve)
        await asyncio.shield(self._reload)

    def touch(self) -> None:
        """A request let go of the slot: the idle time starts again."""
        self._last_used = self._clock()

    async def close(self) -> None:
        """Stop the idle timer (service shutdown)."""
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await timer

    async def _load_again(self) -> None:
        try:
            async with self._lock:  # an unload in progress finishes first
                if self.health.model_loaded:
                    return
                self.health.mark_reloading()
                started = time.perf_counter()
                try:
                    await self.load()
                except BaseException:
                    self.health.mark_unloaded()
                    raise
                log.info(
                    "%s: model loaded again in %.1fs",
                    self.health.service,
                    time.perf_counter() - started,
                )
        finally:
            self._reload = None

    async def _unload_when_idle(self) -> None:
        while True:
            await self._loaded.wait()
            if self.queue.depth:
                # A request holds or awaits the slot; it touches when it ends.
                await asyncio.sleep(min(self.idle_unload_s, BUSY_RECHECK_S))
                continue
            due = self._last_used + self.idle_unload_s - self._clock()
            if due > 0:
                await asyncio.sleep(due)
                continue
            await self._unload()

    async def _unload(self) -> None:
        """Unload if still idle."""
        async with self._lock:
            if not self.health.model_loaded or self.queue.depth:
                return
            # Marked first: a request that arrives now waits on the lock,
            # then loads the model again.
            self.health.mark_unloaded()
            self._loaded.clear()
            try:
                await self.engine.unload()
            except Exception as exc:
                # Whatever it still holds, the next request loads it afresh.
                log.exception("%s: unloading the model failed", self.health.service)
                self.health.record_error(exc)
                return
            log.info(
                "%s: model unloaded after %gs without a request",
                self.health.service,
                self.idle_unload_s,
            )


def _retrieve(task: asyncio.Task[None]) -> None:
    """Mark a reload's failure as seen: every request that waited for it
    has raised it already, or was cancelled."""
    if not task.cancelled():
        task.exception()
