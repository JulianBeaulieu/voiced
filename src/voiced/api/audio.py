"""Audio decoding and encoding for the HTTP layer.

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

from voiced.engines.base import STT_SAMPLE_RATE, TTS_SAMPLE_RATE

log = logging.getLogger(__name__)

EncodedFormat = Literal["mp3", "opus"]
OutputFormat = Literal["wav", "pcm", "mp3", "opus"]

MEDIA_TYPES: dict[str, str] = {
    "wav": "audio/wav",
    "pcm": "audio/pcm",  # raw int16 LE, 24 kHz mono; what the OpenAI endpoint returns
    "mp3": "audio/mpeg",
    "opus": "audio/ogg",  # Opus in an Ogg container, like the OpenAI endpoint
}

# ffmpeg output flags per encoded format. Low-latency settings: no bitstream
# buffering the encoder can avoid, so the first bytes leave as soon as possible.
_ENCODER_ARGS: dict[str, list[str]] = {
    "mp3": ["-codec:a", "libmp3lame", "-b:a", "128k", "-f", "mp3"],
    "opus": ["-codec:a", "libopus", "-b:a", "64k", "-f", "ogg"],
}

_DECODE_TIMEOUT_S = 120.0


class AudioDecodeError(Exception):
    """The upload could not be decoded. Mapped to HTTP 400."""


class FFmpegMissing(AudioDecodeError):
    """ffmpeg is not installed."""


def ffmpeg_path() -> str | None:
    """Absolute path of the ffmpeg binary, or ``None`` if it is not on ``PATH``."""
    return shutil.which("ffmpeg")


def ffmpeg_available() -> bool:
    return ffmpeg_path() is not None


async def decode(data: bytes, sample_rate: int = STT_SAMPLE_RATE) -> NDArray[np.float32]:
    """Decode any audio file ``data`` to mono float32 at ``sample_rate`` with ffmpeg."""
    if not data:
        raise AudioDecodeError("empty upload")
    ffmpeg = ffmpeg_path()
    if ffmpeg is None:
        raise FFmpegMissing("ffmpeg is not installed (brew install ffmpeg)")
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
        message = err.decode(errors="replace").strip().splitlines()
        raise AudioDecodeError(message[-1] if message else "ffmpeg could not decode the upload")
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
    ffmpeg = ffmpeg_path()
    if ffmpeg is None:
        raise FFmpegMissing(f"ffmpeg is required for {fmt} output (brew install ffmpeg)")
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
    producer_error: list[BaseException] = []

    async def feed() -> None:
        try:
            async with aclosing(cast(AsyncGenerator[bytes, None], pcm)):
                async for chunk in pcm:
                    stdin.write(chunk)
                    await stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
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
