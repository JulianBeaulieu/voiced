"""Float audio to the project's PCM format.

Shared by the TTS engines: whatever a model produces (float32 at its native
rate) becomes mono int16 little-endian at :data:`TTS_SAMPLE_RATE`.
"""

from __future__ import annotations

from fractions import Fraction

import numpy as np
from numpy.typing import NDArray

from voiced.engines.base import TTS_SAMPLE_RATE

__all__ = ["resample", "to_pcm16"]


def resample(samples: NDArray[np.floating], src_rate: int, dst_rate: int) -> NDArray[np.float32]:
    """Polyphase resampling with an anti-aliasing filter (``scipy.signal``).

    ``scipy`` is a dependency of ``mlx-audio`` and therefore of the ``tts``
    extra; it is imported lazily so the core package does not need it.
    """
    if src_rate == dst_rate:
        return np.asarray(samples, dtype=np.float32)
    if src_rate <= 0 or dst_rate <= 0:
        raise ValueError("sample rates must be positive")
    from scipy.signal import resample_poly

    ratio = Fraction(dst_rate, src_rate)
    out = resample_poly(np.asarray(samples, dtype=np.float32), ratio.numerator, ratio.denominator)
    return np.asarray(out, dtype=np.float32)


def to_pcm16(samples: NDArray[np.floating], src_rate: int = TTS_SAMPLE_RATE) -> bytes:
    """Mono float audio in [-1, 1] at ``src_rate`` to int16 LE bytes at 24 kHz.

    Multi-dimensional input is flattened (models return ``(1, n)``). Values
    outside [-1, 1] are clipped rather than wrapped.
    """
    flat = np.asarray(samples, dtype=np.float32).reshape(-1)
    if flat.size == 0:
        return b""
    flat = resample(flat, src_rate, TTS_SAMPLE_RATE)
    clipped = np.clip(flat, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()
