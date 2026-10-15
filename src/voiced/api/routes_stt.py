"""``/v1/audio/transcriptions`` routes: batch upload and WebSocket streaming.

Batch (``POST /v1/audio/transcriptions``)
    Multipart form with ``file`` plus the OpenAI fields ``model`` (ignored
    unless it names another installed engine's model), ``language``,
    ``response_format`` (json, text, verbose_json, srt, vtt) and
    ``timestamp_granularities[]`` (accepted, ignored: segment timing is
    always included in ``verbose_json``). The body is bounded by
    ``stt.max_upload_mb`` before it is parsed; oversize requests get 413.

Streaming (``WS /v1/audio/transcriptions/stream``)
    The client may first send a JSON text frame
    ``{"type": "config", "sample_rate": 16000, "language": "en"}``; both keys
    are optional and 16 kHz is the default. It then sends binary frames of
    mono **int16 little-endian PCM** at that sample rate (any frame size; a
    partial sample at the end of a frame is carried into the next one) and
    finally the text frame ``{"type": "end"}``. The server sends
    ``{"type": "partial", "text": "..."}`` whenever the hypothesis changes
    (at most every :data:`PARTIAL_INTERVAL_S`) and, after ``end``, one
    ``{"type": "final", "text": ..., "language": ..., "duration": ...,
    "segments": [...]}`` followed by a normal close.

    The whole session runs inside one ``inference()`` block, so the queue
    bounds concurrent streams and the close code follows the outcome table
    in ADR 0005 (``voiced.api.context.WS_CLOSE``): a full queue closes with
    1013, a client mistake (bad frame, unsupported language, bad sample rate)
    with 1008, an engine failure with 1011. A client that sends nothing for
    ``server.session_idle_s`` is a disconnect: the socket closes with 1001.
    A bad API key is refused at the handshake (HTTP 403).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from typing import Any, cast

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import Response
from starlette.datastructures import FormData, UploadFile
from starlette.requests import ClientDisconnect
from starlette.types import Message, Receive

from voiced.api import ffmpeg, formats
from voiced.api.context import WS_CLOSE, APIError, Ctx, close_ws
from voiced.api.foreign_models import ForeignModels, reject_foreign_model
from voiced.audio import MAX_INPUT_RATE, MIN_INPUT_RATE, PCMConverter
from voiced.config import Settings
from voiced.context import ServiceContext
from voiced.engines.base import STT_SAMPLE_RATE, AudioInput, EngineError
from voiced.inference import bounded, classify, inference
from voiced.metrics import Operation, Outcome

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/audio", tags=["stt"])

PARTIAL_INTERVAL_S = 0.25
"""Minimum time between two ``partial`` events on the WebSocket."""

MB = 1024 * 1024

CLIENT_CLOSED_REQUEST = 499
"""Status for a request the client abandoned (nginx's convention); never delivered."""

UPLOAD_SLACK_BYTES = 64 * 1024
"""Multipart framing allowance on top of ``stt.max_upload_mb``."""

_TRANSCRIPTION_FORM_SCHEMA: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["file"],
                    "properties": {
                        "file": {"type": "string", "format": "binary"},
                        "model": {"type": "string"},
                        "language": {"type": "string"},
                        "response_format": {
                            "type": "string",
                            "enum": list(formats.RESPONSE_FORMATS),
                            "default": "json",
                        },
                        "timestamp_granularities[]": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                }
            }
        },
    }
}


class _PayloadTooLarge(Exception):
    pass


def _bounded_receive(receive: Receive, limit: int) -> Receive:
    """Wrap an ASGI ``receive`` so the request body cannot exceed ``limit`` bytes."""
    seen = 0

    async def bounded_receive() -> Message:
        nonlocal seen
        message = await receive()
        if message["type"] == "http.request":
            seen += len(message.get("body", b""))
            if seen > limit:
                raise _PayloadTooLarge
        return message

    return bounded_receive


def _too_large(limit_mb: int) -> APIError:
    return APIError(
        status.HTTP_413_CONTENT_TOO_LARGE,
        f"upload exceeds the {limit_mb} MB limit (stt.max_upload_mb)",
        type="payload_too_large",
    )


def _parse_format(value: str | None) -> formats.ResponseFormat:
    if not value:
        return "json"
    if value in formats.RESPONSE_FORMATS:
        return cast(formats.ResponseFormat, value)
    raise APIError(
        status.HTTP_400_BAD_REQUEST,
        f"response_format must be one of {', '.join(formats.RESPONSE_FORMATS)}",
    )


@router.post(
    "/transcriptions",
    summary="Transcribe an audio file",
    openapi_extra=_TRANSCRIPTION_FORM_SCHEMA,
    responses={
        200: {"description": "Transcript in the requested response_format"},
        400: {"description": "Bad parameters or undecodable audio"},
        413: {"description": "Upload larger than stt.max_upload_mb"},
        503: {"description": "Inference queue full; retry later"},
    },
)
async def transcriptions(request: Request, ctx: Ctx, foreign: ForeignModels) -> Response:
    limit_mb = ctx.settings.stt.max_upload_mb
    # The body is capped while it is read, before and during multipart parsing.
    # Malformed multipart raises Starlette's HTTPException(400), which the app
    # renders in the shared error envelope.
    bounded_request = Request(
        request.scope, _bounded_receive(request.receive, limit_mb * MB + UPLOAD_SLACK_BYTES)
    )
    try:
        async with bounded_request.form() as form:
            data, fmt, language = await _parse_upload(ctx, foreign, form)
    except _PayloadTooLarge:
        raise _too_large(limit_mb) from None
    except ClientDisconnect:
        # The client hung up mid-upload. Not an error, and there is no one to
        # answer: return quietly (the server discards this response) rather
        # than raise, which would make the server log a traceback.
        log.debug("client went away during the upload")
        return Response(status_code=CLIENT_CLOSED_REQUEST)

    try:
        # Decoding runs outside the slot, so it is bounded on its own: each
        # upload holds its bytes, an ffmpeg process and the decoded audio.
        with ctx.uploads.admit():
            samples = await ffmpeg.decode(
                data, STT_SAMPLE_RATE, max_seconds=ctx.settings.stt.max_audio_s
            )
    except ffmpeg.AudioDecodeError as exc:
        raise APIError(status.HTTP_400_BAD_REQUEST, str(exc), "invalid_audio") from exc
    except ffmpeg.AudioTooLong as exc:
        raise APIError(
            status.HTTP_413_CONTENT_TOO_LARGE,
            f"{exc} (stt.max_upload_mb read as 16 kHz 16-bit audio)",
            type="payload_too_large",
        ) from exc

    async with inference(ctx, Operation.TRANSCRIBE):
        transcript = await ctx.stt_engine.transcribe(AudioInput(samples), language)
    body, media_type = formats.render(transcript, fmt)
    return Response(content=body, media_type=media_type)


async def _parse_upload(
    ctx: ServiceContext, foreign: dict[str, str], form: FormData
) -> tuple[bytes, formats.ResponseFormat, str | None]:
    """Validate the form fields and read the upload."""
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        raise APIError(status.HTTP_400_BAD_REQUEST, "multipart field 'file' is required")
    fmt = _parse_format(_str_field(form, "response_format"))
    language = ctx.check_language(_str_field(form, "language"))
    reject_foreign_model(ctx, foreign, _str_field(form, "model"))
    limit_mb = ctx.settings.stt.max_upload_mb
    # The receive cap allows UPLOAD_SLACK_BYTES of multipart framing, so the
    # file itself can still be a little over the limit.
    if (upload.size or 0) > limit_mb * MB:
        raise _too_large(limit_mb)
    return await upload.read(), fmt, language


def _str_field(form: FormData, key: str) -> str | None:
    value = form.get(key)
    return value if isinstance(value, str) and value else None


class BadFrame(EngineError):
    """A WebSocket frame the protocol does not allow. A client error (close 1008)."""


@router.websocket("/transcriptions/stream")
async def transcriptions_stream(ws: WebSocket, ctx: Ctx) -> None:
    # The API key and the loaded check run as router dependencies and refuse
    # the handshake (403) before accept().
    await ws.accept()
    try:
        async with inference(ctx, Operation.STREAM):
            await _stream_session(ws, ctx)
    except Exception as exc:
        outcome = classify(exc)
        if outcome is Outcome.FAILURE:
            log.exception("streaming transcription failed")
        reason = "" if outcome is Outcome.DISCONNECT else f"{type(exc).__name__}: {exc}"
        await close_ws(ws, WS_CLOSE[outcome], reason)
        return
    await close_ws(ws, status.WS_1000_NORMAL_CLOSURE)


async def _next_frame(ws: WebSocket, ctx: ServiceContext) -> bytes | dict[str, Any]:
    """The next client frame: PCM bytes or a parsed JSON event.

    Raises ``WebSocketDisconnect`` when the client leaves, ``ClientTimeout``
    (a disconnect) after ``server.session_idle_s`` without a frame, and
    :class:`BadFrame` for anything that is not binary or a JSON object.
    """
    message = await bounded(ws.receive(), ctx.settings.server.session_idle_s)
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000))
    data: bytes | None = message.get("bytes")
    if data is not None:
        return data
    text: str | None = message.get("text")
    try:
        event = json.loads(text if text is not None else "")
    except json.JSONDecodeError as exc:
        raise BadFrame(f"text frames must be JSON: {exc.msg}") from exc
    if not isinstance(event, dict):
        raise BadFrame("text frames must be JSON objects")
    return event


def _parse_config(event: dict[str, Any]) -> tuple[int, str | None]:
    rate = event.get("sample_rate", STT_SAMPLE_RATE)
    if not isinstance(rate, int) or not MIN_INPUT_RATE <= rate <= MAX_INPUT_RATE:
        raise BadFrame(
            f"config.sample_rate must be an integer between {MIN_INPUT_RATE} and {MAX_INPUT_RATE}"
        )
    language = event.get("language")
    if language is not None and not isinstance(language, str):
        raise BadFrame("config.language must be a string")
    return rate, language


def max_stream_s(settings: Settings) -> float:
    """Longest audio one WebSocket session may carry, in seconds."""
    return settings.stt.max_audio_s


async def _stream_session(ws: WebSocket, ctx: ServiceContext) -> None:
    """Runs inside ``inference()``: only ``EngineError`` subclasses for client mistakes.

    Bounded like a Wyoming utterance (ADR 0011): at most :func:`max_stream_s`
    of audio, and the whole session at most that long plus
    ``server.session_idle_s``, so a client trickling frames cannot hold the
    slot forever.
    """
    limit_s = max_stream_s(ctx.settings)
    deadline = asyncio.timeout(limit_s + ctx.settings.server.session_idle_s)
    try:
        async with deadline:
            await _stream_frames(ws, ctx, int(limit_s * STT_SAMPLE_RATE))
    except TimeoutError:
        if not deadline.expired():
            raise
        raise EngineError(f"stream longer than {limit_s:.0f} s") from None


async def _stream_frames(ws: WebSocket, ctx: ServiceContext, max_samples: int) -> None:
    write_timeout = ctx.settings.server.write_timeout_s
    converter = PCMConverter(STT_SAMPLE_RATE, 2, 1)
    language: str | None = None
    last_partial = ""
    last_partial_at = -float("inf")
    session = None
    total = 0
    try:
        while True:
            frame = await _next_frame(ws, ctx)
            if isinstance(frame, dict):
                kind = frame.get("type")
                if kind == "end":
                    break
                if kind == "config":
                    if session is not None:
                        raise BadFrame("config must be the first frame")
                    rate, hint = _parse_config(frame)
                    language = ctx.check_language(hint)
                    converter = PCMConverter(rate, 2, 1)
                    continue
                raise BadFrame(f"unexpected event type {kind!r}")

            if session is None:
                session = await ctx.stt_engine.stream(language)
            samples = converter.convert(frame)
            total += len(samples)
            if total > max_samples:
                raise EngineError(f"stream longer than {max_samples / STT_SAMPLE_RATE:.0f} s")
            await session.add_audio(samples)
            partial = session.partial()
            now = time.monotonic()
            if partial != last_partial and now - last_partial_at >= PARTIAL_INTERVAL_S:
                last_partial, last_partial_at = partial, now
                await bounded(ws.send_json({"type": "partial", "text": partial}), write_timeout)

        if session is None:
            session = await ctx.stt_engine.stream(language)
        transcript = await session.finalize()
        final = {
            "type": "final",
            "text": transcript.text,
            "language": transcript.language,
            "duration": round(transcript.duration_s, 3),
            "segments": [asdict(s) for s in transcript.segments],
        }
        await bounded(ws.send_json(final), write_timeout)
    finally:
        if session is not None:
            await session.close()
