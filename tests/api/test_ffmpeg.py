from __future__ import annotations

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
