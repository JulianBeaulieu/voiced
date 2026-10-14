"""Response classes shared by the route modules."""

from __future__ import annotations

from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send


class ClosingStreamingResponse(StreamingResponse):
    """A ``StreamingResponse`` that always closes its body generator.

    Under ASGI spec 2.4 Starlette leaves the ``async for`` through
    ``ClientDisconnect`` without ``aclose()``, which would leave the inference
    slot (and any ffmpeg pipe) to the garbage collector. This closes it
    deterministically, whatever happened.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            aclose = getattr(self.body_iterator, "aclose", None)
            if aclose is not None:
                await aclose()
