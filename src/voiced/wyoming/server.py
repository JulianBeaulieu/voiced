"""Wyoming listeners. Each runs in the same process and event loop as the HTTP
app for its modality and shares its ``ServiceContext`` (engine, queue, health).

:func:`attach` wraps the FastAPI lifespan so uvicorn starts the listener with
the HTTP app and stops it on shutdown; nothing else in the process needs to
know about it. The engine loads in the background (ADR 0005), so the listener
may accept connections before the model is warm; requests answer
``server_busy`` until it is. See ``docs/decisions/0011-wyoming.md``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from voiced.context import ServiceContext
from voiced.inference import DISCONNECT_ERRORS
from voiced.wyoming.handlers import VoicedEventHandler
from voiced.wyoming.zeroconf import ZeroconfAdvertiser, should_advertise

if TYPE_CHECKING:
    from fastapi import FastAPI

log = logging.getLogger(__name__)

STOP_GRACE_S = 2.0


class WyomingListener:
    """One TCP listener bound to ``host:port`` (``port=0`` picks a free one).

    ``handler_cls`` and ``service_name`` are supplied by the stt/tts process so
    the listener never has to branch on ``ctx.service``.
    """

    def __init__(
        self,
        ctx: ServiceContext,
        host: str,
        port: int,
        *,
        handler_cls: type[VoicedEventHandler],
        service_name: str,
    ) -> None:
        self.ctx = ctx
        self.host = host
        self._requested_port = port
        self._handler_cls = handler_cls
        self.service_name = service_name
        self.zeroconf_enabled = ctx.settings.server.zeroconf
        self._server: asyncio.Server | None = None
        self._handlers: dict[asyncio.Task[None], VoicedEventHandler] = {}
        self._advertiser: ZeroconfAdvertiser | None = None

    @property
    def port(self) -> int:
        """The bound port once started."""
        if self._server is None or not self._server.sockets:
            return self._requested_port
        port: int = self._server.sockets[0].getsockname()[1]
        return port

    @property
    def advertising(self) -> bool:
        return self._advertiser is not None and self._advertiser.active

    async def _on_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        handler = self._handler_cls(self.ctx, reader, writer)
        task = asyncio.current_task()
        assert task is not None
        self._handlers[task] = handler
        log.debug("%s wyoming: %s connected", self.ctx.service, handler.peer)
        try:
            await handler.run()
        except DISCONNECT_ERRORS as exc:
            log.debug("%s wyoming: %s dropped: %s", self.ctx.service, handler.peer, exc)
        finally:
            self._handlers.pop(task, None)
            with contextlib.suppress(OSError):  # the socket is gone either way
                await writer.wait_closed()
            log.debug("%s wyoming: %s disconnected", self.ctx.service, handler.peer)

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._on_connection, host=self.host, port=self._requested_port
        )
        log.info("%s wyoming listening on %s:%d", self.ctx.service, self.host, self.port)
        if should_advertise(self.host, self.zeroconf_enabled):
            self._advertiser = ZeroconfAdvertiser(self.service_name, self.port, self.host)
            await self._advertiser.start()
        elif self.zeroconf_enabled:
            log.info("%s wyoming: bound to loopback, zeroconf not advertised", self.ctx.service)

    async def stop(self) -> None:
        if self._advertiser is not None:
            await self._advertiser.stop()
            self._advertiser = None
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        handlers = list(self._handlers.items())
        for _, handler in handlers:
            await handler.stop()
        if handlers:
            tasks = [task for task, _ in handlers]
            _, pending = await asyncio.wait(tasks, timeout=STOP_GRACE_S)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        await server.wait_closed()
        log.info("%s wyoming listener stopped", self.ctx.service)

    async def __aenter__(self) -> WyomingListener:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()


def attach(
    app: FastAPI,
    ctx: ServiceContext,
    host: str,
    port: int,
    *,
    handler_cls: type[VoicedEventHandler],
    service_name: str,
) -> WyomingListener:
    """Run a listener for the app's lifetime, inside the app's own lifespan.

    The listener starts right after the inner (HTTP) lifespan has started the
    background engine load, so, like ``/health``, it is up while the model is
    still loading. It is stored as ``app.state.wyoming``.
    """
    listener = WyomingListener(ctx, host, port, handler_cls=handler_cls, service_name=service_name)
    inner = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with inner(app):
            await listener.start()
            try:
                yield
            finally:
                await listener.stop()

    app.router.lifespan_context = lifespan
    app.state.wyoming = listener
    return listener
