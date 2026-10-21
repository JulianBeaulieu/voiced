from __future__ import annotations

import io
import wave

import numpy as np
import pytest

from voiced.audio import (
    PCMConverter,
    UnsupportedAudioFormat,
    clip_segment,
    float32_to_pcm16,
    pcm16_to_float32,
    resample,
    stt_input,
    wav_header,
)
from voiced.engines.base import AudioInput, EngineError, Segment

RNG = np.random.default_rng(0)


def _tone(n: int, rate: int) -> np.ndarray:
    return (0.5 * np.sin(2 * np.pi * 440 * np.arange(n) / rate)).astype(np.float32)


def _chunks(data: bytes, sizes: list[int]) -> list[bytes]:
    out, i, k = [], 0, 0
    while i < len(data):
        n = sizes[k % len(sizes)]
        out.append(data[i : i + n])
        i, k = i + n, k + 1
    return out


def test_pcm16_round_trip() -> None:
    x = RNG.uniform(-1, 1, 4000).astype(np.float32)
    back = pcm16_to_float32(float32_to_pcm16(x))
    assert back.dtype == np.float32
    # Encode scales by 32767, decode by 32768: at most two LSBs apart.
    assert np.max(np.abs(back - x)) < 2 / 32768


def test_float32_to_pcm16_clips_and_pcm16_drops_odd_byte() -> None:
    x = np.array([2.0, -2.0, 0.0, np.nan, np.inf], dtype=np.float32)
    pcm = float32_to_pcm16(x)
    assert np.frombuffer(pcm, "<i2").tolist() == [32767, -32767, 0, 0, 32767]
    assert len(pcm16_to_float32(pcm + b"\x01")) == 5


@pytest.mark.parametrize("width", [1, 2, 4])
@pytest.mark.parametrize("sizes", [[1], [3, 7], [1023]])
def test_chunking_does_not_change_output(width: int, sizes: list[int]) -> None:
    x = _tone(3000, 22050)
    data = _encode(x, width)
    whole = PCMConverter(22050, width, 1).convert(data)
    conv = PCMConverter(22050, width, 1)
    parts = np.concatenate([conv.convert(c) for c in _chunks(data, sizes)])
    np.testing.assert_allclose(parts, whole, atol=1e-6)


@pytest.mark.parametrize("rate", [8000, 16000, 22050, 44100, 48000])
def test_resample_length(rate: int) -> None:
    n = rate  # one second
    out = PCMConverter(rate, 2, 1).convert(float32_to_pcm16(_tone(n, rate)))
    assert abs(len(out) - 16000) <= 1


def test_downmix_averages_channels() -> None:
    left = np.full(100, 0.5, dtype=np.float32)
    right = np.full(100, -0.25, dtype=np.float32)
    stereo = float32_to_pcm16(np.stack([left, right], axis=1).reshape(-1))
    out = PCMConverter(16000, 2, 2).convert(stereo)
    assert len(out) == 100
    np.testing.assert_allclose(out, 0.125, atol=1e-4)


def test_partial_frames_carry_over() -> None:
    conv = PCMConverter(16000, 2, 2)
    pcm = float32_to_pcm16(np.array([0.5, -0.5], dtype=np.float32))
    stereo = pcm + pcm  # two stereo frames
    assert len(conv.convert(stereo[:3])) == 0
    out = conv.convert(stereo[3:])
    assert len(out) == 2


@pytest.mark.parametrize(("rate", "width", "channels"), [(16000, 3, 1), (16000, 2, 0), (0, 2, 1)])
def test_unsupported_format_is_a_client_error(rate: int, width: int, channels: int) -> None:
    with pytest.raises(UnsupportedAudioFormat) as info:
        PCMConverter(rate, width, channels)
    assert isinstance(info.value, EngineError)


def test_wav_header_known_length_parses() -> None:
    pcm = float32_to_pcm16(_tone(2400, 24000))
    with wave.open(io.BytesIO(wav_header(24000, 1, 2, len(pcm)) + pcm)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (24000, 1, 2)
        assert w.readframes(w.getnframes()) == pcm


def test_wav_header_streaming_form() -> None:
    h = wav_header()
    assert len(h) == 44
    assert h[4:8] == b"\xff\xff\xff\xff" and h[40:44] == b"\xff\xff\xff\xff"


def test_resample_same_rate_is_identity() -> None:
    audio = np.linspace(-1, 1, 100, dtype=np.float32)
    out = resample(audio, 24_000, 24_000)
    assert out.dtype == np.float32
    assert np.array_equal(out, audio)


def test_resample_rejects_nonpositive_rates() -> None:
    with pytest.raises(ValueError):
        resample(np.zeros(10, dtype=np.float32), 0, 24_000)
    with pytest.raises(ValueError):
        resample(np.zeros(10, dtype=np.float32), 32_000, 0)


def test_resampled_chunk_durations_add_up() -> None:
    """Chunks resampled separately add up to the whole within one sample per chunk."""
    pytest.importorskip("scipy")
    for n in [160, 321, 1600, 16_001]:
        out = resample(np.zeros(n, dtype=np.float32), 32_000, 24_000)
        assert abs(len(out) - n * 3 / 4) <= 1


def test_resample_flattens_batch_dimension() -> None:
    pytest.importorskip("scipy")
    out = resample(np.zeros((1, 3200), dtype=np.float32), 32_000, 24_000)
    assert out.shape == (2400,)


def test_resample_32k_to_24k_keeps_the_tone() -> None:
    pytest.importorskip("scipy")
    src_rate, seconds = 32_000, 0.5
    t = np.arange(int(src_rate * seconds)) / src_rate
    tone = (0.5 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    out = resample(tone, src_rate, 24_000)
    assert len(out) == int(24_000 * seconds)
    freqs = np.fft.rfftfreq(len(out), 1 / 24_000)
    assert abs(freqs[int(np.argmax(np.abs(np.fft.rfft(out))))] - 440) < 5
    assert np.max(np.abs(out)) == pytest.approx(0.5, abs=0.02)


def _encode(x: np.ndarray, width: int) -> bytes:
    if width == 1:
        return np.clip(np.rint(x * 127 + 128), 0, 255).astype(np.uint8).tobytes()
    if width == 2:
        return float32_to_pcm16(x)
    return np.rint(x * 2147483647).astype("<i4").tobytes()


def test_clip_segment_keeps_times_inside_the_input() -> None:
    assert clip_segment(0.1234, 0.5678, "a", 1.0) == Segment(0.123, 0.568, "a")
    assert clip_segment(-0.2, 0.3, "a", 1.0) == Segment(0.0, 0.3, "a")
    assert clip_segment(0.5, 0.2, "a", 1.0) == Segment(0.5, 0.5, "a")  # end never before start
    assert clip_segment(0.8, 1.4, "a", 1.0) == Segment(0.8, 1.0, "a")
    assert clip_segment(1.2, 1.4, "a", 1.0) == Segment(1.0, 1.0, "a")
    assert clip_segment(1.2, 1.4, "a", 0.0) == Segment(1.2, 1.4, "a")  # no known length


def test_stt_input_checks_rate_and_samples() -> None:
    tone = _tone(16_000, 16_000)
    with pytest.raises(EngineError, match="expected 16000 Hz audio, got 8000 Hz"):
        stt_input(AudioInput(tone, sample_rate=8000))
    with pytest.raises(EngineError, match="float32"):
        stt_input(AudioInput(np.zeros(10, dtype=np.int16)))
    out = stt_input(AudioInput(tone.astype(np.float64)))
    assert out is not None
    assert out.dtype == np.float32
    np.testing.assert_array_equal(out, tone)


def test_stt_input_is_none_when_there_is_nothing_to_transcribe() -> None:
    assert stt_input(AudioInput(np.zeros(16_000, dtype=np.float32))) is None
    assert stt_input(AudioInput(np.zeros(0, dtype=np.float32))) is None
    short = _tone(256, 16_000)
    assert stt_input(AudioInput(short)) is not None
    assert stt_input(AudioInput(short), min_samples=257) is None
    assert stt_input(AudioInput(short), min_samples=256) is not None
