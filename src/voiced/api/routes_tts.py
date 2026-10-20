"""``/v1/audio/speech`` and ``/v1/audio/voices`` routes.

``POST /v1/audio/speech`` takes the OpenAI JSON body ``{model, input, voice,
speed, response_format}``. ``voice`` defaults to ``tts.default_voice`` and
``speed`` to ``tts.speed``; ``model`` is ignored unless it names another
installed engine's model. ``input`` longer than ``tts.max_input_chars`` is
rejected with 413.

Every format streams. ``pcm`` is raw int16 little-endian mono at 24 kHz.
``wav`` is the same PCM behind a 44-byte header whose size fields are
``0xFFFFFFFF`` (length unknown), so the first bytes leave before the last
sentence is synthesized. ``mp3`` and ``opus`` (Ogg) are encoded by ffmpeg
through a pipe while the engine is still producing audio.

``voice`` should be one of ``/v1/audio/voices``, the engine's voices whose
language is configured; anything else (an OpenAI client's ``alloy``, the
voice of the engine before a switch) is spoken in ``tts.default_voice``, or
the first served voice, with a warning in the log (ADR 0022). Only when no
voice is served at all is it 400 ``UnknownVoice``.

The engine runs inside ``inference()`` in its own task, and the response
body reads its chunks from a queue. The route primes the body with one
``anext()``, so a full queue is still a 503 and an engine error still a 400
before any header is sent (ADR 0005). A client that hangs up while the engine
is still producing stops the body; the synthesis then ends at the next chunk
and is counted as a disconnect. A hang-up after synthesis finished records
``ok``: the audio was produced, only its delivery failed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing
from typing import Any, Literal, cast

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import ClientDisconnect

from voiced import audio
from voiced.api import ffmpeg
from voiced.api.context import APIError, Ctx
from voiced.api.foreign_models import ForeignModels, reject_foreign_model
from voiced.api.responses import ClosingStreamingResponse
from voiced.context import ServiceContext
from voiced.engines.base import TTS_SAMPLE_RATE, PCMChunk
from voiced.inference import inference
from voiced.metrics import Operation
from voiced.text import normalize

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/audio", tags=["tts"])

MEDIA_TYPES: dict[str, str] = {
    "wav": "audio/wav",
    "pcm": "audio/pcm",  # raw int16 LE, 24 kHz mono; what the OpenAI endpoint returns
    "mp3": "audio/mpeg",
    "opus": "audio/ogg",  # Opus in an Ogg container, like the OpenAI endpoint
}


class SpeechRequest(BaseModel):
    """OpenAI ``POST /v1/audio/speech`` body. Unknown fields are ignored."""

    model_config = ConfigDict(extra="ignore")

    model: str | None = None
    input: str
    voice: str | None = None
    speed: float | None = Field(default=None, gt=0, le=4.0)
    response_format: Literal["wav", "pcm", "mp3", "opus"] = "wav"


@router.get("/voices", summary="List voices")
async def voices(ctx: Ctx) -> dict[str, Any]:
    """Voices of the running engine whose language is configured, in the OpenAI
    list envelope."""
    return {
        "object": "list",
        "data": [
            {
                "object": "voice",
                "id": v.id,
                "name": v.name,
                "language": v.language,
                "gender": v.gender,
            }
            for v in await ctx.voices()
        ],
    }


@router.post(
    "/speech",
    summary="Synthesize speech",
    response_class=ClosingStreamingResponse,
    responses={
        200: {
            "description": "Audio stream in response_format",
            "content": {media: {} for media in MEDIA_TYPES.values()},
        },
        400: {"description": "Bad parameters, or a voice that is not served"},
        413: {"description": "input longer than tts.max_input_chars"},
        503: {"description": "Inference queue full; retry later"},
    },
)
async def speech(body: SpeechRequest, ctx: Ctx, foreign: ForeignModels) -> ClosingStreamingResponse:
    settings = ctx.settings.tts
    if not body.input.strip():
        raise APIError(status.HTTP_400_BAD_REQUEST, "input must not be empty")
    if len(body.input) > settings.max_input_chars:
        raise APIError(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"input is {len(body.input)} characters; the limit is {settings.max_input_chars} "
            "(tts.max_input_chars)",
            type="payload_too_large",
        )
    reject_foreign_model(ctx, foreign, body.model)
    fmt = body.response_format
    if fmt in ("mp3", "opus"):
        ffmpeg.require_ffmpeg(f"{fmt} output")

    voice = await ctx.check_voice(body.voice, listed_by="/v1/audio/voices")
    speed = body.speed if body.speed is not None else settings.speed
    text = normalize(body.input, language=voice.language)

    # Prime the body so QueueFull and a pre-audio EngineError surface as
    # 503 / 400 through the app's handlers instead of a broken stream.
    pcm = _synthesize(ctx, text, voice.id, speed)
    await anext(pcm)
    stream: AsyncIterator[bytes] = pcm
    if fmt == "wav":
        stream = _prefixed(audio.wav_header(TTS_SAMPLE_RATE), pcm)
    elif fmt in ("mp3", "opus"):
        stream = ffmpeg.encode_stream(pcm, fmt, TTS_SAMPLE_RATE)
    return ClosingStreamingResponse(
        stream,
        write_timeout_s=ctx.settings.server.write_timeout_s,
        media_type=MEDIA_TYPES[fmt],
        headers={"Cache-Control": "no-store"},
    )


_END = object()
"""Queue marker: synthesis finished."""

_SYNTHESES: set[asyncio.Task[None]] = set()
"""Strong references to running syntheses; asyncio keeps only weak ones."""


async def _synthesize(
    ctx: ServiceContext, text: str, voice: str, speed: float
) -> AsyncGenerator[bytes, None]:
    """The response body: PCM bytes from a synthesis running in its own task.

    Yields ``b""`` once the first result is in hand (the route consumes that
    priming value with ``anext()``, so ``QueueFull`` and a pre-audio
    ``EngineError`` raise before the response starts), then the audio.

    The ``inference()`` block lives in :func:`_produce`, not here. When a
    client hangs up, the server cancels the task streaming this body; if the
    block were in this generator, the ``CancelledError`` would land inside it
    and nothing would be recorded (ADR 0005). Instead, stopping this generator
    for any reason sets ``stop``, and the synthesis ends at the next chunk with
    a disconnect that ``inference()`` counts. Cancellation itself is untouched.
    """
    out: asyncio.Queue[object] = asyncio.Queue()
    stop = asyncio.Event()
    task = asyncio.create_task(_produce(ctx, text, voice, speed, out, stop))
    _SYNTHESES.add(task)
    task.add_done_callback(_SYNTHESES.discard)
    try:
        item = await out.get()
        if isinstance(item, Exception):
            raise item
        yield b""
        while item is not _END:
            yield cast(bytes, item)
            item = await out.get()
            if isinstance(item, Exception):
                raise item
    finally:
        stop.set()


async def _produce(
    ctx: ServiceContext,
    text: str,
    voice: str,
    speed: float,
    out: asyncio.Queue[object],
    stop: asyncio.Event,
) -> None:
    """Run the engine inside ``inference()`` and hand each chunk to the body.

    The queue is unbounded on purpose: synthesis runs at GPU speed and frees
    the slot as soon as it is done, however slowly the client reads (the input
    is bounded by ``tts.max_input_chars``).
    """
    try:
        async with inference(ctx, Operation.SPEECH):
            # base.py requires synthesize() to be an async generator.
            chunks = cast(
                AsyncGenerator[PCMChunk, None], ctx.tts_engine.synthesize(text, voice, speed)
            )
            async with aclosing(chunks):
                async for chunk in chunks:
                    if stop.is_set():
                        raise ClientDisconnect  # the body stopped reading: a disconnect
                    out.put_nowait(chunk.data)
    except Exception as exc:
        # inference() has already recorded the outcome; hand the error to the body.
        out.put_nowait(exc)
    finally:
        # Always, even if this task is cancelled, so the body never waits forever.
        out.put_nowait(_END)


async def _prefixed(head: bytes, rest: AsyncGenerator[bytes, None]) -> AsyncGenerator[bytes, None]:
    """``head`` then ``rest``; closing this generator closes ``rest``."""
    async with aclosing(rest):
        yield head
        async for chunk in rest:
            yield chunk
