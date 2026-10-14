"""Shared base for the two event handlers.

The handlers never decide what an exception means. Engine calls run inside
``voiced.inference.inference()``, which records health and metrics; the base
has one ``except Exception`` that maps ``classify(exc)`` to a Wyoming ``Error``
through :data:`_WIRE`, the Wyoming counterpart of the HTTP ``_HTTP`` table
(ADR 0005). Writes made while holding the slot are bounded with
:func:`voiced.inference.bounded` so a client that stops reading cannot pin the
GPU.
"""

from __future__ import annotations

import asyncio
import logging
from abc import abstractmethod
from typing import Any

from wyoming.error import Error
from wyoming.event import Event
from wyoming.server import AsyncEventHandler

from voiced.context import ServiceContext
from voiced.inference import DISCONNECT_ERRORS, bounded, classify
from voiced.metrics import Outcome

log = logging.getLogger(__name__)

ERROR_SERVER_BUSY = "server_busy"
ERROR_INVALID_REQUEST = "invalid_request_error"
ERROR_ENGINE = "engine_error"

# Outcome -> (Wyoming error code, keep the connection open), the same shape as
# the HTTP ``_HTTP`` table. ``disconnect`` is handled before this table: nothing
# is sent and the connection closes. The error text names the exception type.
_WIRE: dict[Outcome, tuple[str, bool]] = {
    Outcome.BUSY: (ERROR_SERVER_BUSY, False),
    Outcome.CLIENT_ERROR: (ERROR_INVALID_REQUEST, True),
    Outcome.FAILURE: (ERROR_ENGINE, False),
}


async def cancel_task(task: asyncio.Task[Any] | None) -> None:
    """Cancel ``task`` and wait for it to finish.

    The tasks this is used for (STT session, TTS worker) catch their own
    exceptions, so only their cancellation can surface here; it is swallowed
    unless the caller itself is being cancelled.
    """
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise


class VoicedEventHandler(AsyncEventHandler):
    """One client connection. Subclasses implement ``handle`` and ``cleanup``."""

    def __init__(
        self, ctx: ServiceContext, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        super().__init__(reader, writer)
        self.ctx = ctx
        self._write_lock = asyncio.Lock()
        self._write_timeout_s = ctx.settings.server.write_timeout_s
        self._idle_timeout_s = ctx.settings.server.session_idle_s
        peer = writer.get_extra_info("peername")
        self.peer = f"{peer[0]}:{peer[1]}" if isinstance(peer, tuple) else str(peer)

    async def write_event(self, event: Event) -> None:
        if self.writer.is_closing():
            raise ConnectionResetError("client disconnected")
        async with self._write_lock:
            await bounded(super().write_event(event), self._write_timeout_s)

    async def handle_event(self, event: Event) -> bool:
        try:
            return await self.handle(event)
        except Exception as exc:
            await self._cleanup_quietly()
            return await self.report(exc)

    async def report(self, exc: Exception) -> bool:
        """Turn a classified exception into a Wyoming reply. Records nothing:
        ``inference()`` already did. Returns whether to keep the connection."""
        outcome = classify(exc)
        if outcome is Outcome.DISCONNECT:
            log.debug("%s %s: client went away: %r", self.ctx.service, self.peer, exc)
            return False
        code, keep_open = _WIRE[outcome]
        await self.send_error(f"{type(exc).__name__}: {exc}", code)
        return keep_open

    @abstractmethod
    async def handle(self, event: Event) -> bool: ...

    async def cleanup(self) -> None:
        """Release per-connection resources (session task, worker task)."""

    async def disconnect(self) -> None:
        await self._cleanup_quietly()

    async def send_error(self, text: str, code: str) -> None:
        try:
            await self.write_event(Error(text=text, code=code).event())
        except DISCONNECT_ERRORS:
            pass

    async def _cleanup_quietly(self) -> None:
        try:
            await self.cleanup()
        except Exception:
            log.exception("%s %s: cleanup failed", self.ctx.service, self.peer)
