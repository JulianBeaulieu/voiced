"""Framing and dispatch, without a socket."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from voiced.control import protocol
from voiced.control.ops import Operations
from voiced.control.protocol import OpError, parse_request

from .conftest import FakeSupervisor


def read(path: Path) -> str:
    return path.read_text()


def test_encode_is_one_line() -> None:
    line = protocol.encode({"event": "x", "data": {"text": "a\nb", "voice": "Zoe"}})
    assert line.endswith(b"\n") and line.count(b"\n") == 1
    assert json.loads(line) == {"event": "x", "data": {"text": "a\nb", "voice": "Zoe"}}


def test_hello() -> None:
    assert protocol.hello("1.2.3") == {
        "event": "hello",
        "data": {"protocol": protocol.PROTOCOL_VERSION, "version": "1.2.3"},
    }


def test_parse_request() -> None:
    req = parse_request(b'{"id": 7, "op": "status"}\n')
    assert (req.id, req.op, req.args) == (7, "status", {})
    req = parse_request(b'{"id": 1, "op": "logs.tail", "args": {"lines": 3}, "extra": 1}')
    assert req.args == {"lines": 3}


@pytest.mark.parametrize(
    ("line", "req_id"),
    [
        (b"not json", None),
        (b"[1, 2]", None),
        (b'{"op": "status"}', None),
        (b'{"id": "1", "op": "status"}', None),
        (b'{"id": true, "op": "status"}', None),
        (b'{"id": 4}', 4),
        (b'{"id": 5, "op": "status", "args": [1]}', 5),
        (b"\xff\xfe", None),
    ],
)
def test_parse_request_rejects(line: bytes, req_id: int | None) -> None:
    with pytest.raises(OpError) as info:
        parse_request(line)
    assert info.value.code == protocol.BAD_REQUEST
    assert info.value.extra.get("id") == req_id


async def test_dispatch_unknown_op(ops: Operations) -> None:
    with pytest.raises(OpError) as info:
        await ops.dispatch("nope", {})
    assert info.value.code == protocol.UNKNOWN_OP


async def test_dispatch_status(ops: Operations) -> None:
    result = await ops.dispatch("status", {})
    assert result["installed"] is True
    assert set(result["services"]) == {"stt", "tts", "watchdog"}


async def test_dispatch_internal_error(ops: Operations, supervisor: FakeSupervisor) -> None:
    def boom() -> dict[str, object]:
        raise RuntimeError("bug")

    supervisor.status = boom  # type: ignore[method-assign]
    with pytest.raises(OpError) as info:
        await ops.dispatch("status", {})
    assert info.value.code == protocol.INTERNAL


async def test_restart(ops: Operations, supervisor: FakeSupervisor) -> None:
    assert (await ops.dispatch("service.restart", {"service": "tts"}))["accepted"] is True
    await ops.dispatch("service.restart", {"service": "all"})
    assert supervisor.restarted == ["tts", "stt", "tts"]
    for bad in ({}, {"service": "watchdog"}, {"service": 1}):
        with pytest.raises(OpError) as info:
            await ops.dispatch("service.restart", bad)
        assert info.value.code == protocol.BAD_REQUEST


async def test_restart_not_installed(ops: Operations, supervisor: FakeSupervisor) -> None:
    supervisor.installed = False
    with pytest.raises(OpError) as info:
        await ops.dispatch("service.restart", {"service": "stt"})
    assert info.value.code == protocol.NOT_INSTALLED


async def test_logs_tail(ops: Operations, config_file: Path) -> None:
    from voiced.config import load_settings

    log_dir = load_settings(config_file).server.log_dir
    log_dir.mkdir(parents=True)
    (log_dir / "tts.log").write_text("".join(f"line {i}\n" for i in range(50)))
    result = await ops.dispatch("logs.tail", {"service": "tts", "lines": 3})
    assert result["lines"] == ["line 47", "line 48", "line 49"]
    assert (await ops.dispatch("logs.tail", {"service": "stt"}))["lines"] == []
    for bad in (
        {"service": "x"},
        {"service": "tts", "lines": 1001},
        {"service": "tts", "lines": 0},
    ):
        with pytest.raises(OpError) as info:
            await ops.dispatch("logs.tail", bad)
        assert info.value.code == protocol.BAD_REQUEST


async def test_open(ops: Operations, config_file: Path) -> None:
    assert (await ops.dispatch("open", {"what": "config_file"}))["target"] == str(config_file)
    page = (await ops.dispatch("open", {"what": "status_page"}))["target"]
    assert page == "http://127.0.0.1:8880/"
    stt = await ops.dispatch("open", {"what": "status_page", "service": "stt"})
    assert stt["target"] == "http://127.0.0.1:8881/"
    assert (await ops.dispatch("open", {"what": "logs_dir"}))["target"].endswith("logs")
    with pytest.raises(OpError):
        await ops.dispatch("open", {"what": "terminal"})


async def test_settings_get_never_returns_the_key(ops: Operations, config_file: Path) -> None:
    await ops.dispatch("settings.set", {"api_key": "hunter2"})
    result = await ops.dispatch("settings.get", {})
    assert result["api_key_set"] is True
    assert "hunter2" not in json.dumps(result)
    assert "hunter2" in read(config_file)


async def test_settings_set_result(ops: Operations, supervisor: FakeSupervisor) -> None:
    result = await ops.dispatch("settings.set", {"tts.voice": "bf_emma", "tts.speed": 1.5})
    assert result["tts"]["voice"] == "bf_emma"
    assert result["tts"]["speed"] == 1.5
    assert result["restart_required"] == ["tts"]
    assert result["warnings"] == []
    assert len(supervisor.changed) == 1
    again = await ops.dispatch("settings.set", {"tts.voice": "bf_emma"})
    assert again["restart_required"] == []
    assert len(supervisor.changed) == 1  # nothing changed, nobody told


@pytest.mark.parametrize(
    ("args", "field"),
    [
        ({"tts.speed": 0}, "tts.speed"),
        ({"tts.speed": "fast"}, "tts.speed"),
        ({"bind": "0.0.0.0"}, "bind"),
        ({"zeroconf": "yes"}, "zeroconf"),
        ({"tts.voice": "nobody"}, "tts.voice"),
        ({"tts.engine": "missing"}, "tts.engine"),
        ({"stt.engine": "missing"}, "stt.engine"),
        ({"stt.model": "  "}, "stt.model"),
        ({"log_level": "debug"}, "log_level"),
    ],
)
async def test_settings_set_invalid(
    ops: Operations, config_file: Path, args: dict[str, object], field: str
) -> None:
    before = read(config_file)
    with pytest.raises(OpError) as info:
        await ops.dispatch("settings.set", args)
    assert info.value.code == protocol.INVALID_SETTING
    assert info.value.extra["field"] == field
    assert read(config_file) == before


async def test_settings_set_empty(ops: Operations) -> None:
    with pytest.raises(OpError) as info:
        await ops.dispatch("settings.set", {})
    assert info.value.code == protocol.BAD_REQUEST


async def test_engines_list(ops: Operations) -> None:
    result = await ops.dispatch("engines.list", {})
    tts = {e["name"]: e for e in result["tts"]}
    stt = {e["name"]: e for e in result["stt"]}
    assert {"fake", "kokoro", "soprano"} <= set(tts)
    assert {"fake", "parakeet"} <= set(stt)
    assert tts["fake"]["voices"] == ["af_heart", "am_adam", "bf_emma"]
    assert tts["soprano"]["voices"] == ["default"]
    assert "en" in stt["parakeet"]["languages"]
    assert stt["parakeet"]["models"] == ["mlx-community/parakeet-tdt-0.6b-v3"]


async def test_concurrent_sets_do_not_lose_writes(ops: Operations, config_file: Path) -> None:
    import asyncio
    import tomllib

    await asyncio.gather(
        ops.dispatch("settings.set", {"tts.speed": 1.5}),
        ops.dispatch("settings.set", {"stt.model": "a/b"}),
        ops.dispatch("settings.set", {"zeroconf": False}),
        ops.dispatch("settings.set", {"tts.voice": "am_adam"}),
    )
    data = tomllib.loads(read(config_file))
    assert data["tts"]["speed"] == 1.5 and data["tts"]["default_voice"] == "am_adam"
    assert data["stt"]["model"] == "a/b" and data["server"]["zeroconf"] is False
