"""Serialized, bounded inference queue.

Model objects are not thread-safe and share one GPU, so each service
serializes GPU work (and MLX engines run every call for one model on one
thread, see :mod:`voiced.threads`). A slot is exclusive (one holder at a
time) or shared (up to ``max_shared`` holders at once, for an engine that
interleaves their work itself); the rest wait in FIFO order. Waiting is
bounded; when ``capacity`` requests are already queued or running, new ones
fail immediately with :class:`QueueFull`, which the API layer maps to HTTP
503. Callers are never blocked indefinitely.

``async with queue.slot():`` holds the GPU for the duration of the block. It
is the queue's only interface; transports reach it through
:func:`voiced.inference.inference`, which also records the outcome. Blocking
model calls inside the block go through :func:`voiced.threads.run_blocking`,
so a cancelled caller keeps the slot until its thread has finished and a
second inference never starts on a busy GPU.

``slot(shared=True)`` is for an engine that interleaves several holders'
work itself (Parakeet's stream sessions, ADR 0020): up to ``max_shared``
such holders run at once; an exclusive holder waits until none is left, and
they wait for it. Admission stays FIFO, so an exclusive request at the head
of the line is not overtaken by shared ones behind it.

Cancellation: if the awaiting task is cancelled while waiting, it leaves the
queue without running.

Re-entering ``slot()`` from the task that already holds it would wait on
itself forever, so it raises ``RuntimeError`` instead. A task spawned inside
a slot is a different task and is not caught by that check: never take a
slot from a child task while its parent holds one, or both wait forever.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager


class QueueFull(Exception):
    """Raised when the queue has ``capacity`` requests pending or running."""

    def __init__(self, capacity: int) -> None:
        super().__init__(f"inference queue full ({capacity} pending)")
        self.capacity = capacity


class InferenceQueue:
    def __init__(self, capacity: int, max_shared: int = 1) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        if not 1 <= max_shared <= capacity:
            raise ValueError("max_shared must be between 1 and capacity")
        self.capacity = capacity
        self.max_shared = max_shared
        self._pending = 0
        self._line: deque[tuple[bool, asyncio.Future[None]]] = deque()
        self._running_shared = 0
        self._running_exclusive = False
        self._holders: set[asyncio.Task[object]] = set()

    @property
    def depth(self) -> int:
        """Requests currently waiting or running."""
        return self._pending

    @asynccontextmanager
    async def slot(self, shared: bool = False) -> AsyncIterator[None]:
        task = asyncio.current_task()
        if task is not None and task in self._holders:
            raise RuntimeError("this task already holds the inference slot")
        if self._pending >= self.capacity:
            raise QueueFull(self.capacity)
        self._pending += 1
        try:
            await self._enter(shared)
            if task is not None:
                self._holders.add(task)
            try:
                yield
            finally:
                self._holders.discard(task)
                self._leave(shared)
        finally:
            self._pending -= 1

    async def _enter(self, shared: bool) -> None:
        turn = asyncio.get_running_loop().create_future()
        entry = (shared, turn)
        self._line.append(entry)
        self._admit()
        try:
            await turn
        except asyncio.CancelledError:
            if turn.done() and not turn.cancelled():
                self._leave(shared)  # admitted as the cancel arrived
            else:
                self._line.remove(entry)
                self._admit()  # it may have been holding up the next in line
            raise

    def _admit(self) -> None:
        """Let in, in order, every request at the head of the line that fits."""
        while self._line:
            shared, turn = self._line[0]
            if self._running_exclusive:
                return
            if shared and self._running_shared >= self.max_shared:
                return
            if not shared and self._running_shared:
                return
            self._line.popleft()
            if shared:
                self._running_shared += 1
            else:
                self._running_exclusive = True
            turn.set_result(None)

    def _leave(self, shared: bool) -> None:
        if shared:
            self._running_shared -= 1
        else:
            self._running_exclusive = False
        self._admit()


class TooManyUploads(QueueFull):
    """Raised by :class:`Admission`: a :class:`QueueFull`, so it is ``busy`` (503)."""

    def __init__(self, capacity: int) -> None:
        Exception.__init__(self, f"too many uploads decoding ({capacity})")
        self.capacity = capacity


class Admission:
    """A non-blocking counter for work done outside the slot, such as decoding
    uploads: at most ``capacity`` at once, the next raises :class:`TooManyUploads`."""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.capacity = capacity
        self.active = 0

    @contextmanager
    def admit(self) -> Iterator[None]:
        if self.active >= self.capacity:
            raise TooManyUploads(self.capacity)
        self.active += 1
        try:
            yield
        finally:
            self.active -= 1
