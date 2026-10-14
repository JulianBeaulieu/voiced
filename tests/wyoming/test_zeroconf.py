from __future__ import annotations

import pytest
from fastapi import FastAPI
from wyoming.client import AsyncTcpClient
from wyoming.info import Describe, Info

from voiced.wyoming import WYOMING_STT_SERVICE, ZEROCONF_TYPE
from voiced.wyoming import zeroconf as zc
from voiced.wyoming.server import WyomingListener
from voiced.wyoming.stt_handler import STTEventHandler


@pytest.mark.parametrize(
    ("bind", "expected"),
    [
        ("127.0.0.1", False),
        ("localhost", False),
        ("::1", False),
        ("0.0.0.0", True),
        ("10.0.0.5", True),
    ],
)
def test_should_advertise(bind: str, expected: bool) -> None:
    assert zc.should_advertise(bind, enabled=True) is expected
    assert zc.should_advertise(bind, enabled=False) is False


async def test_advertised_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(zc, "_primary_ipv4", lambda: "10.1.2.3")
    assert await zc.advertised_address("127.0.0.1") is None
    assert await zc.advertised_address("0.0.0.0") == "10.1.2.3"
    assert await zc.advertised_address("10.9.8.7") == "10.9.8.7"
    assert await zc.advertised_address("::") == "10.1.2.3"


async def test_advertised_address_resolves_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_resolve(host: str) -> str | None:
        return "10.4.5.6" if host == "host.example" else None

    monkeypatch.setattr(zc, "_resolve_host", fake_resolve)
    assert await zc.advertised_address("host.example") == "10.4.5.6"
    assert await zc.advertised_address("nope.example") is None


def test_server_name_ends_with_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(zc.socket, "gethostname", lambda: "example.local")
    assert zc.local_server_name() == "example.local."


async def test_listener_registers_when_routable(
    stt_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, int, str]] = []

    class FakeAdvertiser:
        def __init__(self, service_name: str, port: int, bind: str) -> None:
            calls.append((service_name, port, bind))
            self.active = False

        async def start(self) -> bool:
            self.active = True
            return True

        async def stop(self) -> None:
            self.active = False

    import voiced.wyoming.server as server_mod

    monkeypatch.setattr(server_mod, "ZeroconfAdvertiser", FakeAdvertiser)
    monkeypatch.setattr(server_mod, "should_advertise", lambda bind, enabled: enabled)
    ctx = stt_app.state.ctx
    await ctx.engine.load()

    ctx.settings.server.zeroconf = True
    listener = WyomingListener(
        ctx, "127.0.0.1", 0, handler_cls=STTEventHandler, service_name=WYOMING_STT_SERVICE
    )
    async with listener:
        assert listener.advertising is True
        assert calls == [("voiced-stt", listener.port, "127.0.0.1")]
    assert listener.advertising is False

    ctx.settings.server.zeroconf = False
    off = WyomingListener(
        ctx, "127.0.0.1", 0, handler_cls=STTEventHandler, service_name=WYOMING_STT_SERVICE
    )
    async with off:
        assert off.advertising is False
    assert len(calls) == 1


def test_service_type_constant() -> None:
    assert ZEROCONF_TYPE == "_wyoming._tcp.local."


async def test_zeroconf_failure_does_not_stop_the_listener(
    stt_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """launchd may start the service before the network is up; AsyncZeroconf()
    then raises and the listener must keep serving."""

    def no_interfaces() -> None:
        raise RuntimeError("No interfaces to listen on")

    async def routable(bind: str) -> str:
        return "10.0.0.5"

    import voiced.wyoming.server as server_mod

    monkeypatch.setattr(zc, "AsyncZeroconf", no_interfaces)
    monkeypatch.setattr(zc, "advertised_address", routable)
    monkeypatch.setattr(server_mod, "should_advertise", lambda bind, enabled: True)
    ctx = stt_app.state.ctx
    await ctx.engine.load()
    ctx.health.mark_loaded()
    listener = WyomingListener(
        ctx, "127.0.0.1", 0, handler_cls=STTEventHandler, service_name=WYOMING_STT_SERVICE
    )
    async with listener:
        assert listener.advertising is False
        async with AsyncTcpClient("127.0.0.1", listener.port, read_timeout=5) as c:
            await c.write_event(Describe().event())
            event = await c.read_event()
        assert event is not None and Info.is_type(event.type)
