from __future__ import annotations

import numpy as np
import pytest

from voiced.engines.base import STT_SAMPLE_RATE
from voiced.stt.audio import (
    _resample_linear,
    ensure_stt_input,
    float32_to_pcm16,
    pcm16_to_float32,
    resample,
    to_mono,
)


def sine(freq: float, rate: int, seconds: float = 0.5) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    return (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def dominant_frequency(samples: np.ndarray, rate: int) -> float:
    spectrum = np.abs(np.fft.rfft(samples))
    return float(np.fft.rfftfreq(len(samples), 1 / rate)[np.argmax(spectrum)])


def test_pcm16_round_trip() -> None:
    original = sine(440, STT_SAMPLE_RATE)
    restored = pcm16_to_float32(float32_to_pcm16(original))
    assert restored.dtype == np.float32
    assert len(restored) == len(original)
    assert np.max(np.abs(restored - original)) < 1e-4


def test_pcm16_accepts_arrays_and_scales_full_range() -> None:
    assert pcm16_to_float32(np.array([-32768, 0, 32767], dtype=np.int16)).tolist() == pytest.approx(
        [-1.0, 0.0, 32767 / 32768]
    )


def test_float32_to_pcm16_clips() -> None:
    out = np.frombuffer(float32_to_pcm16(np.array([2.0, -2.0], dtype=np.float32)), dtype="<i2")
    assert out.tolist() == [32767, -32767]


def test_to_mono_averages_frames_by_channels() -> None:
    stereo = np.stack([np.ones(100, np.float32), np.zeros(100, np.float32)], axis=1)
    mono = to_mono(stereo)
    assert mono.shape == (100,)
    assert np.allclose(mono, 0.5)


def test_to_mono_accepts_channels_by_frames() -> None:
    stereo = np.stack([np.ones(100, np.float32), np.zeros(100, np.float32)], axis=0)
    assert to_mono(stereo).shape == (100,)


def test_to_mono_passes_mono_through_and_rejects_3d() -> None:
    mono = np.zeros(10, np.float32)
    assert to_mono(mono) is not None and to_mono(mono).shape == (10,)
    with pytest.raises(ValueError):
        to_mono(np.zeros((2, 2, 2), np.float32))


@pytest.mark.parametrize("from_rate", [8000, 22050, 44100, 48000])
def test_resample_keeps_tone_and_length(from_rate: int) -> None:
    tone = sine(440, from_rate)
    out = resample(tone, from_rate, STT_SAMPLE_RATE)
    assert out.dtype == np.float32
    expected = len(tone) * STT_SAMPLE_RATE / from_rate
    assert abs(len(out) - expected) <= 2
    assert abs(dominant_frequency(out, STT_SAMPLE_RATE) - 440) < 10


def test_resample_same_rate_is_identity() -> None:
    tone = sine(440, STT_SAMPLE_RATE)
    out = resample(tone, STT_SAMPLE_RATE, STT_SAMPLE_RATE)
    assert np.array_equal(out, tone)


def test_resample_linear_fallback_keeps_tone() -> None:
    tone = sine(440, 48000)
    out = _resample_linear(tone, 48000, STT_SAMPLE_RATE)
    assert len(out) == len(tone) // 3
    assert abs(dominant_frequency(out, STT_SAMPLE_RATE) - 440) < 10


def test_resample_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        resample(np.zeros((2, 10), np.float32), 8000)
    with pytest.raises(ValueError):
        resample(np.zeros(10, np.float32), 0)


def test_ensure_stt_input_combines_steps() -> None:
    stereo = np.stack([sine(440, 48000), sine(440, 48000)], axis=1)
    out = ensure_stt_input(stereo, 48000)
    assert out.ndim == 1 and out.dtype == np.float32
    assert abs(len(out) - len(stereo) / 3) <= 2
