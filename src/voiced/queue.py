"""Serialized, bounded inference queue.

MLX runs one kernel stream per process and model objects are not thread-safe,
so each service serializes GPU work: exactly one inference runs at a time and
the rest wait in FIFO order. Waiting is bounded; when ``capacity`` requests are
already queued or running, new ones fail immediately with :class:`QueueFull`,
which the API layer maps to HTTP 503. Callers are never blocked indefinitely.

Two entry points:

* ``async with queue.slot():`` holds the GPU for the duration of the block.
  Use this for streaming (TTS sentence loops, STT stream sessions).
* ``await queue.run(fn, *args)`` runs a blocking callable in a worker thread
  while holding a slot. Use this for one-shot inference.

Cancellation: if the awaiting task is cancelled while waiting, it leaves the
queue without running. Once ``fn`` has started in its thread it runs to
completion (threads cannot be interrupted) and the slot stays held until it
returns (see :func:`voiced.threads.run_blocking`), so a cancelled client never
lets a second inference start on a busy GPU. The cancelled caller sees
``CancelledError`` after the thread finishes.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import ParamSpec, TypeVar

from voiced.threads import run_blocking

P = ParamSpec("P")
T = TypeVar("T")


class QueueFull(Exception):
    """Raised when the queue has ``capacity`` requests pending or running."""

    def __init__(self, capacity: int) -> None:
        super().__init__(f"inference queue full ({capacity} pending)")
        self.capacity = capacity


class InferenceQueue:
    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.capacity = capacity
        self._lock = asyncio.Lock()
        self._pending = 0

    @property
    def depth(self) -> int:
        """Requests currently waiting or running."""
        return self._pending

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        if self._pending >= self.capacity:
            raise QueueFull(self.capacity)
        self._pending += 1
        try:
            async with self._lock:
                yield
        finally:
            self._pending -= 1

    async def run(self, fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
        async with self.slot():
            return await run_blocking(fn, *args, **kwargs)
