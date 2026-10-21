"""Response classes shared by the route modules."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterable, Mapping
from typing import Any

from fastapi.responses import Response, StreamingResponse
from starlette.types import Message, Receive, Scope, Send

from voiced.inference import DISCONNECT_ERRORS, bounded

log = logging.getLogger(__name__)

CLIENT_CLOSED_REQUEST = 499
"""Status for a request the client abandoned (nginx's convention); never delivered."""


class ClientGoneResponse(Response):
    """What a route returns when its client hung up before the response began.

    Returning it, rather than raising, keeps the server from logging a
    traceback for a hang-up. There is no one to deliver it to: a server on
    ASGI spec 2.4 raises ``OSError`` from ``send`` on a closed connection,
    which stops here (uvicorn on 2.3 drops the messages).
    """

    def __init__(self) -> None:
        super().__init__(status_code=CLIENT_CLOSED_REQUEST)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        except OSError as exc:
            log.debug("no client to answer: %r", exc)


class ClosingStreamingResponse(StreamingResponse):
    """A ``StreamingResponse`` that always closes its body generator.

    Under ASGI spec 2.4 Starlette leaves the ``async for`` through
    ``ClientDisconnect`` without ``aclose()``, which would leave the inference
    slot (and any ffmpeg pipe) to the garbage collector. This closes it
    deterministically, whatever happened.

    Every ``send`` is bounded by ``write_timeout_s`` (``server.write_timeout_s``),
    so a client that stops reading is treated as gone instead of holding the
    response open indefinitely.

    A client that hangs up mid-stream is not an error: the headers are sent
    and there is no one left to answer. The exception stops here, so neither
    the app's catch-all handler nor the server logs a traceback for it. The
    route counts the disconnect, not this class: closing the body ends the
    synthesis, which ``inference()`` records as a disconnect.
    """

    def __init__(
        self,
        content: AsyncIterable[bytes],
        *,
        write_timeout_s: float,
        status_code: int = 200,
        media_type: str | None = None,
        headers: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        # status_code stays an explicit parameter: FastAPI reads its default
        # from this signature to document the route's success response, and
        # fails to build /openapi.json when it is hidden in **kwargs.
        super().__init__(
            content, status_code=status_code, media_type=media_type, headers=headers, **kwargs
        )
        self.write_timeout_s = write_timeout_s

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        timeout = self.write_timeout_s

        async def bounded_send(message: Message) -> None:
            # A client that stops reading must not keep this task, its queued
            # audio and any ffmpeg process alive: after server.write_timeout_s
            # the send raises ClientTimeout (a ConnectionError), handled below.
            await bounded(send(message), timeout)

        try:
            await super().__call__(scope, receive, bounded_send)
        except DISCONNECT_ERRORS as exc:
            log.debug("client went away mid-stream: %r", exc)
        finally:
            aclose = getattr(self.body_iterator, "aclose", None)
            if aclose is not None:
                await aclose()
