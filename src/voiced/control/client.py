"""Blocking client for the control socket, used by the CLI.

.. code-block:: python

    with ControlClient.connect(path) as client:
        client.request("settings.get")

:class:`ControlUnavailable` means nothing answers on the socket: voiced is
not running or not installed, and the caller falls back to working without
it. Events that arrive while waiting for an answer are kept in
:attr:`ControlClient.events`.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from types import TracebackType
from typing import Any

from voiced.control.protocol import encode

DEFAULT_TIMEOUT_S = 10.0
"""Every operation answers at once (a restart is accepted and finishes in
the background); the engine catalogue, the slowest, times out sooner."""


class ControlUnavailable(OSError):
    """Nothing answers on the control socket."""


class ControlError(Exception):
    """The server answered ``ok: false``."""

    def __init__(self, code: str, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.data = data or {}


class ControlClient:
    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._file = sock.makefile("rb")
        self._next_id = 0
        self.events: list[dict[str, Any]] = []
        self.hello: dict[str, Any] = {}

    @classmethod
    def connect(cls, path: Path, timeout_s: float = DEFAULT_TIMEOUT_S) -> ControlClient:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout_s)
        try:
            sock.connect(str(path))
        except OSError as exc:
            sock.close()
            raise ControlUnavailable(f"no control socket at {path}: {exc}") from exc
        client = cls(sock)
        try:
            first = client._read()
        except (OSError, ValueError) as exc:
            client.close()
            raise ControlUnavailable(f"{path} did not greet: {exc}") from exc
        if first.get("event") != "hello":
            client.close()
            raise ControlUnavailable(f"{path} did not greet")
        client.hello = first.get("data") or {}
        return client

    def _read(self) -> dict[str, Any]:
        # The 64 KiB line limit applies to requests only; a reply such as a
        # 1000-line log tail is longer.
        line = self._file.readline()
        if not line:
            raise ConnectionError("control socket closed")
        message = json.loads(line)
        if not isinstance(message, dict):
            raise ValueError("not a JSON object")
        return message

    def request(self, op: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one request and wait for its answer. Raises :class:`ControlError`
        for ``ok: false`` and ``OSError`` if the connection breaks."""
        self._next_id += 1
        req_id = self._next_id
        self._sock.sendall(encode({"id": req_id, "op": op, "args": args or {}}))
        while True:
            message = self._read()
            if "event" in message and "id" not in message:
                self.events.append(message)
                continue
            if message.get("id") != req_id:
                continue
            if message.get("ok"):
                result = message.get("result")
                return result if isinstance(result, dict) else {}
            err = message.get("error") or {}
            raise ControlError(
                str(err.get("code", "internal")), str(err.get("message", "")), dict(err)
            )

    def close(self) -> None:
        try:
            self._file.close()
        finally:
            self._sock.close()

    def __enter__(self) -> ControlClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def request(
    path: Path, op: str, args: dict[str, Any] | None = None, timeout_s: float = DEFAULT_TIMEOUT_S
) -> dict[str, Any]:
    """Connect, send one request, close. Raises :class:`ControlUnavailable`
    when nothing answers."""
    with ControlClient.connect(path, timeout_s) as client:
        return client.request(op, args)
