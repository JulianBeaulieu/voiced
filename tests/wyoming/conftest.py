from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import cast

import pytest
from fastapi import FastAPI
from wyoming.client import AsyncTcpClient
from wyoming.event import Event

from tests.conftest import free_port
from voiced.context import ServiceContext
from voiced.wyoming import WYOMING_STT_SERVICE, WYOMING_TTS_SERVICE
from voiced.wyoming.server import WyomingListener
from voiced.wyoming.stt_handler import STTEventHandler
from voiced.wyoming.tts_handler import TTSEventHandler

__all__ = ["client", "free_port", "read_until", "stub_writer", "wait_for"]

READ_TIMEOUT = 5.0


async def _loaded_ctx(app: FastAPI) -> ServiceContext:
    ctx: ServiceContext = app.state.ctx
    await ctx.engine.load()
    ctx.health.mark_loaded()
    return ctx


@pytest.fixture
async def stt_listener(stt_app: FastAPI) -> AsyncIterator[WyomingListener]:
    ctx = await _loaded_ctx(stt_app)
    ctx.settings.server.zeroconf = False
    listener = WyomingListener(
        ctx, "127.0.0.1", 0, handler_cls=STTEventHandler, service_name=WYOMING_STT_SERVICE
    )
    async with listener:
        yield listener


@pytest.fixture
async def tts_listener(tts_app: FastAPI) -> AsyncIterator[WyomingListener]:
    ctx = await _loaded_ctx(tts_app)
    ctx.settings.server.zeroconf = False
    listener = WyomingListener(
        ctx, "127.0.0.1", 0, handler_cls=TTSEventHandler, service_name=WYOMING_TTS_SERVICE
    )
    async with listener:
        yield listener


class _Client(AsyncTcpClient):
    """A test client whose close tolerates a reset.

    When the server closes a connection while audio the client sent is still
    unread in the server's receive buffer, the kernel answers with a reset, and
    closing our end then raises. The events read before that are unaffected.
    """

    async def disconnect(self) -> None:
        with contextlib.suppress(ConnectionError):
            await super().disconnect()


def client(listener: WyomingListener) -> AsyncTcpClient:
    return _Client("127.0.0.1", listener.port, read_timeout=READ_TIMEOUT)


async def read_until(client: AsyncTcpClient, *types: str) -> list[Event]:
    """Read events until one of ``types`` arrives (inclusive) or the peer closes."""
    events: list[Event] = []
    while True:
        event = await client.read_event()
        if event is None:
            return events
        events.append(event)
        if event.type in types:
            return events


async def wait_for(predicate: object, within: float = 2.0) -> None:
    """Poll a zero-argument callable until it returns true."""
    assert callable(predicate)
    deadline = asyncio.get_running_loop().time() + within
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


class _StubWriter:
    """A stream writer without a socket. ``drain`` takes ``drain_delay``
    seconds, long enough by default to exercise the write timeout."""

    def __init__(self, *, closing: bool = False, drain_delay: float = 10.0) -> None:
        self._closing = closing
        self._drain_delay = drain_delay

    def get_extra_info(self, name: str, default: object = None) -> tuple[str, int]:
        return ("203.0.113.1", 9999)

    def is_closing(self) -> bool:
        return self._closing

    def writelines(self, data: object) -> None:
        pass

    def write(self, data: object) -> None:
        pass

    async def drain(self) -> None:
        await asyncio.sleep(self._drain_delay)

    def close(self) -> None:
        self._closing = True

    async def wait_closed(self) -> None:
        pass


def stub_writer(*, closing: bool = False) -> asyncio.StreamWriter:
    """A handler-ready writer for unit tests that need no connection."""
    return cast("asyncio.StreamWriter", _StubWriter(closing=closing))
