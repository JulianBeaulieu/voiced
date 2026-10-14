"""Framing, without a socket."""

from __future__ import annotations

import json

import pytest

from voiced.control import protocol
from voiced.control.protocol import OpError, parse_request


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
