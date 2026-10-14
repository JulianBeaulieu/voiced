"""The HTTP app and the Wyoming listener run in one process and one loop."""

from __future__ import annotations

import socket

import pytest
from httpx import ASGITransport, AsyncClient
from wyoming.client import AsyncTcpClient
from wyoming.info import Describe, Info

from voiced.config import Settings
from voiced.stt import build_stt_app
from voiced.testing import FakeSTTEngine, FakeTTSEngine
from voiced.tts import build_tts_app
from voiced.wyoming.server import WyomingListener

from .conftest import free_port


@pytest.mark.parametrize("service", ["stt", "tts"])
async def test_health_and_wyoming_share_one_process(settings: Settings, service: str) -> None:
    port = free_port()
    if service == "stt":
        settings.stt.wyoming_port = port
        engine: FakeSTTEngine | FakeTTSEngine = FakeSTTEngine()
        app = build_stt_app(settings, engine)
    else:
        settings.tts.wyoming_port = port
        engine = FakeTTSEngine()
        app = build_tts_app(settings, engine)

    listener: WyomingListener = app.state.wyoming
    assert listener.ctx is app.state.ctx  # same engine, queue and health

    # Before the lifespan nothing listens.
    with pytest.raises(OSError), socket.create_connection(("127.0.0.1", port), timeout=0.5):
        pass

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
            health = await http.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        async with AsyncTcpClient("127.0.0.1", port, read_timeout=5) as c:
            await c.write_event(Describe().event())
            event = await c.read_event()
        assert event is not None
        info = Info.from_event(event)
        programs = info.asr if service == "stt" else info.tts
        assert programs[0].name == "voiced"
        assert listener.port == port
        assert listener.advertising is False  # loopback bind

    # Shutdown closes the listener.
    with pytest.raises(OSError), socket.create_connection(("127.0.0.1", port), timeout=0.5):
        pass
