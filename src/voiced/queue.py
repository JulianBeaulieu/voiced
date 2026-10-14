"""Serialized, bounded inference queue.

MLX runs one kernel stream per process and model objects are not thread-safe,
so each service serializes GPU work: exactly one inference runs at a time and
the rest wait in FIFO order. Waiting is bounded; when ``capacity`` requests are
already queued or running, new ones fail immediately with :class:`QueueFull`,
which the API layer maps to HTTP 503. Callers are never blocked indefinitely.

``async with queue.slot():`` holds the GPU for the duration of the block. It
is the queue's only interface; transports reach it through
:func:`voiced.inference.inference`, which also records the outcome. Blocking
model calls inside the block go through :func:`voiced.threads.run_blocking`,
so a cancelled caller keeps the slot until its thread has finished and a
second inference never starts on a busy GPU.

Cancellation: if the awaiting task is cancelled while waiting, it leaves the
queue without running.

Re-entering ``slot()`` from the task that already holds it would wait on
itself forever, so it raises ``RuntimeError`` instead. A task spawned inside
a slot is a different task and is not caught by that check: never take a
slot from a child task while its parent holds one, or both wait forever.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


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
        self._holder: asyncio.Task[object] | None = None

    @property
    def depth(self) -> int:
        """Requests currently waiting or running."""
        return self._pending

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        task = asyncio.current_task()
        if task is not None and task is self._holder:
            raise RuntimeError("this task already holds the inference slot")
        if self._pending >= self.capacity:
            raise QueueFull(self.capacity)
        self._pending += 1
        try:
            async with self._lock:
                self._holder = task
                try:
                    yield
                finally:
                    self._holder = None
        finally:
            self._pending -= 1
