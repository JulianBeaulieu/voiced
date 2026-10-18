"""Wire format of the control channel.

One JSON object per line, UTF-8. Requests are
``{"id": <int>, "op": "<name>", "args": {...}}``; answers are
``{"id": <same>, "ok": true, "result": {...}}`` or
``{"id": <same>, "ok": false, "error": {"code": ..., "message": ...}}``;
events have no id: ``{"event": "<name>", "data": {...}}``. The first line a
client receives is the ``hello`` event.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Final

PROTOCOL_VERSION: Final = 1
MAX_LINE_BYTES: Final = 64 * 1024

# Error codes.
NOT_INSTALLED: Final = "not_installed"
INVALID_SETTING: Final = "invalid_setting"
UNKNOWN_OP: Final = "unknown_op"
BAD_REQUEST: Final = "bad_request"
SERVICE_UNAVAILABLE: Final = "service_unavailable"
INTERNAL: Final = "internal"


class OpError(Exception):
    """Raised by an operation; becomes an ``ok: false`` answer."""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


@dataclass(frozen=True)
class Request:
    id: int
    op: str
    args: dict[str, Any] = field(default_factory=dict)


def encode(message: dict[str, Any]) -> bytes:
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"


def parse_request(line: bytes) -> Request:
    """Parse one request line. Raises :class:`OpError` (``bad_request``)
    with ``extra["id"]`` set when the id could be read."""
    try:
        message = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise OpError(BAD_REQUEST, "not a JSON object") from None
    if not isinstance(message, dict):
        raise OpError(BAD_REQUEST, "not a JSON object")
    req_id = message.get("id")
    if isinstance(req_id, bool) or not isinstance(req_id, int):
        raise OpError(BAD_REQUEST, '"id" must be an integer')
    op = message.get("op")
    if not isinstance(op, str) or not op:
        raise OpError(BAD_REQUEST, '"op" must be a string', id=req_id)
    args = message.get("args", {})
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise OpError(BAD_REQUEST, '"args" must be an object', id=req_id)
    return Request(req_id, op, args)


def ok(req_id: int, result: dict[str, Any]) -> dict[str, Any]:
    return {"id": req_id, "ok": True, "result": result}


def error(req_id: int | None, code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"id": req_id, "ok": False, "error": {"code": code, "message": message, **extra}}


def event(name: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"event": name, "data": data}


def hello(version: str, install: dict[str, Any] | None = None) -> dict[str, Any]:
    """``install`` is what ``install.json`` records (ADR 0017), None without
    one; added within protocol 1, which ignores unknown fields."""
    data = {"protocol": PROTOCOL_VERSION, "version": version, "install": install}
    return event("hello", data)
