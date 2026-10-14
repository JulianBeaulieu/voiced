"""``/v1/audio/transcriptions`` routes. Stubs until the API worker fills them in.

Planned (see HANDOFF section 5):

* ``POST /v1/audio/transcriptions``: multipart ``file``, ``model``,
  ``language``, ``response_format`` in json, text, verbose_json, srt, vtt.
  Decode with ffmpeg to 16 kHz float32 mono, then ``ctx.queue.run`` around
  ``ctx.stt_engine.transcribe``.
* ``WS /v1/audio/transcriptions/stream``: live streaming with partial and
  final events over an ``STTStreamSession`` held inside ``ctx.queue.slot()``.
"""

from __future__ import annotations

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/v1/audio", tags=["stt"])


@router.post("/transcriptions", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def transcriptions() -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        content={
            "error": {"message": "transcription is not implemented yet", "type": "not_implemented"}
        },
    )
