from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from wyoming.client import AsyncTcpClient
from wyoming.event import Event

from voiced.context import ServiceContext
from voiced.wyoming import WYOMING_STT_SERVICE, WYOMING_TTS_SERVICE
from voiced.wyoming.server import WyomingListener
from voiced.wyoming.stt_handler import STTEventHandler
from voiced.wyoming.tts_handler import TTSEventHandler

READ_TIMEOUT = 5.0


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
    return port


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


def client(listener: WyomingListener) -> AsyncTcpClient:
    return AsyncTcpClient("127.0.0.1", listener.port, read_timeout=READ_TIMEOUT)


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
