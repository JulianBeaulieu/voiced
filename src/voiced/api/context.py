"""Request-level helpers shared by every module in ``voiced.api``.

Route modules import from here (and ``ServiceContext`` from
``voiced.context``), never from ``voiced.api.app``, so the app factory can
import the routes at module level without a cycle.

Error responses on ``/v1`` routes share one JSON shape, which is also what
the OpenAI clients parse into their exceptions::

    {"error": {"message": "...", "type": "..."}}
"""

from __future__ import annotations

import hmac
from contextlib import suppress
from typing import Annotated

from fastapi import Depends, status
from starlette.requests import HTTPConnection
from starlette.websockets import WebSocket, WebSocketDisconnect

from voiced.context import ServiceContext
from voiced.metrics import Outcome

WS_CLOSE: dict[Outcome, int] = {
    Outcome.BUSY: status.WS_1013_TRY_AGAIN_LATER,
    Outcome.CLIENT_ERROR: status.WS_1008_POLICY_VIOLATION,
    Outcome.DISCONNECT: status.WS_1001_GOING_AWAY,  # only if the socket is still open
    Outcome.FAILURE: status.WS_1011_INTERNAL_ERROR,
}
"""WebSocket close code per outcome, for the handshake and in-session alike."""


async def close_ws(ws: WebSocket, code: int, reason: str = "") -> None:
    """Close ``ws`` with ``code``, unless the connection is already gone.

    The reason is cut to the protocol's 123-byte limit. ``RuntimeError`` means
    the socket was already closed; ``WebSocketDisconnect`` is Starlette's
    translation of the server's send error after the peer dropped the TCP
    connection. Neither is news, and letting either escape makes the server
    log a traceback.
    """
    with suppress(RuntimeError, WebSocketDisconnect):
        await ws.close(code, reason.encode()[:123].decode(errors="ignore"))


def get_context(conn: HTTPConnection) -> ServiceContext:
    """Works for HTTP requests and WebSocket connections alike."""
    ctx: ServiceContext = conn.app.state.ctx
    return ctx


Ctx = Annotated[ServiceContext, Depends(get_context)]
"""Route parameter annotation: ``async def route(ctx: Ctx) -> ...``."""


class APIError(Exception):
    """A request-level error with an explicit HTTP status.

    Routes raise this for anything the client did wrong that is not an
    :class:`~voiced.engines.base.EngineError`: bad parameters (400), oversize
    input (413), missing credentials (401). ``type`` is a stable
    machine-readable tag. Raise it only outside an ``inference()`` block;
    inside one, client mistakes must be ``EngineError`` subclasses.

    On a WebSocket it closes the socket with :data:`WS_CLOSE` (a 4xx is a
    client error, 503 is busy, any other 5xx a failure); a close before
    ``accept()`` reaches the client as HTTP 403.
    """

    def __init__(self, status: int, message: str, type: str = "invalid_request_error") -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.type = type


def require_api_key(conn: HTTPConnection, ctx: Ctx) -> None:
    """Bearer auth for ``/v1`` routes and WebSocket handshakes when ``server.api_key`` is set."""
    expected = ctx.settings.server.api_key
    if not expected:
        return
    scheme, _, token = conn.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), expected.encode()):
        raise APIError(status.HTTP_401_UNAUTHORIZED, "invalid or missing API key", "auth_error")


def require_loaded(ctx: Ctx) -> None:
    """503 until the engine has loaded (``ServiceContext.check_loaded``)."""
    ctx.check_loaded()
