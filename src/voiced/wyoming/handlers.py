"""Shared base for the two event handlers.

The handlers never decide what an exception means. Engine calls run inside
``voiced.inference.inference()``, which records health and metrics; the base
has one ``except Exception`` that maps ``classify(exc)`` to a Wyoming ``Error``
through :data:`_WIRE`, the Wyoming counterpart of the HTTP ``_HTTP`` table
(ADR 0005). Writes made while holding the slot are bounded with
:func:`voiced.inference.bounded` so a client that stops reading cannot pin the
GPU.

A streamed request (an STT session, a TTS stream) that fails sends one
``Error`` and the connection stays open: the rest of its events are read and
dropped until the event that ends it (``AudioStop``, ``SynthesizeStop``), at
most ``server.session_idle_s`` after the failure (:meth:`fail_and_drop`,
:meth:`drop`). Home Assistant writes the whole stream before it reads, and
closing at once would answer its next write with a reset, which can discard
the ``Error`` before it is read. A disconnect still closes at once.
"""

from __future__ import annotations

import asyncio
import json
import logging
from abc import abstractmethod
from typing import Any, TypeVar

from wyoming.error import Error
from wyoming.event import Event, Eventable
from wyoming.server import AsyncEventHandler

from voiced.context import ServiceContext
from voiced.engines.base import EngineError
from voiced.inference import DISCONNECT_ERRORS, bounded, classify
from voiced.metrics import Outcome
from voiced.validate import is_int

log = logging.getLogger(__name__)

ERROR_SERVER_BUSY = "server_busy"
ERROR_INVALID_REQUEST = "invalid_request_error"
ERROR_ENGINE = "engine_error"

MAX_EVENT_BYTES = 1_000_000
"""Largest JSON ``data`` block, and largest binary payload, of one event.
Home Assistant sends audio chunks of about 2 KB and synthesis text of a few
KB; this also bounds ``tts.max_input_chars`` on Wyoming to roughly 170 000
characters (escaped JSON can take six bytes per character)."""
CONNECTION_IDLE_S = 300.0
"""A connection that starts no event for this long is closed. Home Assistant
opens a connection per request; the bound is long so a streaming synthesis
can wait for a slow conversation agent between chunks."""


class BadEvent(Exception):
    """An event the reader refuses before buffering it: oversize or malformed."""


class MalformedEvent(EngineError):
    """An event whose data lacks a field or has the wrong type. A client error."""


E = TypeVar("E", bound=Eventable)


def parse(cls: type[E], event: Event) -> E:
    """``cls.from_event(event)``, with a missing or mistyped field reported as
    the client's mistake instead of a server failure."""
    try:
        parsed = cls.from_event(event)
    except (KeyError, TypeError, ValueError) as exc:
        raise MalformedEvent(f"malformed {event.type}: {exc!r}") from None
    assert isinstance(parsed, cls)
    return parsed


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


def _length(header: dict[str, Any], key: str, limit: int) -> int:
    value = header.get(key)
    if value is None:
        return 0
    if not is_int(value) or value < 0:
        raise BadEvent(f"{key} must be a non-negative integer")
    if value > limit:
        raise BadEvent(f"{key} {value} is over the limit of {limit} bytes")
    return value


class VoicedEventHandler(AsyncEventHandler):
    """One client connection. Subclasses implement ``handle`` and ``cleanup``."""

    _dropping_until: float | None = None
    """Set when a streamed request failed and reported: the loop time until
    which the rest of it is read and dropped (see the module docstring)."""

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

    async def run(self) -> None:
        """The ``AsyncEventHandler`` loop (``wyoming`` 1.10) with a bounded reader.

        The ``wyoming`` package reads ``data_length`` and ``payload_length``
        bytes, whatever they say, before anything can look at the event, and
        waits for the next event forever. Here an event must announce sizes
        within the limits, the rest of an event must arrive within
        ``session_idle_s`` of its header, and a connection may stay silent
        between events for :data:`CONNECTION_IDLE_S`.
        """
        self._is_running = True
        try:
            while self._is_running:
                try:
                    event = await self.read_event()
                except BadEvent as exc:
                    log.debug("%s %s: refused event: %s", self.ctx.service, self.peer, exc)
                    await self.send_error(f"BadEvent: {exc}", ERROR_INVALID_REQUEST)
                    break
                if event is None or not await self.handle_event(event):
                    break
        finally:
            await self.disconnect()
            self.writer.close()

    async def read_event(self) -> Event | None:
        """The next event, or ``None`` when the client closed or went quiet."""
        try:
            line = await bounded(self.reader.readline(), CONNECTION_IDLE_S)
        except ValueError:  # longer than the reader's line limit (64 KiB)
            raise BadEvent("header line too long") from None
        except DISCONNECT_ERRORS:
            return None
        if not line:
            return None
        try:
            header = json.loads(line)
        except (ValueError, RecursionError):  # "[" * 60000 recurses before it fails
            raise BadEvent("header is not JSON") from None
        if not isinstance(header, dict) or not isinstance(header.get("type"), str):
            raise BadEvent("header must be an object with a type")
        data = header.get("data") or {}
        if not isinstance(data, dict):
            raise BadEvent("data must be an object")
        data_length = _length(header, "data_length", MAX_EVENT_BYTES)
        payload_length = _length(header, "payload_length", MAX_EVENT_BYTES)
        try:
            if data_length:
                block = await bounded(self.reader.readexactly(data_length), self._idle_timeout_s)
                try:
                    extra = json.loads(block)
                except (ValueError, RecursionError):
                    raise BadEvent("data is not JSON") from None
                if not isinstance(extra, dict):
                    raise BadEvent("data must be an object")
                data = {**data, **extra}
            payload = None
            if payload_length:
                payload = await bounded(
                    self.reader.readexactly(payload_length), self._idle_timeout_s
                )
        except DISCONNECT_ERRORS:
            return None
        return Event(type=header["type"], data=data, payload=payload)

    async def write_event(self, event: Event) -> None:
        if self.writer.is_closing():
            raise ConnectionResetError("client disconnected")
        async with self._write_lock:
            await bounded(super().write_event(event), self._write_timeout_s)

    async def handle_event(self, event: Event) -> bool:
        try:
            return await self.handle(event)
        except Exception as exc:
            await self.cleanup()
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

    async def fail_and_drop(self, exc: Exception) -> None:
        """Report a failed streamed request once and start dropping the rest
        of it. The deadline is set before the first await, so the connection
        drops every later event of the request."""
        self._dropping_until = asyncio.get_running_loop().time() + self._idle_timeout_s
        await self.report(exc)
        if classify(exc) is Outcome.DISCONNECT:
            await self.stop()  # nobody is left to read an Error

    async def drop(self, end: bool, task: asyncio.Task[Any] | None) -> bool:
        """An event of the failed request: dropped (``True``), or the close
        once ``end`` (the request's last event) arrives or the deadline has
        passed. ``task`` may still be sending the ``Error``; it is awaited
        before the close."""
        assert self._dropping_until is not None
        if not end and asyncio.get_running_loop().time() < self._dropping_until:
            return True
        if task is not None:
            await task
        return False

    @abstractmethod
    async def handle(self, event: Event) -> bool: ...

    async def cleanup(self) -> None:
        """Release per-connection resources (session task, worker task).

        Must not raise: the tasks it cancels catch their own exceptions, so
        only the caller's own cancellation propagates (see :func:`cancel_task`).
        """

    async def disconnect(self) -> None:
        await self.cleanup()

    async def send_error(self, text: str, code: str) -> None:
        try:
            await self.write_event(Error(text=text, code=code).event())
        except DISCONNECT_ERRORS:
            pass
