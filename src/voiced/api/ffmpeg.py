"""ffmpeg decoding and encoding for the HTTP layer.

Decoding (uploads to STT) goes through ``ffmpeg`` as an external program:
any container or codec ffmpeg knows becomes mono float32 at 16 kHz. The
upload is written to an anonymous temporary file first because containers
such as ``.m4a`` put their index at the end and cannot be decoded from a
pipe; ffmpeg may open nothing but that file (see :func:`decode`). The
binary comes from the ``imageio-ffmpeg`` wheel (``voiced.ffmpeg_exe``,
``voiced doctor`` checks it); without any ffmpeg :func:`decode` raises
:class:`FFmpegMissing`.

Encoding (TTS output) is streaming: :func:`encode_stream` pipes PCM through
ffmpeg for ``mp3`` and ``opus`` while the engine is still producing audio.
Raw PCM helpers (WAV header, int16 conversion, resampling) are in
``voiced.audio``, shared with the Wyoming layer.

See ``docs/decisions/0007-http-api.md`` for why ffmpeg runs as an external
program, and ADR 0018 for where it comes from.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from collections.abc import AsyncGenerator, AsyncIterator, Iterable
from contextlib import aclosing, suppress
from typing import IO, Any, Literal, cast

import numpy as np
from numpy.typing import NDArray

from voiced.api.context import APIError
from voiced.engines.base import STT_SAMPLE_RATE, TTS_SAMPLE_RATE
from voiced.ffmpeg_exe import ffmpeg_path

log = logging.getLogger(__name__)

EncodedFormat = Literal["mp3", "opus"]

# ffmpeg output flags per encoded format. Low-latency settings: no bitstream
# buffering the encoder can avoid, so the first bytes leave as soon as possible.
_ENCODER_ARGS: dict[str, list[str]] = {
    "mp3": ["-codec:a", "libmp3lame", "-b:a", "128k", "-f", "mp3"],
    "opus": ["-codec:a", "libopus", "-b:a", "64k", "-f", "ogg"],
}

_DECODE_TIMEOUT_S = 120.0
_READ_BYTES = 1 << 20
_STDERR_KEEP = 64 * 1024
"""ffmpeg's diagnostics kept for the debug log; the rest is read and dropped."""


class AudioDecodeError(Exception):
    """ffmpeg ran but could not decode the upload. The route maps it to 400."""


class AudioTooLong(Exception):
    """The upload decodes to more audio than allowed. The route maps it to 413."""

    def __init__(self, max_seconds: float) -> None:
        super().__init__(f"the upload decodes to more than {max_seconds:.0f} s of audio")
        self.max_seconds = max_seconds


class FFmpegMissing(APIError):
    """No ffmpeg: the imageio-ffmpeg wheel is missing and none is on ``PATH``.
    A server-side problem: 500 ``ffmpeg_missing``.

    An :class:`APIError` so the app's one ``APIError`` handler renders it for
    every route; ``voiced doctor`` reports the same condition as a failure.
    """

    def __init__(self, purpose: str) -> None:
        super().__init__(
            500,
            f"ffmpeg is required for {purpose} (it comes with voiced's imageio-ffmpeg "
            "dependency: reinstall voiced)",
            "ffmpeg_missing",
        )


def require_ffmpeg(purpose: str) -> str:
    """Path of the ffmpeg binary; raises :class:`FFmpegMissing` if it is not installed."""
    path = ffmpeg_path()
    if path is None:
        raise FFmpegMissing(purpose)
    return path


def ffmpeg_available() -> bool:
    return ffmpeg_path() is not None


async def decode(
    data: bytes, sample_rate: int = STT_SAMPLE_RATE, *, max_seconds: float | None = None
) -> NDArray[np.float32]:
    """Decode any audio file ``data`` to mono float32 at ``sample_rate`` with ffmpeg.

    The upload goes to an anonymous temporary file (unlinked from the start,
    so nothing is left behind if the process is killed) that ffmpeg reads
    through an inherited descriptor, with ``fd`` as the only protocol it may
    open. Containers such as ``.m4a`` need to seek, which a pipe cannot do;
    the whitelist stops formats that refer to other inputs (``ffconcat``
    lists, playlists) from opening files or URLs on the server's behalf.

    ``max_seconds`` bounds the decoded audio, not the upload: a small file can
    decode to hours of audio. Past it ffmpeg is stopped and
    :class:`AudioTooLong` raised.
    """
    if not data:
        raise AudioDecodeError("empty upload")
    ffmpeg = require_ffmpeg("decoding uploads")
    limit = None if max_seconds is None else int(max_seconds * sample_rate) * 4
    upload = await asyncio.to_thread(_spool, data)
    try:
        fd = upload.fileno()
        proc = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "fd",
            "-fd",
            str(fd),
            "-i",
            "fd:",
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
            pass_fds=(fd,),
        )
    finally:
        upload.close()  # ffmpeg holds its own copy of the descriptor
    assert proc.stdout is not None and proc.stderr is not None
    stdout, stderr = proc.stdout, proc.stderr
    read_out = asyncio.create_task(_read_at_most(stdout, limit))
    read_err = asyncio.create_task(_read_head(stderr, _STDERR_KEEP))
    reads = (read_out, read_err)
    try:
        await asyncio.wait_for(asyncio.gather(*reads), timeout=_DECODE_TIMEOUT_S)
    except TimeoutError:
        await _stop(proc, reads)
        raise AudioDecodeError(f"decoding took longer than {_DECODE_TIMEOUT_S:.0f} s") from None
    except _OverLimit:
        await _stop(proc, reads)
        assert max_seconds is not None
        raise AudioTooLong(max_seconds) from None
    except asyncio.CancelledError:
        await _stop(proc, reads)
        raise
    out, err = read_out.result(), read_err.result()
    await proc.wait()
    if proc.returncode != 0:
        # ffmpeg's stderr names internals; keep it out of the response.
        log.debug("ffmpeg could not decode the upload: %s", err.decode(errors="replace").strip())
        raise AudioDecodeError("the upload is not an audio file ffmpeg can decode")
    if not out:
        raise AudioDecodeError("no audio stream in upload")
    return np.frombuffer(out, dtype=np.float32)


class _OverLimit(Exception):
    pass


async def _stop(proc: asyncio.subprocess.Process, reads: Iterable[asyncio.Task[Any]]) -> None:
    """Stop a decode early: cancel the readers, kill ffmpeg and reap it."""
    readers = list(reads)
    for task in readers:
        task.cancel()
    await asyncio.gather(*readers, return_exceptions=True)
    with suppress(ProcessLookupError):
        proc.kill()
    assert proc.stdout is not None and proc.stderr is not None
    # Read both pipes to their end so wait() can return (see encode_stream).
    await asyncio.gather(proc.stdout.read(), proc.stderr.read(), return_exceptions=True)
    await proc.wait()


async def _read_at_most(stream: asyncio.StreamReader, limit: int | None) -> bytearray:
    """Read ``stream`` to EOF; more than ``limit`` bytes raises :class:`_OverLimit`.
    Returns the buffer itself: decoded audio can be 100 MB, not worth a copy."""
    buf = bytearray()
    while chunk := await stream.read(_READ_BYTES):
        if limit is not None and len(buf) + len(chunk) > limit:
            raise _OverLimit
        buf += chunk
    return buf


async def _read_head(stream: asyncio.StreamReader, keep: int) -> bytes:
    """Read ``stream`` to EOF, keeping its first ``keep`` bytes."""
    buf = bytearray()
    while chunk := await stream.read(_READ_BYTES):
        buf += chunk[: max(0, keep - len(buf))]
    return bytes(buf)


def _spool(data: bytes) -> IO[bytes]:
    upload = tempfile.TemporaryFile(prefix="voiced-upload-")
    upload.write(data)
    upload.flush()
    upload.seek(0)
    return upload


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
        # Raw PCM needs no probing; ffmpeg 7.1 (imageio-ffmpeg's) probes it
        # anyway and holds the first bytes back about a second without this.
        "-probesize",
        "32",
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
        if not feeder.done():
            feeder.cancel()
        # Read stdout and stderr to EOF before waiting. Since Python 3.12,
        # Process.wait() returns only once every pipe is closed, and a pipe
        # whose reader stopped (a client that stopped reading leaves stdout
        # full, so asyncio paused it) never sees EOF on its own: wait() would
        # block forever and pin this response, even with ffmpeg killed. What is
        # left is at most a pipe buffer.
        await asyncio.gather(feeder, stderr_task, stdout.read(), return_exceptions=True)
        await proc.wait()
