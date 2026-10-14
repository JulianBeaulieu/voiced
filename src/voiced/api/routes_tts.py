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

The body is an async generator that runs ``inference()`` around the engine
call; the route primes it with one ``anext()`` so a full queue is still a
503 and an unknown voice still a 400 before any header is sent (ADR 0005).
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import aclosing
from typing import Any, Literal, cast

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, Field

from voiced import audio as pcm
from voiced.api import audio
from voiced.api.context import APIError, Ctx
from voiced.api.foreign_models import ForeignModels, reject_foreign_model
from voiced.api.responses import ClosingStreamingResponse
from voiced.context import ServiceContext
from voiced.engines.base import TTS_SAMPLE_RATE, PCMChunk
from voiced.inference import inference
from voiced.metrics import Operation

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/audio", tags=["tts"])


def _load_normalizer() -> Callable[[str], str]:
    """``voiced.text.normalize`` if that module exists, else identity.

    The text pipeline is written by the TTS engine workstream; this guarded
    import lets the HTTP layer work on its own and pick the normalizer up once
    it lands.
    """
    try:
        module = importlib.import_module("voiced.text")
    except ImportError:
        return lambda text: text
    fn = getattr(module, "normalize", None)
    if not callable(fn):
        return lambda text: text
    return cast(Callable[[str], str], fn)


normalize_text: Callable[[str], str] = _load_normalizer()


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
            "content": {media: {} for media in audio.MEDIA_TYPES.values()},
        },
        400: {"description": "Bad parameters or unknown voice"},
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
    if body.response_format in ("mp3", "opus") and not audio.ffmpeg_available():
        raise APIError(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"ffmpeg is required for {body.response_format} output (brew install ffmpeg)",
            type="ffmpeg_missing",
        )

    voice = body.voice or settings.default_voice
    speed = body.speed if body.speed is not None else settings.speed
    text = normalize_text(body.input)
    fmt = body.response_format

    # Prime the body so QueueFull and a pre-audio EngineError surface as
    # 503 / 400 through the app's handlers instead of a broken stream.
    body_pcm = _synthesize(ctx, text, voice, speed)
    first = await anext(body_pcm)
    stream: AsyncIterator[bytes] = _chain(first, body_pcm)
    if fmt == "wav":
        stream = _chain(pcm.wav_header(TTS_SAMPLE_RATE), stream)
    elif fmt in ("mp3", "opus"):
        stream = audio.encode_stream(stream, fmt, TTS_SAMPLE_RATE)
    return ClosingStreamingResponse(
        stream, media_type=audio.MEDIA_TYPES[fmt], headers={"Cache-Control": "no-store"}
    )


async def _synthesize(
    ctx: ServiceContext, text: str, voice: str, speed: float
) -> AsyncGenerator[bytes, None]:
    """PCM bytes from the engine, inside one ``inference()`` block.

    Yields ``b""`` once the first chunk is in hand (that is the priming
    value), then the audio. Closing the generator early (client disconnect)
    passes ``GeneratorExit`` through ``inference()`` and frees the slot.
    """
    async with inference(ctx, Operation.SPEECH):
        # base.py requires synthesize() to be an async generator.
        chunks = cast(AsyncGenerator[PCMChunk, None], ctx.tts_engine.synthesize(text, voice, speed))
        async with aclosing(chunks):
            first = await anext(chunks, None)
            yield b""
            if first is not None:
                yield first.data
                async for chunk in chunks:
                    yield chunk.data


async def _chain(head: bytes, rest: AsyncIterator[bytes]) -> AsyncGenerator[bytes, None]:
    async with aclosing(cast(AsyncGenerator[bytes, None], rest)):
        if head:
            yield head
        async for chunk in rest:
            yield chunk
