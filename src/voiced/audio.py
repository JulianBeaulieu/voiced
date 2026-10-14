"""PCM toolbox shared by the HTTP and Wyoming layers.

Raw integer PCM in, mono float32 at :data:`STT_SAMPLE_RATE` out (for STT),
and int16 bytes plus WAV headers out (for TTS). No transport imports here:
callers pass plain ``bytes`` and format parameters.
"""

from __future__ import annotations

import struct

import numpy as np
from numpy.typing import NDArray

from voiced.engines.base import STT_SAMPLE_RATE, TTS_SAMPLE_RATE, TTS_SAMPLE_WIDTH, EngineError

_INT_DTYPES: dict[int, tuple[np.dtype[np.signedinteger] | np.dtype[np.unsignedinteger], float]] = {
    1: (np.dtype(np.uint8), 128.0),
    2: (np.dtype("<i2"), 32768.0),
    4: (np.dtype("<i4"), 2147483648.0),
}

UNKNOWN_LENGTH = 0xFFFFFFFF


class UnsupportedAudioFormat(EngineError):
    """The sample width, channel count or rate cannot be converted.

    An :class:`EngineError` because the client chose the format, so it maps to
    a client error on every transport.
    """


def _to_float32(data: bytes, width: int) -> NDArray[np.float32]:
    """Whole frames of little-endian integer PCM to float32 in [-1, 1]."""
    try:
        dtype, scale = _INT_DTYPES[width]
    except KeyError:
        raise UnsupportedAudioFormat(f"unsupported sample width {width}") from None
    samples = np.frombuffer(data, dtype=dtype).astype(np.float32)
    if width == 1:
        samples -= 128.0
    samples /= scale
    return samples


def pcm16_to_float32(data: bytes) -> NDArray[np.float32]:
    """Mono int16 little-endian PCM to float32 in [-1, 1]. A trailing odd byte is dropped."""
    return _to_float32(data[: len(data) - len(data) % 2], 2)


def float32_to_pcm16(samples: NDArray[np.float32]) -> bytes:
    """Float samples to int16 little-endian bytes, clipping to [-1, 1]. NaN becomes 0."""
    clipped = np.clip(np.nan_to_num(samples, nan=0.0, posinf=1.0, neginf=-1.0), -1.0, 1.0) * 32767.0
    return np.rint(clipped).astype("<i2").tobytes()


def wav_header(
    sample_rate: int = TTS_SAMPLE_RATE,
    channels: int = 1,
    width: int = TTS_SAMPLE_WIDTH,
    data_length: int | None = None,
) -> bytes:
    """A 44-byte PCM WAV header.

    ``data_length=None`` writes the streaming form: both size fields are
    ``0xFFFFFFFF``, which browsers, ffmpeg and most players treat as "read
    until end of stream". Pass the real length when the whole file is known.
    """
    data_size = UNKNOWN_LENGTH if data_length is None else data_length
    riff_size = UNKNOWN_LENGTH if data_length is None else 36 + data_length
    byte_rate = sample_rate * channels * width
    block_align = channels * width
    fmt = struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, 8 * width)
    sizes = struct.pack("<I", riff_size), struct.pack("<I", data_size)
    return b"RIFF" + sizes[0] + b"WAVE" + b"fmt " + fmt + b"data" + sizes[1]


class PCMConverter:
    """Stateful converter from integer PCM chunks to mono float32 at ``target_rate``.

    One instance per audio stream and input format: ``width`` 1 is unsigned
    8-bit, 2 is int16, 4 is int32 (float32 input is not supported). State is
    kept across chunks so odd-sized payloads (a partial frame at the end of
    one chunk) and resampling (fractional read position) are handled without
    clicks at chunk boundaries; feeding the same audio in any chunking yields
    the same samples.
    """

    def __init__(
        self, rate: int, width: int, channels: int, target_rate: int = STT_SAMPLE_RATE
    ) -> None:
        if channels < 1:
            raise UnsupportedAudioFormat(f"channels must be >= 1, got {channels}")
        if rate < 1:
            raise UnsupportedAudioFormat(f"invalid sample rate {rate}")
        if width not in _INT_DTYPES:
            raise UnsupportedAudioFormat(f"unsupported sample width {width}")
        self.rate, self.width, self.channels = rate, width, channels
        self.target_rate = target_rate
        self._carry = b""
        self._prev: float | None = None  # last input sample, for interpolation
        self._pos = 0.0  # fractional read position relative to ``_prev``

    def convert(self, data: bytes) -> NDArray[np.float32]:
        frame = self.width * self.channels
        raw = self._carry + data
        usable = len(raw) - len(raw) % frame
        self._carry = raw[usable:]
        if usable == 0:
            return np.zeros(0, dtype=np.float32)

        samples = _to_float32(raw[:usable], self.width)
        if self.channels > 1:
            samples = samples.reshape(-1, self.channels).mean(axis=1, dtype=np.float32)
        return self._resample(samples)

    def _resample(self, x: NDArray[np.float32]) -> NDArray[np.float32]:
        """Linear interpolation. Good enough for speech going into an ASR model."""
        if self.rate == self.target_rate:
            return x
        buf = x if self._prev is None else np.concatenate(([self._prev], x))
        step = self.rate / self.target_rate
        last = len(buf) - 1
        positions = np.arange(self._pos, last + 1e-9, step)
        out: NDArray[np.float32] = np.asarray(
            np.interp(positions, np.arange(len(buf)), buf), dtype=np.float32
        )
        next_pos = positions[-1] + step if len(positions) else self._pos
        self._pos = next_pos - last
        self._prev = float(buf[-1])
        return out
