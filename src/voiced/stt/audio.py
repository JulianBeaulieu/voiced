"""Sample-format helpers for the STT side.

Engines receive mono float32 PCM at 16 kHz (see ``voiced.engines.base``).
These helpers convert from the formats that arrive at the edges, 16-bit PCM
from Wyoming and other sample rates from decoded uploads, into that
convention. They are pure numpy so they work in CI; ``soxr`` is used for
resampling when it is installed (it comes with the ``stt`` extra) and a linear
interpolation fallback is used otherwise.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from voiced.engines.base import STT_SAMPLE_RATE

__all__ = [
    "ensure_stt_input",
    "float32_to_pcm16",
    "pcm16_to_float32",
    "resample",
    "to_mono",
]

_PCM16_SCALE = 32768.0


PCMBytes = bytes | bytearray | memoryview


def pcm16_to_float32(data: PCMBytes | NDArray[np.int16]) -> NDArray[np.float32]:
    """Convert signed 16-bit little-endian PCM to float32 in [-1.0, 1.0)."""
    if isinstance(data, np.ndarray):
        ints = data.astype("<i2", copy=False)
    else:
        ints = np.frombuffer(data, dtype="<i2")
    out: NDArray[np.float32] = ints.astype(np.float32) / _PCM16_SCALE
    return out


def float32_to_pcm16(samples: NDArray[np.floating]) -> bytes:
    """Convert float samples to signed 16-bit little-endian PCM bytes, clipping to range."""
    clipped = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


def to_mono(samples: NDArray[np.floating]) -> NDArray[np.float32]:
    """Average interleaved channels down to one. One-dimensional input passes through.

    Two-dimensional input is treated as ``(frames, channels)`` when the second
    axis is the shorter one, which is what ``soundfile`` and most decoders
    produce, and as ``(channels, frames)`` otherwise.
    """
    arr = np.asarray(samples, dtype=np.float32)
    if arr.ndim == 1:
        return arr
    if arr.ndim != 2:
        raise ValueError(f"expected 1-D or 2-D samples, got shape {arr.shape}")
    axis = 1 if arr.shape[1] <= arr.shape[0] else 0
    mono: NDArray[np.float32] = arr.mean(axis=axis, dtype=np.float32)
    return mono


def resample(
    samples: NDArray[np.floating], from_rate: int, to_rate: int = STT_SAMPLE_RATE
) -> NDArray[np.float32]:
    """Resample mono float samples from ``from_rate`` to ``to_rate`` Hz.

    Uses ``soxr`` (band-limited, high quality) when available. The fallback is
    linear interpolation, which is adequate for speech going to 16 kHz but does
    not low-pass before downsampling; install the ``stt`` extra for the real
    thing.
    """
    if from_rate <= 0 or to_rate <= 0:
        raise ValueError("sample rates must be positive")
    arr = np.ascontiguousarray(samples, dtype=np.float32)
    if arr.ndim != 1:
        raise ValueError(f"expected mono 1-D samples, got shape {arr.shape}")
    if from_rate == to_rate or len(arr) == 0:
        return arr
    try:
        import soxr
    except ImportError:
        return _resample_linear(arr, from_rate, to_rate)
    out: NDArray[np.float32] = np.ascontiguousarray(
        soxr.resample(arr, from_rate, to_rate, quality="HQ"), dtype=np.float32
    )
    return out


def _resample_linear(arr: NDArray[np.float32], from_rate: int, to_rate: int) -> NDArray[np.float32]:
    n_out = max(1, round(len(arr) * to_rate / from_rate))
    src = np.arange(len(arr), dtype=np.float64)
    dst = np.linspace(0.0, len(arr) - 1, n_out, dtype=np.float64)
    out: NDArray[np.float32] = np.interp(dst, src, arr).astype(np.float32)
    return out


def ensure_stt_input(samples: NDArray[np.floating], sample_rate: int) -> NDArray[np.float32]:
    """Mono float32 at :data:`STT_SAMPLE_RATE`, whatever came in."""
    return resample(to_mono(samples), sample_rate, STT_SAMPLE_RATE)
