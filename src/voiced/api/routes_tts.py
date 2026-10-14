"""``/v1/audio/speech`` and ``/v1/audio/voices`` routes. Stubs until the API
worker fills them in.

Planned (see HANDOFF section 5):

* ``POST /v1/audio/speech``: JSON ``{model, input, voice, speed,
  response_format}`` with wav, pcm, mp3, opus. ``pcm`` streams chunks from
  ``ctx.tts_engine.synthesize`` inside ``ctx.queue.slot()``; the others
  collect and encode.
* ``GET /v1/audio/voices``: ``ctx.tts_engine.voices()``.
"""

from __future__ import annotations

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/v1/audio", tags=["tts"])


def _stub(what: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        content={"error": {"message": f"{what} is not implemented yet", "type": "not_implemented"}},
    )


@router.post("/speech", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def speech() -> JSONResponse:
    return _stub("speech synthesis")


@router.get("/voices", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def voices() -> JSONResponse:
    return _stub("voice listing")
