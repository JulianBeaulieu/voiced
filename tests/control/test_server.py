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


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def server(short_dir: Path, ops: Operations, clock: Clock) -> AsyncIterator[ControlServer]:
    """Polls only when woken (a connect, a restart); tests step the rest with
    :meth:`ControlServer.poll_once` and ``clock``."""
    srv = ControlServer(
        short_dir / "state" / "control.sock",
        ops,
        version="9.9.9",
        status_poll_s=3600,
        status_push_max_s=10,
        clock=clock,
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
    hello = {"protocol": 1, "version": "9.9.9", "install": None}
    assert await conn.recv() == {"event": "hello", "data": hello}
    assert stat.S_IMODE(server.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(server.path.parent.stat().st_mode) == 0o700
    await conn.send(b'{"id": 1, "op": "status"}\n')
    answer = await conn.answer(1)
    assert answer["ok"] is True and answer["result"]["installed"] is True
    await conn.close()


async def test_hello_reads_the_manifest_for_each_connection(
    short_dir: Path, ops: Operations
) -> None:
    """`voiced install` writes install.json after repointing the link, so a
    watchdog that already followed the link must not keep what it read at
    start: the app waits for a hello with the new lock hash."""
    manifests: list[dict[str, Any] | None] = [None, {"source": "app", "lock_sha256": "new"}]
    srv = ControlServer(short_dir / "control.sock", ops, version="x", install=lambda: manifests[0])
    async with srv:
        first = await Conn.open(srv.path)
        assert (await first.recv())["data"]["install"] is None
        manifests.pop(0)
        second = await Conn.open(srv.path)
        assert (await second.recv())["data"]["install"] == {"source": "app", "lock_sha256": "new"}
        await first.close()
        await second.close()


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
    """After a settings.set that wrote the file, every client hears of it."""
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


async def test_settings_event_when_another_writer_changes_the_file(
    server: ControlServer, config_file: Path
) -> None:
    """The status page form (or an editor) writes the file; the next poll
    tells socket clients."""
    conn = await Conn.open(server.path)
    await conn.recv()
    text = await asyncio.to_thread(config_file.read_text)
    edited = text.replace("[server]\n", "[server]\nzeroconf = false\n")
    await asyncio.to_thread(config_file.write_text, edited)
    await server.poll_once()
    assert (await conn.event("settings"))["data"]["zeroconf"] is False
    await conn.close()


async def test_invalid_setting_error_names_the_field(server: ControlServer) -> None:
    conn = await Conn.open(server.path)
    await conn.recv()
    await conn.send(b'{"id": 1, "op": "settings.set", "args": {"tts.speed": -1}}\n')
    err = (await conn.answer(1))["error"]
    assert err["code"] == protocol.INVALID_SETTING and err["field"] == "tts.speed"
    await conn.close()


async def next_message(conn: Conn, req_id: int) -> dict[str, Any]:
    """The first message after asking for something: proves no event was
    queued before the answer."""
    await conn.send(protocol.encode({"id": req_id, "op": "settings.get"}))
    return await conn.recv()


async def test_status_events_on_change_and_periodically(
    server: ControlServer, supervisor: FakeSupervisor, clock: Clock
) -> None:
    conn = await Conn.open(server.path)
    await conn.recv()
    first = await conn.event("status")  # on connect
    assert first["data"]["services"]["tts"]["state"] == "running"
    await server.poll_once()  # unchanged, not due: nothing
    assert (await next_message(conn, 1)).get("id") == 1
    supervisor.state = "starting"
    await server.poll_once()
    changed = await conn.recv()
    assert changed["event"] == "status"
    assert changed["data"]["services"]["tts"]["state"] == "starting"
    clock.now += server.status_push_max_s
    await server.poll_once()  # unchanged but due
    periodic = await conn.recv()
    assert periodic["event"] == "status"
    assert periodic["data"]["services"]["tts"]["state"] == "starting"
    await conn.close()


async def test_restart_answers_then_pushes_status(
    server: ControlServer, supervisor: FakeSupervisor
) -> None:
    conn = await Conn.open(server.path)
    await conn.recv()
    await conn.send(b'{"id": 1, "op": "service.restart", "args": {"service": "all"}}\n')
    assert (await conn.answer(1))["result"]["accepted"] is True
    await server.ops.wait_restarts()
    assert sorted(supervisor.restarted) == ["stt", "tts"]
    await server.poll_once()
    while True:
        status = await conn.event("status")
        if status["data"]["services"]["stt"]["restarts"] == 2:
            break
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


async def stuck_client(server: ControlServer, config_file: Path, requests: int = 8) -> Conn:
    """A client that asks for large log tails and never reads the answers,
    so the server's writes to it stop going through."""
    from voiced.config import load_settings

    log_dir = load_settings(config_file).server.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "tts.log").write_text("".join(f"{i:04d} {'x' * 400}\n" for i in range(1000)))
    conn = await Conn.open(server.path)
    for i in range(requests):
        await conn.send(
            protocol.encode({"id": i, "op": "logs.tail", "args": {"service": "tts", "lines": 1000}})
        )
    await asyncio.sleep(0.3)  # its replies fill the socket buffers
    return conn


async def test_a_stuck_client_does_not_hold_up_the_others(
    server: ControlServer, config_file: Path
) -> None:
    """Another client is answered, and a status event to both is queued,
    while the stuck client's replies still wait. A server that waited on the
    stuck client's write as long as its sender does, up to the write timeout,
    could only go on once that write ended: it never completes, so it would
    end by dropping the client. A shorter wait that keeps the client passes."""
    stuck = await stuck_client(server, config_file)
    (stuck_state,) = server.clients
    assert stuck_state.queued_bytes > 0
    good = await Conn.open(server.path)
    await good.recv()
    for i, (op, args) in enumerate(
        [("settings.set", {"zeroconf": False}), ("settings.get", {}), ("status", {})]
    ):
        await good.send(protocol.encode({"id": 100 + i, "op": op, "args": args}))
        assert (await good.answer(100 + i))["ok"] is True, op
    await server.poll_once()  # queues a status event for both
    assert stuck_state in server.clients and stuck_state.queued_bytes > 0
    await good.close()
    stuck.writer.transport.abort()


async def test_a_client_that_never_reads_holds_little_memory(
    server: ControlServer, config_file: Path
) -> None:
    """60 large log tails from a client that never reads: the server stops
    reading its requests once about 1 MiB waits for it, so what is queued
    stays small, and other clients are still answered."""
    from voiced.control import server as server_mod

    stuck = await stuck_client(server, config_file, requests=60)
    await asyncio.sleep(0.5)
    (client,) = server.clients
    assert client.queued_bytes <= server_mod.READ_PAUSE_BYTES + 512 * 1024
    good = await Conn.open(server.path)
    await good.recv()
    await good.send(b'{"id": 1, "op": "settings.get"}\n')
    assert (await good.answer(1))["ok"] is True
    await good.close()
    stuck.writer.transport.abort()


async def test_a_client_over_the_byte_cap_is_dropped(server: ControlServer) -> None:
    from voiced.control import server as server_mod

    conn = await Conn.open(server.path)
    await conn.recv()  # hello; nothing is read after it
    big = protocol.event("status", {"pad": "x" * (1 << 20)})
    for _ in range(server_mod.SEND_QUEUE_BYTES // (1 << 20) + 4):
        server.broadcast(big)
    assert not server.clients
    conn.writer.transport.abort()


async def test_a_client_whose_queue_fills_is_dropped(server: ControlServer) -> None:
    conn = await Conn.open(server.path)
    await conn.recv()  # hello; nothing is read after it
    assert len(server.clients) == 1
    for _ in range(1000):
        if not server.clients:
            break
        server.broadcast(protocol.event("status", {"pad": "x" * 65536}))
        await asyncio.sleep(0)
    assert not server.clients
    conn.writer.transport.abort()


# The blocking client used by the CLI ------------------------------------------


async def test_blocking_client(server: ControlServer) -> None:
    def use() -> dict[str, Any]:
        with ControlClient.connect(server.path, timeout_s=5) as client:
            assert client.hello["protocol"] == 1
            got = client.request("settings.get")
            try:
                client.request("nope")
            except ControlError as exc:
                assert exc.code == protocol.UNKNOWN_OP
            else:
                raise AssertionError("expected ControlError")
            assert all("event" in e for e in client.events)
            return got

    result = await asyncio.to_thread(use)
    assert result["bind"] == "loopback"


async def test_blocking_client_reads_replies_over_64_kib(
    server: ControlServer, config_file: Path
) -> None:
    """The line limit is for requests; a 1000-line log tail is longer."""
    from voiced.config import load_settings

    log_dir = load_settings(config_file).server.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "tts.log").write_text("".join(f"{i:04d} {'x' * 400}\n" for i in range(1000)))

    def use() -> dict[str, Any]:
        with ControlClient.connect(server.path, timeout_s=5) as client:
            return client.request("logs.tail", {"service": "tts", "lines": 1000})

    result = await asyncio.to_thread(use)
    assert len(result["lines"]) == 1000
    assert len(protocol.encode(result)) > protocol.MAX_LINE_BYTES


def test_client_without_server(short_dir: Path) -> None:
    with pytest.raises(ControlUnavailable):
        ControlClient.connect(short_dir / "control.sock", timeout_s=1)


class SlowSpeech:
    """A ``tts.speak`` that waits until released, and notes a cancel."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def __call__(self, text: str, voice: str) -> bytes:
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return b"RIFF"


async def test_a_waiting_speak_does_not_hold_up_the_clients_other_requests(
    server: ControlServer,
) -> None:
    speech = SlowSpeech()
    server.ops.speak = speech
    conn = await Conn.open(server.path)
    await conn.recv()
    await conn.send(b'{"id": 1, "op": "tts.speak", "args": {"text": "Hi.", "voice": "a"}}\n')
    await asyncio.wait_for(speech.started.wait(), 2)
    await conn.send(b'{"id": 2, "op": "settings.get"}\n')
    assert (await conn.answer(2))["ok"] is True  # while the audio is still coming
    speech.release.set()
    assert (await conn.answer(1))["result"]["audio"] == "UklGRg=="
    await conn.close()


async def test_a_client_that_goes_away_cancels_its_speak(server: ControlServer) -> None:
    speech = SlowSpeech()
    server.ops.speak = speech
    conn = await Conn.open(server.path)
    await conn.recv()
    await conn.send(b'{"id": 1, "op": "tts.speak", "args": {"text": "Hi.", "voice": "a"}}\n')
    await asyncio.wait_for(speech.started.wait(), 2)
    await conn.close()  # the app's stop: it closes the preview's connection
    await asyncio.wait_for(speech.cancelled.wait(), 2)
