from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from tests.api.conftest import requires_ffmpeg, sine_wav
from voiced.api import ffmpeg


@requires_ffmpeg
async def test_ffmpeg_decodes_wav_to_16k_float32_mono() -> None:
    samples = await ffmpeg.decode(sine_wav(seconds=0.5, sample_rate=16_000))
    assert samples.dtype == np.float32
    assert samples.ndim == 1
    assert abs(len(samples) - 8000) <= 16
    assert 0.25 < float(np.abs(samples).max()) <= 0.31


@requires_ffmpeg
async def test_ffmpeg_resamples_and_downmixes() -> None:
    samples = await ffmpeg.decode(sine_wav(seconds=0.5, sample_rate=44_100, channels=2))
    assert abs(len(samples) - 8000) <= 32
    assert samples.dtype == np.float32


@requires_ffmpeg
async def test_ffmpeg_rejects_garbage() -> None:
    with pytest.raises(ffmpeg.AudioDecodeError):
        await ffmpeg.decode(b"this is not audio" * 100)


async def test_empty_upload_rejected() -> None:
    with pytest.raises(ffmpeg.AudioDecodeError):
        await ffmpeg.decode(b"")


async def test_without_ffmpeg_decode_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ffmpeg, "ffmpeg_path", lambda: None)
    with pytest.raises(ffmpeg.FFmpegMissing) as exc:
        await ffmpeg.decode(sine_wav())
    # An APIError, rendered by the app's one handler for every route.
    assert (exc.value.status, exc.value.type) == (500, "ffmpeg_missing")
    assert not isinstance(exc.value, ffmpeg.AudioDecodeError)


@requires_ffmpeg
@pytest.mark.parametrize("fmt", ["mp3", "opus"])
async def test_encode_stream_round_trips_through_ffmpeg(fmt: str) -> None:
    n = 24_000  # one second of 24 kHz int16 silence, in four chunks
    pcm_chunks = [bytes(n // 2) for _ in range(4)]

    async def produce():  # type: ignore[no-untyped-def]
        for c in pcm_chunks:
            yield c

    encoded = b"".join([c async for c in ffmpeg.encode_stream(produce(), fmt)])  # type: ignore[arg-type]
    assert encoded.startswith(b"ID3" if fmt == "mp3" else b"OggS")
    decoded = await ffmpeg.decode(encoded, 16_000)
    assert abs(len(decoded) - 16_000) < 16_000 * 0.15  # codec priming and padding


@requires_ffmpeg
async def test_upload_cannot_make_ffmpeg_open_other_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ffmpeg picks the demuxer from the content, and an ``ffconcat`` list names
    other inputs. When uploads were named files in the temporary directory
    (``/tmp`` for a daemon without ``TMPDIR``), such a list made ffmpeg read
    another audio file there and the server transcribe it."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    (tmp_path / "other.wav").write_bytes(sine_wav(seconds=1.0))
    concat = b"ffconcat version 1.0\nfile other.wav\n"
    with pytest.raises(ffmpeg.AudioDecodeError):
        await ffmpeg.decode(concat)


@requires_ffmpeg
async def test_upload_cannot_name_another_file_by_absolute_path(tmp_path: Path) -> None:
    other = tmp_path / "other.wav"
    other.write_bytes(sine_wav(seconds=1.0))
    concat = f"ffconcat version 1.0\nfile {other}\n".encode()
    with pytest.raises(ffmpeg.AudioDecodeError):
        await ffmpeg.decode(concat)


async def test_decode_lets_ffmpeg_open_only_the_uploads_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``-protocol_whitelist fd``, before the input. ffmpeg 7.1's concat
    demuxer also refuses absolute paths and URLs by itself (its ``safe``
    option), so no upload tells the two apart; the whitelist is what holds
    for every demuxer and every ffmpeg on ``PATH``."""
    seen: list[str] = []

    class Spawned(Exception):
        pass

    async def spy(*args: Any, **kwargs: Any) -> Any:
        seen.extend(str(a) for a in args)
        raise Spawned

    monkeypatch.setattr(ffmpeg, "ffmpeg_path", lambda: "/x/ffmpeg")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    with pytest.raises(Spawned):
        await ffmpeg.decode(sine_wav())
    at = seen.index("-protocol_whitelist")
    assert seen[at + 1] == "fd" and at < seen.index("-i")


@requires_ffmpeg
async def test_upload_leaves_no_file_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The temporary file is anonymous from the start, so a service killed
    mid-decode (the watchdog uses SIGKILL) leaves nothing to clean up."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    started = asyncio.Event()
    real = asyncio.create_subprocess_exec

    def leftovers() -> list[Path]:
        return list(tmp_path.iterdir())

    async def spy(*args: Any, **kwargs: Any) -> Any:
        assert leftovers() == [], "a named upload file exists during the decode"
        started.set()
        return await real(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    await ffmpeg.decode(sine_wav())
    assert started.is_set()
    assert leftovers() == []


@requires_ffmpeg
async def test_decoded_length_is_bounded() -> None:
    """A small upload can decode to hours of audio; the bound is on what comes
    out of ffmpeg, not on the upload's size."""
    with pytest.raises(ffmpeg.AudioTooLong):
        await ffmpeg.decode(sine_wav(seconds=3.0, sample_rate=8_000), max_seconds=1.0)
    samples = await ffmpeg.decode(sine_wav(seconds=0.5), max_seconds=1.0)
    assert abs(len(samples) - 8000) <= 16


@requires_ffmpeg
async def test_seekable_containers_still_decode(tmp_path: Path) -> None:
    """``.m4a`` keeps its index at the end; reading through the descriptor
    must still allow seeking."""
    src = tmp_path / "in.wav"
    src.write_bytes(sine_wav(seconds=1.0))
    out = tmp_path / "out.m4a"
    exe = ffmpeg.ffmpeg_path()  # the one voiced runs, not whatever is on PATH
    assert exe is not None
    proc = await asyncio.create_subprocess_exec(
        exe, "-loglevel", "error", "-i", str(src), "-c:a", "aac", str(out)
    )
    assert await proc.wait() == 0
    samples = await ffmpeg.decode(out.read_bytes())
    assert abs(len(samples) - 16_000) < 16_000 * 0.1
