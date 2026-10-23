"""Keep a service's model loaded only while it is wanted (ADR 0033).

:class:`Residency` owns every ``load()`` and ``unload()`` of the engine. The
service loads its model at start, as it always has. With ``idle_unload_s``
set, the model is unloaded once no request has used it for that long, and
the next request loads it again before it runs: it waits for the load, it
is never refused. ``idle_unload_s = 0`` (the default) never unloads.

Requests reach the engine only inside :func:`voiced.inference.inference`,
which calls :meth:`Residency.ensure_loaded` once it holds the slot and
:meth:`Residency.touch` when it lets go. An unload starts only while the
queue is empty, so it never runs alongside an inference; a request that
arrives during one waits for it to finish and then loads the model again.
One lock orders the loads and unloads.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable

from voiced.engines.base import EngineNotLoaded, STTEngine, TTSEngine, Voice
from voiced.health import HealthState
from voiced.models import hub_missing_model
from voiced.queue import InferenceQueue

log = logging.getLogger(__name__)


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
        self._lock = asyncio.Lock()
        self._loaded = asyncio.Event()
        self._last_used = clock()
        self._timer: asyncio.Task[None] | None = None
        self.voices: list[Voice] = []
        """A TTS engine's voices as of its first load, so they can be listed
        while the model is unloaded (``voices()`` is stable after a load)."""

    async def load(self) -> None:
        """Load the engine and mark it loaded. Raises what ``load()`` raised;
        a model that is not downloaded as :class:`ModelNotDownloaded`, after
        marking health ``missing_model``."""
        started = time.perf_counter()
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
        if isinstance(self.engine, TTSEngine) and not self.voices:
            self.voices = await self.engine.voices()
        first = not self.health.started
        self.health.mark_loaded()
        self._loaded.set()
        self._last_used = self._clock()
        if not first:
            log.info(
                "%s: model loaded again in %.1fs",
                self.health.service,
                time.perf_counter() - started,
            )
        if self.idle_unload_s > 0 and self._timer is None:
            self._timer = asyncio.create_task(
                self._unload_when_idle(), name=f"voiced-{self.health.service}-idle-unload"
            )

    async def ensure_loaded(self) -> None:
        """Return once the model is loaded, loading it again if it was
        unloaded. Called with the slot held. Before the first load has
        finished it raises :class:`EngineNotLoaded`, as ``check_loaded``
        does; a failed load raises, and the next request tries again."""
        if self._in_memory():
            return
        if not self.health.started:
            raise EngineNotLoaded("model is still loading")
        async with self._lock:
            if self._in_memory():  # loaded by the request ahead of this one
                return
            self.health.reloading = True
            try:
                await self.load()
            finally:
                self.health.reloading = False

    def _in_memory(self) -> bool:
        return self.health.model_loaded

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

    async def _unload_when_idle(self) -> None:
        while True:
            await self._loaded.wait()
            if self.queue.depth:
                # A request holds or awaits the slot; it touches when it ends.
                await asyncio.sleep(self.idle_unload_s)
                continue
            due = self._last_used + self.idle_unload_s - self._clock()
            if due > 0:
                await asyncio.sleep(due)
                continue
            if not await self._unload():
                return

    async def _unload(self) -> bool:
        """Unload if still idle; False if the engine cannot unload."""
        async with self._lock:
            if not self.health.model_loaded or self.queue.depth:
                return True
            # Marked first: a request that arrives now waits on the lock,
            # then loads the model again.
            self.health.mark_unloaded()
            self._loaded.clear()
            try:
                await self.engine.unload()
            except NotImplementedError:
                self.health.model_loaded, self.health.unloaded = True, False
                log.warning(
                    "%s: engine %s cannot unload its model; %s.idle_unload_s has no effect",
                    self.health.service,
                    self.engine.name,
                    self.health.service,
                )
                return False
            except Exception as exc:
                # Whatever it still holds, the next request loads it afresh.
                log.exception("%s: unloading the model failed", self.health.service)
                self.health.record_error(exc)
            log.info(
                "%s: model unloaded after %gs without a request",
                self.health.service,
                self.idle_unload_s,
            )
            return True
