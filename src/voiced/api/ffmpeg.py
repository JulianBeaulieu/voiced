"""ffmpeg decoding and encoding for the HTTP layer.

Decoding (uploads to STT) goes through ``ffmpeg`` as an external program:
any container or codec ffmpeg knows becomes mono float32 at 16 kHz. The
upload is written to a temporary file first because containers such as
``.m4a`` put their index at the end and cannot be decoded from a pipe. ffmpeg
is a hard external dependency (``voiced doctor`` checks for it); without it
:func:`decode` raises :class:`FFmpegMissing`.

Encoding (TTS output) is streaming: :func:`encode_stream` pipes PCM through
ffmpeg for ``mp3`` and ``opus`` while the engine is still producing audio.
Raw PCM helpers (WAV header, int16 conversion, resampling) are in
``voiced.audio``, shared with the Wyoming layer.

See ``docs/decisions/0007-http-api.md`` for why ffmpeg is an external
dependency rather than a Python package.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing
from typing import Literal, cast

import numpy as np
from numpy.typing import NDArray

from voiced.api.context import APIError
from voiced.engines.base import STT_SAMPLE_RATE, TTS_SAMPLE_RATE

log = logging.getLogger(__name__)

EncodedFormat = Literal["mp3", "opus"]

# ffmpeg output flags per encoded format. Low-latency settings: no bitstream
# buffering the encoder can avoid, so the first bytes leave as soon as possible.
_ENCODER_ARGS: dict[str, list[str]] = {
    "mp3": ["-codec:a", "libmp3lame", "-b:a", "128k", "-f", "mp3"],
    "opus": ["-codec:a", "libopus", "-b:a", "64k", "-f", "ogg"],
}

_DECODE_TIMEOUT_S = 120.0


class AudioDecodeError(Exception):
    """ffmpeg ran but could not decode the upload. The route maps it to 400."""


class FFmpegMissing(APIError):
    """ffmpeg is not installed. A server-side problem: 500 ``ffmpeg_missing``.

    An :class:`APIError` so the app's one ``APIError`` handler renders it for
    every route; ``voiced doctor`` reports the same condition as a failure.
    """

    def __init__(self, purpose: str) -> None:
        super().__init__(
            500, f"ffmpeg is required for {purpose} (brew install ffmpeg)", "ffmpeg_missing"
        )


def require_ffmpeg(purpose: str) -> str:
    """Path of the ffmpeg binary; raises :class:`FFmpegMissing` if it is not installed."""
    path = ffmpeg_path()
    if path is None:
        raise FFmpegMissing(purpose)
    return path


def ffmpeg_path() -> str | None:
    """Absolute path of the ffmpeg binary, or ``None`` if it is not on ``PATH``."""
    return shutil.which("ffmpeg")


def ffmpeg_available() -> bool:
    return ffmpeg_path() is not None


async def decode(data: bytes, sample_rate: int = STT_SAMPLE_RATE) -> NDArray[np.float32]:
    """Decode any audio file ``data`` to mono float32 at ``sample_rate`` with ffmpeg."""
    if not data:
        raise AudioDecodeError("empty upload")
    ffmpeg = require_ffmpeg("decoding uploads")
    fd, path = tempfile.mkstemp(prefix="voiced-upload-", suffix=".bin")
    try:
        await asyncio.to_thread(_write_and_close, fd, data)
        proc = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            path,
            "-vn",
            "-f",
            "f32le",
            "-acodec",
            "pcm_f32le",
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "pipe:1",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=_DECODE_TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise AudioDecodeError(f"decoding took longer than {_DECODE_TIMEOUT_S:.0f} s") from None
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise
    finally:
        try:
            os.unlink(path)
        except OSError:  # pragma: no cover
            pass
    if proc.returncode != 0:
        # ffmpeg's stderr names temp files and internals; keep it out of the response.
        log.debug("ffmpeg could not decode the upload: %s", err.decode(errors="replace").strip())
        raise AudioDecodeError("the upload is not an audio file ffmpeg can decode")
    if not out:
        raise AudioDecodeError("no audio stream in upload")
    return np.frombuffer(out, dtype=np.float32)


def _write_and_close(fd: int, data: bytes) -> None:
    with os.fdopen(fd, "wb") as f:
        f.write(data)


async def encode_stream(
    pcm: AsyncIterator[bytes],
    fmt: EncodedFormat,
    sample_rate: int = TTS_SAMPLE_RATE,
) -> AsyncIterator[bytes]:
    """Pipe int16 mono PCM chunks through ffmpeg and yield encoded bytes as they appear.

    The PCM producer and the encoder run concurrently: a writer task feeds
    ffmpeg's stdin while this generator reads its stdout, so the client hears
    the first sentence before the last one is synthesized. Stopping the
    generator early (client disconnect) kills ffmpeg and closes ``pcm``.
    """
    ffmpeg = require_ffmpeg(f"{fmt} output")
    proc = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "s16le",
        "-ar",
        str(sample_rate),
        "-ac",
        "1",
        "-i",
        "pipe:0",
        *_ENCODER_ARGS[fmt],
        "-flush_packets",
        "1",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    stdin, stdout, stderr = proc.stdin, proc.stdout, proc.stderr
    producer_error: list[Exception] = []

    async def feed() -> None:
        try:
            async with aclosing(cast(AsyncGenerator[bytes, None], pcm)):
                async for chunk in pcm:
                    stdin.write(chunk)
                    await stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # CancelledError is a BaseException and passes through
            producer_error.append(exc)
        finally:
            if not stdin.is_closing():
                stdin.close()

    async def drain_stderr() -> bytes:
        return await stderr.read()

    feeder = asyncio.create_task(feed())
    stderr_task = asyncio.create_task(drain_stderr())
    try:
        while True:
            chunk = await stdout.read(4096)
            if not chunk:
                break
            yield chunk
        await feeder
        err = await stderr_task
        await proc.wait()
        if producer_error:
            raise producer_error[0]
        if proc.returncode != 0:
            text = err.decode(errors="replace").strip()
            raise RuntimeError(f"ffmpeg {fmt} encoder failed: {text or proc.returncode}")
    finally:
        if proc.returncode is None:
            proc.kill()
        for task in (feeder, stderr_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(feeder, stderr_task, proc.wait(), return_exceptions=True)
