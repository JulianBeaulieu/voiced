from __future__ import annotations

import numpy as np
import pytest

from voiced.engines.audio import resample, to_pcm16
from voiced.engines.base import TTS_SAMPLE_RATE


def test_to_pcm16_scales_and_clips() -> None:
    pcm = to_pcm16(np.array([0.0, 0.5, -0.5, 1.0, -1.0, 2.0, -2.0], dtype=np.float32))
    values = np.frombuffer(pcm, dtype="<i2")
    assert values.tolist() == [0, 16383, -16383, 32767, -32767, 32767, -32767]


def test_to_pcm16_flattens_batch_dimension() -> None:
    audio = np.zeros((1, 240), dtype=np.float32)
    assert len(to_pcm16(audio)) == 240 * 2


def test_to_pcm16_empty() -> None:
    assert to_pcm16(np.zeros(0, dtype=np.float32)) == b""
    assert to_pcm16(np.zeros((1, 0), dtype=np.float32)) == b""


def test_resample_same_rate_is_identity() -> None:
    audio = np.linspace(-1, 1, 100, dtype=np.float32)
    out = resample(audio, 24_000, 24_000)
    assert out.dtype == np.float32
    assert np.array_equal(out, audio)


def test_resample_rejects_bad_rates() -> None:
    with pytest.raises(ValueError):
        resample(np.zeros(10, dtype=np.float32), 0, 24_000)


def test_resample_32k_to_24k_keeps_duration_and_tone() -> None:
    pytest.importorskip("scipy")
    src_rate = 32_000
    seconds = 0.5
    t = np.arange(int(src_rate * seconds)) / src_rate
    tone = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    pcm = to_pcm16(tone, src_rate)
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32767
    assert len(samples) == int(TTS_SAMPLE_RATE * seconds)
    # The 440 Hz peak must survive resampling.
    spectrum = np.abs(np.fft.rfft(samples))
    freqs = np.fft.rfftfreq(len(samples), 1 / TTS_SAMPLE_RATE)
    assert abs(freqs[int(np.argmax(spectrum))] - 440) < 5
    assert np.max(np.abs(samples)) == pytest.approx(0.5, abs=0.02)
