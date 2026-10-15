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
SEND_QUEUE_MAX = 64
"""Messages waiting for one client; a client that falls this far behind is
dropped rather than slowing the others."""

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
    """One connection. Everything sent to it goes through ``queue`` to its own
    writer task, so a client that stops reading only ever holds up itself."""

    writer: asyncio.StreamWriter
    queue: asyncio.Queue[bytes | None] = field(
        default_factory=lambda: asyncio.Queue(SEND_QUEUE_MAX)
    )
    sender: asyncio.Task[None] | None = None
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
        self._read_s = 0.0
        """How long the last status read took."""
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
        await asyncio.gather(*(self._finish(c) for c in list(self.clients)))
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
        client.sender = asyncio.create_task(self._sender(client), name="voiced-control-send")
        self.clients.add(client)
        self.refresh_status()
        try:
            self._send(client, hello(self.version))
            await self._serve(client, reader)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            await self._finish(client)
            if task is not None:
                self._tasks.discard(task)

    async def _serve(self, client: _Client, reader: asyncio.StreamReader) -> None:
        while not client.closed:
            try:
                line = await reader.readuntil(b"\n")
            except asyncio.LimitOverrunError:
                self._send(client, error(None, BAD_REQUEST, "line too long"))
                return
            except asyncio.IncompleteReadError as exc:
                if exc.partial.strip():
                    self._send(client, error(None, BAD_REQUEST, "incomplete line"))
                return
            if not line.strip():
                continue
            await self._answer(client, line)

    async def _answer(self, client: _Client, line: bytes) -> None:
        try:
            request = parse_request(line)
        except OpError as exc:
            self._send(client, error(exc.extra.get("id"), exc.code, exc.message))
            return
        try:
            result = await self.ops.dispatch(request.op, request.args)
        except OpError as exc:
            self._send(client, error(request.id, exc.code, exc.message, **exc.extra))
            return
        self._send(client, ok(request.id, result))
        if request.op == "settings.set":
            await self.check_config()
        elif request.op == "service.restart":
            self.refresh_status()

    def _send(self, client: _Client, message: dict[str, Any]) -> None:
        """Queue ``message`` for ``client``; never waits. A client whose
        queue is full is dropped."""
        if client.closed:
            return
        try:
            client.queue.put_nowait(encode(message))
        except asyncio.QueueFull:
            log.warning("control: dropping a client that stopped reading")
            self._abort(client)

    async def _sender(self, client: _Client) -> None:
        """Write ``client``'s queue in order until the ``None`` that ends it."""
        try:
            while (data := await client.queue.get()) is not None:
                client.writer.write(data)
                await asyncio.wait_for(client.writer.drain(), WRITE_TIMEOUT_S)
        except (OSError, TimeoutError):
            self._abort(client)

    def _abort(self, client: _Client) -> None:
        """Drop ``client`` now, discarding what it has not taken."""
        if client.closed:
            return
        client.closed = True
        self.clients.discard(client)
        client.writer.transport.abort()
        if client.sender is not None and client.sender is not asyncio.current_task():
            client.sender.cancel()

    async def _finish(self, client: _Client) -> None:
        """Send what is queued for ``client`` (for at most the write timeout),
        then close it."""
        if client.closed:
            return
        self.clients.discard(client)
        try:
            client.queue.put_nowait(None)
        except asyncio.QueueFull:
            self._abort(client)
            return
        if client.sender is not None:
            await asyncio.wait({client.sender}, timeout=WRITE_TIMEOUT_S)
        client.closed = True  # the sender may have aborted it already; closing twice is fine
        if client.sender is not None:
            client.sender.cancel()
        client.writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(client.writer.wait_closed(), 1.0)

    def broadcast(self, message: dict[str, Any]) -> None:
        """Queue ``message`` for every client; never waits for any of them."""
        for client in list(self.clients):
            self._send(client, message)

    # Status events ---------------------------------------------------------

    def refresh_status(self) -> None:
        """Read the status now and push it to every client, changed or not."""
        self._force_push = True
        self._wake.set()

    async def poll_once(self) -> None:
        """One round of the event loop: push ``status`` if it changed (or is
        due, or was asked for) and ``settings`` if the config file changed."""
        await self.check_config()
        started = self.clock()
        try:
            status: dict[str, Any] | None = await self.ops.dispatch("status", {})
        except OpError as exc:
            log.warning("control: status for event failed: %s", exc.message)
            status = None
        now = self.clock()
        self._read_s = now - started
        force, self._force_push = self._force_push, False
        if status is None:
            return
        fingerprint = _fingerprint(status)
        # Due one read early: the next read would end past the promise.
        due = (
            self._last_push is None
            or now - self._last_push >= self.status_push_max_s - self._read_s
        )
        if force or due or fingerprint != self._last_status:
            self.broadcast(event("status", status))
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
            self.broadcast(event("settings", data))

    async def _status_loop(self) -> None:
        while True:
            if not self.clients:
                self._last_status = self._last_push = None
                self._wake.clear()
                await self._wake.wait()
            await self.poll_once()
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self._next_wait())

    def _next_wait(self) -> float:
        """The poll interval, shortened so that the read which makes the next
        periodic ``status`` due ends by the promised time, however long the
        reads take (a hung service costs a full health check timeout)."""
        if self._last_push is None:
            return self.status_poll_s
        # The next read is due once it ends within one read of the promise
        # (see poll_once), so it starts two reads before it: one read of slack.
        start_by = self._last_push + self.status_push_max_s - 2 * self._read_s
        return max(0.0, min(self.status_poll_s, start_by - self.clock()))
