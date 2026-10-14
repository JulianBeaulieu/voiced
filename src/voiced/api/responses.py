"""Response classes shared by the route modules."""

from __future__ import annotations

import logging

from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from voiced.inference import DISCONNECT_ERRORS

log = logging.getLogger(__name__)


class ClosingStreamingResponse(StreamingResponse):
    """A ``StreamingResponse`` that always closes its body generator.

    Under ASGI spec 2.4 Starlette leaves the ``async for`` through
    ``ClientDisconnect`` without ``aclose()``, which would leave the inference
    slot (and any ffmpeg pipe) to the garbage collector. This closes it
    deterministically, whatever happened.

    A client that hangs up mid-stream is not an error: the headers are sent,
    there is no one left to answer, and ``inference()`` has already counted
    the disconnect. The exception stops here, so neither the app's catch-all
    handler nor the server logs a traceback for it.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        except DISCONNECT_ERRORS as exc:
            log.debug("client went away mid-stream: %r", exc)
        finally:
            aclose = getattr(self.body_iterator, "aclose", None)
            if aclose is not None:
                await aclose()
