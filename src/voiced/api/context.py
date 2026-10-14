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
from typing import Annotated

from fastapi import Depends, status
from starlette.requests import HTTPConnection

from voiced.context import ServiceContext


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

    During a WebSocket handshake it closes the socket instead (1008 for 4xx,
    1013 for 503, 1011 for other 5xx); a close before ``accept()`` reaches the
    client as HTTP 403.
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
