"""mDNS advertisement so Home Assistant discovers the listeners.

Each listener is announced as ``<service>._wyoming._tcp.local.`` with the
bound port. Advertisement is skipped when the service binds a loopback
address: nothing outside this machine can reach it, and Home Assistant would
otherwise try to connect to *its own* 127.0.0.1.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket

from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

from voiced.wyoming import ZEROCONF_TYPE

log = logging.getLogger(__name__)

_MDNS_GROUP = "224.0.0.251"


def is_loopback(bind: str) -> bool:
    if bind in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(bind).is_loopback
    except ValueError:
        return False


def should_advertise(bind: str, enabled: bool) -> bool:
    return enabled and not is_loopback(bind)


def _is_unspecified(bind: str) -> bool:
    try:
        return ipaddress.ip_address(bind).is_unspecified
    except ValueError:
        return False


def _primary_ipv4() -> str | None:
    """The IPv4 address the kernel would use to reach the LAN (no packets sent)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((_MDNS_GROUP, 1))
        address: str = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()
    return None if ipaddress.ip_address(address).is_loopback else address


async def advertised_address(bind: str) -> str | None:
    """Which address to put in the mDNS record for a service bound to ``bind``."""
    if is_loopback(bind):
        return None
    if _is_unspecified(bind):
        return _primary_ipv4()
    try:
        ip = ipaddress.ip_address(bind)
    except ValueError:
        return await _resolve_host(bind)
    return str(ip) if ip.version == 4 else _primary_ipv4()


async def _resolve_host(host: str) -> str | None:
    """Resolve a hostname to an IPv4 address via the loop's resolver.

    ``socket.gethostbyname`` would block the event loop; ``loop.getaddrinfo``
    runs the lookup off the loop.
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
    except OSError:
        return None
    return str(infos[0][4][0]) if infos else None


def local_server_name() -> str:
    host = socket.gethostname().removesuffix(".").removesuffix(".local")
    return f"{host or 'voiced'}.local."


class ZeroconfAdvertiser:
    """Registers one ``_wyoming._tcp`` service; ``stop`` withdraws it."""

    def __init__(self, service_name: str, port: int, bind: str) -> None:
        self.service_name = service_name
        self.port = port
        self.bind = bind
        self._zc: AsyncZeroconf | None = None
        self._info: AsyncServiceInfo | None = None

    @property
    def active(self) -> bool:
        return self._info is not None

    async def start(self) -> bool:
        address = await advertised_address(self.bind)
        if address is None:
            log.warning("zeroconf: no routable address for bind %s; not advertising", self.bind)
            return False
        # Everything that can fail goes inside the try: AsyncZeroconf() raises
        # RuntimeError("No interfaces to listen on") when launchd starts the
        # service before the network is up, and that must not kill the service.
        zc: AsyncZeroconf | None = None
        try:
            info = AsyncServiceInfo(
                ZEROCONF_TYPE,
                f"{self.service_name}.{ZEROCONF_TYPE}",
                addresses=[socket.inet_aton(address)],
                port=self.port,
                server=local_server_name(),
            )
            zc = AsyncZeroconf()
            await zc.async_register_service(info, allow_name_change=True)
        except Exception as exc:
            log.warning("zeroconf: registration failed: %s", exc)
            if zc is not None:
                await zc.async_close()
            return False
        self._zc, self._info = zc, info
        log.info("zeroconf: advertising %s at %s:%d", info.name, address, self.port)
        return True

    async def stop(self) -> None:
        zc, info = self._zc, self._info
        self._zc = self._info = None
        if zc is None or info is None:
            return
        try:
            await zc.async_unregister_service(info)
        except Exception as exc:
            log.debug("zeroconf: unregister failed: %s", exc)
        finally:
            await zc.async_close()
