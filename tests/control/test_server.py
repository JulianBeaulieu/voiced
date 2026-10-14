"""The Unix socket server against a fake supervisor."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from voiced.control import protocol
from voiced.control.client import ControlClient, ControlError, ControlUnavailable
from voiced.control.ops import Operations
from voiced.control.server import ControlServer, SocketInUse, peer_uid, prepare_socket_path

from .conftest import FakeSupervisor


@pytest.fixture
async def server(short_dir: Path, ops: Operations) -> AsyncIterator[ControlServer]:
    srv = ControlServer(
        short_dir / "state" / "control.sock",
        ops,
        version="9.9.9",
        status_poll_s=0.05,
        status_push_max_s=0.3,
    )
    async with srv:
        yield srv


class Conn:
    """A raw async client: what any other language's client would do."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer

    @classmethod
    async def open(cls, path: Path) -> Conn:
        reader, writer = await asyncio.open_unix_connection(str(path), limit=1 << 20)
        return cls(reader, writer)

    async def send(self, raw: bytes) -> None:
        self.writer.write(raw)
        await self.writer.drain()

    async def recv(self, wait_s: float = 2.0) -> dict[str, Any]:
        line = await asyncio.wait_for(self.reader.readline(), wait_s)
        assert line, "connection closed"
        message: dict[str, Any] = json.loads(line)
        return message

    async def answer(self, req_id: int | None, wait_s: float = 2.0) -> dict[str, Any]:
        while True:
            message = await self.recv(wait_s)
            if "event" not in message and message.get("id") == req_id:
                return message

    async def event(self, name: str, wait_s: float = 2.0) -> dict[str, Any]:
        while True:
            message = await self.recv(wait_s)
            if message.get("event") == name:
                return message

    async def close(self) -> None:
        self.writer.close()
        await self.writer.wait_closed()


async def test_hello_and_permissions(server: ControlServer) -> None:
    conn = await Conn.open(server.path)
    assert await conn.recv() == {"event": "hello", "data": {"protocol": 1, "version": "9.9.9"}}
    assert stat.S_IMODE(server.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(server.path.parent.stat().st_mode) == 0o700
    await conn.send(b'{"id": 1, "op": "status"}\n')
    answer = await conn.answer(1)
    assert answer["ok"] is True and answer["result"]["installed"] is True
    await conn.close()


async def test_socket_removed_on_close(short_dir: Path, ops: Operations) -> None:
    srv = ControlServer(short_dir / "control.sock", ops, version="x")
    await srv.start()
    assert srv.path.exists()
    await srv.close()
    assert not srv.path.exists()


async def test_peer_uid_is_ours(server: ControlServer) -> None:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(server.path))
    try:
        assert peer_uid(sock) == os.getuid()
    finally:
        sock.close()


async def test_other_uid_is_closed_without_a_byte(short_dir: Path, ops: Operations) -> None:
    srv = ControlServer(
        short_dir / "control.sock", ops, version="x", uid_of=lambda s: os.getuid() + 1
    )
    async with srv:
        reader, writer = await asyncio.open_unix_connection(str(srv.path))
        assert await asyncio.wait_for(reader.read(), 2.0) == b""
        writer.close()
        assert not srv.clients


async def test_unknown_uid_is_refused(short_dir: Path, ops: Operations) -> None:
    async with ControlServer(
        short_dir / "control.sock", ops, version="x", uid_of=lambda s: None
    ) as srv:
        reader, writer = await asyncio.open_unix_connection(str(srv.path))
        assert await asyncio.wait_for(reader.read(), 2.0) == b""
        writer.close()


def test_stale_socket_is_removed(short_dir: Path) -> None:
    path = short_dir / "control.sock"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    sock.close()  # bound, never listened: nobody answers
    assert stat.S_ISSOCK(path.lstat().st_mode)
    prepare_socket_path(path)
    assert not path.exists()


async def test_socket_in_use_is_not_taken_over(server: ControlServer, ops: Operations) -> None:
    with pytest.raises(SocketInUse):
        await ControlServer(server.path, ops, version="x").start()
    conn = await Conn.open(server.path)  # the first server still answers
    assert (await conn.recv())["event"] == "hello"
    await conn.close()


def test_regular_file_is_not_deleted(short_dir: Path) -> None:
    path = short_dir / "control.sock"
    path.write_text("mine")
    with pytest.raises(FileExistsError):
        prepare_socket_path(path)
    assert path.read_text() == "mine"


def test_parent_dir_is_tightened(short_dir: Path) -> None:
    short_dir.chmod(0o755)
    prepare_socket_path(short_dir / "control.sock")
    assert stat.S_IMODE(short_dir.stat().st_mode) == 0o700


async def test_malformed_input_keeps_the_connection(server: ControlServer) -> None:
    conn = await Conn.open(server.path)
    await conn.recv()
    await conn.send(b"this is not json\n")
    bad = await conn.answer(None)
    assert bad["ok"] is False and bad["error"]["code"] == protocol.BAD_REQUEST
    await conn.send(b'{"id": 2}\n\n')
    assert (await conn.answer(2))["error"]["code"] == protocol.BAD_REQUEST
    await conn.send(b'{"id": 3, "op": "bogus"}\n')
    assert (await conn.answer(3))["error"]["code"] == protocol.UNKNOWN_OP
    await conn.send(b'{"id": 4, "op": "settings.get"}\n')
    assert (await conn.answer(4))["ok"] is True
    await conn.close()


async def test_overlong_line_is_refused(server: ControlServer) -> None:
    conn = await Conn.open(server.path)
    await conn.recv()
    await conn.send(b"x" * (protocol.MAX_LINE_BYTES + 10) + b"\n")
    answer = await conn.answer(None)
    assert answer["error"]["code"] == protocol.BAD_REQUEST
    assert await asyncio.wait_for(conn.reader.read(), 2.0) == b""  # then closed


async def test_concurrent_clients(server: ControlServer) -> None:
    conns = [await Conn.open(server.path) for _ in range(8)]
    for conn in conns:
        await conn.recv()
    for i, conn in enumerate(conns):
        await conn.send(protocol.encode({"id": i, "op": "settings.get"}))
    answers = await asyncio.gather(*(conn.answer(i) for i, conn in enumerate(conns)))
    assert all(a["ok"] and a["result"]["tts"]["engine"] == "fake" for a in answers)
    for conn in conns:
        await conn.close()


async def test_settings_event_fans_out(server: ControlServer) -> None:
    a, b = await Conn.open(server.path), await Conn.open(server.path)
    await a.recv()
    await b.recv()
    await a.send(b'{"id": 1, "op": "settings.set", "args": {"tts.voice": "am_adam"}}\n')
    answer = await a.answer(1)
    assert answer["result"]["restart_required"] == ["tts"]
    for conn in (a, b):
        settings = await conn.event("settings")
        assert settings["data"]["tts"]["voice"] == "am_adam"
        assert "restart_required" not in settings["data"]
    await a.close()
    await b.close()


async def test_invalid_setting_error_names_the_field(server: ControlServer) -> None:
    conn = await Conn.open(server.path)
    await conn.recv()
    await conn.send(b'{"id": 1, "op": "settings.set", "args": {"tts.speed": -1}}\n')
    err = (await conn.answer(1))["error"]
    assert err["code"] == protocol.INVALID_SETTING and err["field"] == "tts.speed"
    await conn.close()


async def test_status_events_on_change_and_periodically(
    server: ControlServer, supervisor: FakeSupervisor
) -> None:
    conn = await Conn.open(server.path)
    await conn.recv()
    first = await conn.event("status")
    assert first["data"]["services"]["tts"]["state"] == "running"
    supervisor.state = "starting"
    changed = await conn.event("status")
    assert changed["data"]["services"]["tts"]["state"] == "starting"
    periodic = await conn.event("status", wait_s=1.0)  # unchanged, pushed after push_max
    assert periodic["data"]["services"]["tts"]["state"] == "starting"
    await conn.close()


async def test_restart_answers_then_pushes_status(
    server: ControlServer, supervisor: FakeSupervisor
) -> None:
    conn = await Conn.open(server.path)
    await conn.recv()
    await conn.send(b'{"id": 1, "op": "service.restart", "args": {"service": "all"}}\n')
    assert (await conn.answer(1))["result"]["accepted"] is True
    assert supervisor.restarted == ["stt", "tts"]
    status = await conn.event("status")
    assert status["data"]["services"]["stt"]["restarts"] == 2
    await conn.close()


async def test_disconnected_client_does_not_break_fan_out(server: ControlServer) -> None:
    gone, stays = await Conn.open(server.path), await Conn.open(server.path)
    await gone.recv()
    await stays.recv()
    gone.writer.transport.abort()
    await stays.send(b'{"id": 1, "op": "settings.set", "args": {"zeroconf": false}}\n')
    assert (await stays.answer(1))["ok"] is True
    assert (await stays.event("settings"))["data"]["zeroconf"] is False
    await stays.close()


# The blocking client used by the CLI ------------------------------------------


async def test_blocking_client(server: ControlServer) -> None:
    def use() -> tuple[dict[str, Any], dict[str, Any]]:
        with ControlClient.connect(server.path, timeout_s=5) as client:
            assert client.hello["protocol"] == 1
            got = client.request("settings.get")
            try:
                client.request("nope")
            except ControlError as exc:
                assert exc.code == protocol.UNKNOWN_OP
            else:
                raise AssertionError("expected ControlError")
            return got, client.next_event()

    result, event = await asyncio.to_thread(use)
    assert result["bind"] == "loopback"
    assert event["event"] in ("status", "settings")


def test_client_without_server(short_dir: Path) -> None:
    with pytest.raises(ControlUnavailable):
        ControlClient.connect(short_dir / "control.sock", timeout_s=1)
