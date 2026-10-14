"""The control socket: an asyncio Unix server inside the watchdog process.

Security comes from the filesystem and the kernel, not from the protocol:
the socket lives in the user's 0700 state directory, is 0600 itself, and
every connection's peer uid (``LOCAL_PEERCRED`` on macOS, ``SO_PEERCRED`` on
Linux) must equal the server's own uid or it is closed before a byte is
sent. Nothing listens on a network interface (ADR 0013).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import stat
import struct
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from voiced.control.ops import Operations
from voiced.control.protocol import (
    BAD_REQUEST,
    MAX_LINE_BYTES,
    OpError,
    encode,
    error,
    event,
    hello,
    ok,
    parse_request,
)

log = logging.getLogger("voiced.control")

STATUS_POLL_S = 2.0
"""How often the status is re-read while at least one client is connected."""
STATUS_PUSH_MAX_S = 10.0
"""A ``status`` event goes out at least this often, changed or not."""
WRITE_TIMEOUT_S = 5.0
"""A client that does not take a message within this long is dropped."""

# <sys/un.h> on macOS: SOL_LOCAL and LOCAL_PEERCRED; struct xucred is
# {u_int cr_version; uid_t cr_uid; short cr_ngroups; gid_t cr_groups[16]}.
_SOL_LOCAL = 0
_LOCAL_PEERCRED = 0x001
_XUCRED_SIZE = 76
# <sys/socket.h> on Linux: struct ucred is {pid_t pid; uid_t uid; gid_t gid}.
_SO_PEERCRED = getattr(socket, "SO_PEERCRED", 17)
# A plain str so both branches below are type-checked on either platform.
_PLATFORM: str = sys.platform


def peer_uid(sock: socket.socket) -> int | None:
    """The uid of the process at the other end of a Unix socket, or None if
    the platform cannot say (the server then refuses the connection)."""
    try:
        if _PLATFORM == "darwin":
            raw = sock.getsockopt(_SOL_LOCAL, _LOCAL_PEERCRED, _XUCRED_SIZE)
            _version, uid = struct.unpack_from("=II", raw)
            return int(uid)
        if _PLATFORM.startswith("linux"):
            raw = sock.getsockopt(socket.SOL_SOCKET, _SO_PEERCRED, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", raw)
            return int(uid)
    except (OSError, struct.error):
        return None
    return None


class SocketInUse(RuntimeError):
    """Another server already answers on the socket path."""


def prepare_socket_path(path: Path) -> None:
    """Make the parent directory private and clear a stale socket.

    The parent is created 0700, or tightened to 0700 if it is ours. A socket
    file nobody answers on is left over from a crash and is removed; one that
    answers belongs to a running watchdog, and anything that is not a socket
    is not ours to delete.
    """
    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = parent.stat()
    if info.st_uid != os.getuid():
        raise PermissionError(f"{parent} is not owned by uid {os.getuid()}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        os.chmod(parent, 0o700)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(mode):
        raise FileExistsError(f"{path} exists and is not a socket; not removing it")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        probe.connect(str(path))
    except OSError:
        log.info("removing stale control socket %s", path)
        path.unlink(missing_ok=True)
        return
    finally:
        probe.close()
    raise SocketInUse(f"{path} is in use by another process")


@dataclass(eq=False)
class _Client:
    writer: asyncio.StreamWriter
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False


def _fingerprint(status: dict[str, Any]) -> Any:
    """``status`` without the fields that change on every read."""

    def strip(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k != "uptime_s"}
        return value

    return strip(status)


class ControlServer:
    """Serves :class:`Operations` on a Unix socket and pushes events.

    ``status`` events go to every client whenever the status changes (polled
    every :data:`STATUS_POLL_S` while anyone is connected) and at least every
    :data:`STATUS_PUSH_MAX_S`. ``settings`` events go out whenever the config
    file changes: right after a ``settings.set`` that wrote it, and within a
    poll after anything else wrote it (the status page form, an editor).
    """

    def __init__(
        self,
        path: Path,
        ops: Operations,
        *,
        version: str,
        uid_of: Callable[[socket.socket], int | None] = peer_uid,
        status_poll_s: float = STATUS_POLL_S,
        status_push_max_s: float = STATUS_PUSH_MAX_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.path = path
        self.ops = ops
        self.version = version
        self.uid_of = uid_of
        self.status_poll_s = status_poll_s
        self.status_push_max_s = status_push_max_s
        self.clients: set[_Client] = set()
        self._server: asyncio.AbstractServer | None = None
        self._status_task: asyncio.Task[None] | None = None
        self.clock = clock
        self._wake = asyncio.Event()
        self._force_push = False
        self._last_status: Any = None
        self._last_push: float | None = None
        self._config_stamp: Any = None
        self._config_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        prepare_socket_path(self.path)
        self._config_stamp = await asyncio.to_thread(self.ops.config_stamp)
        self._server = await asyncio.start_unix_server(
            self._handle, path=str(self.path), limit=MAX_LINE_BYTES
        )
        # For a moment 0644 (umask) between bind and chmod; the 0700 parent
        # directory keeps other users out of that window.
        os.chmod(self.path, 0o600)
        self._status_task = asyncio.create_task(self._status_loop(), name="voiced-control-status")
        log.info("control socket listening on %s", self.path)

    async def close(self) -> None:
        if self._status_task is not None:
            self._status_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._status_task
        await self.ops.wait_restarts()
        if self._server is not None:
            self._server.close()
        for client in list(self.clients):
            await self._drop(client)
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 2.0)
        with contextlib.suppress(FileNotFoundError):
            if stat.S_ISSOCK(self.path.lstat().st_mode):
                self.path.unlink()

    async def __aenter__(self) -> ControlServer:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    # Connections -----------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        sock = writer.get_extra_info("socket")
        uid = self.uid_of(sock) if sock is not None else None
        if uid is None or uid != os.getuid():
            log.warning("control: refused a connection from uid %s", uid)
            writer.close()
            if task is not None:
                self._tasks.discard(task)
            return
        client = _Client(writer)
        self.clients.add(client)
        self.refresh_status()
        try:
            await self._send(client, hello(self.version))
            await self._serve(client, reader)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            await self._drop(client)
            if task is not None:
                self._tasks.discard(task)

    async def _serve(self, client: _Client, reader: asyncio.StreamReader) -> None:
        while not client.closed:
            try:
                line = await reader.readuntil(b"\n")
            except asyncio.LimitOverrunError:
                await self._send(client, error(None, BAD_REQUEST, "line too long"))
                return
            except asyncio.IncompleteReadError as exc:
                if exc.partial.strip():
                    await self._send(client, error(None, BAD_REQUEST, "incomplete line"))
                return
            if not line.strip():
                continue
            await self._answer(client, line)

    async def _answer(self, client: _Client, line: bytes) -> None:
        try:
            request = parse_request(line)
        except OpError as exc:
            await self._send(client, error(exc.extra.get("id"), exc.code, exc.message))
            return
        try:
            result = await self.ops.dispatch(request.op, request.args)
        except OpError as exc:
            await self._send(client, error(request.id, exc.code, exc.message, **exc.extra))
            return
        await self._send(client, ok(request.id, result))
        if request.op == "settings.set":
            await self.check_config()
        elif request.op == "service.restart":
            self.refresh_status()

    async def _send(self, client: _Client, message: dict[str, Any]) -> None:
        if client.closed:
            return
        data = encode(message)
        try:
            async with client.lock:
                client.writer.write(data)
                await asyncio.wait_for(client.writer.drain(), WRITE_TIMEOUT_S)
        except (OSError, TimeoutError):
            await self._drop(client)

    async def _drop(self, client: _Client) -> None:
        if client.closed:
            return
        client.closed = True
        self.clients.discard(client)
        client.writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(client.writer.wait_closed(), 1.0)

    async def broadcast(self, message: dict[str, Any]) -> None:
        clients = list(self.clients)
        if clients:
            await asyncio.gather(*(self._send(c, message) for c in clients))

    # Status events ---------------------------------------------------------

    def refresh_status(self) -> None:
        """Read the status now and push it to every client, changed or not."""
        self._force_push = True
        self._wake.set()

    async def poll_once(self) -> None:
        """One round of the event loop: push ``status`` if it changed (or is
        due, or was asked for) and ``settings`` if the config file changed."""
        await self.check_config()
        try:
            status: dict[str, Any] | None = await self.ops.dispatch("status", {})
        except OpError as exc:
            log.warning("control: status for event failed: %s", exc.message)
            status = None
        now = self.clock()
        force, self._force_push = self._force_push, False
        if status is None:
            return
        fingerprint = _fingerprint(status)
        due = self._last_push is None or now - self._last_push >= self.status_push_max_s
        if force or due or fingerprint != self._last_status:
            await self.broadcast(event("status", status))
            self._last_status, self._last_push = fingerprint, now

    async def check_config(self) -> None:
        """Push ``settings`` to every client if the config file changed since
        the last check."""
        async with self._config_lock:
            stamp = await asyncio.to_thread(self.ops.config_stamp)
            if stamp == self._config_stamp:
                return
            self._config_stamp = stamp
            if not self.clients:
                return
            try:
                data = await self.ops.dispatch("settings.get", {})
            except OpError as exc:
                log.warning("control: settings for event failed: %s", exc.message)
                return
            await self.broadcast(event("settings", data))

    async def _status_loop(self) -> None:
        while True:
            if not self.clients:
                self._last_status = self._last_push = None
                self._wake.clear()
                await self._wake.wait()
            await self.poll_once()
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self.status_poll_s)
