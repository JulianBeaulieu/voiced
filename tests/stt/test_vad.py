from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest
from numpy.typing import NDArray

from voiced.config import STTConfig
from voiced.engines.base import STT_SAMPLE_RATE
from voiced.stt.vad import (
    FRAME_SAMPLES,
    Endpointer,
    EndpointEvent,
    SileroDetector,
    create_endpointer,
)
from voiced.testing import EnergyDetector

CHUNK = STT_SAMPLE_RATE // 10  # 100 ms, deliberately not a multiple of the 512-sample frame
FRAME_S = FRAME_SAMPLES / STT_SAMPLE_RATE


def noise(seconds: float, level: float = 0.3, seed: int = 0) -> NDArray[np.float32]:
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(seconds * STT_SAMPLE_RATE)) * level).astype(np.float32)


def silence(seconds: float) -> NDArray[np.float32]:
    return np.zeros(int(seconds * STT_SAMPLE_RATE), dtype=np.float32)


def run(
    endpointer: Endpointer, audio: NDArray[np.float32], chunk: int = CHUNK
) -> list[EndpointEvent]:
    events = []
    for i in range(0, len(audio), chunk):
        events.extend(endpointer.feed(audio[i : i + chunk]))
    return events


def energy_endpointer(silence_ms: int = 200, **kwargs: int) -> Endpointer:
    return Endpointer(EnergyDetector(), silence_ms=silence_ms, **kwargs)


def test_speech_then_silence_triggers_endpoint_after_window() -> None:
    ep = energy_endpointer(silence_ms=200)
    assert ep.silence_s == pytest.approx(6 * FRAME_S)  # 200 ms rounds to 6 frames of 32 ms
    events = run(ep, np.concatenate([noise(1.0), silence(1.0)]))
    assert [e.kind for e in events] == ["speech_start", "speech_end"]
    start, end = events
    assert start.speech_start_s == pytest.approx(0.0, abs=FRAME_S)
    assert start.speech_end_s is None
    assert end.speech_end_s == pytest.approx(1.0, abs=2 * FRAME_S)
    assert end.offset_s == pytest.approx(end.speech_end_s + ep.silence_s)
    assert not ep.in_speech


@pytest.mark.parametrize("silence_ms", [100, 500, 800])
def test_silence_window_is_configurable(silence_ms: int) -> None:
    ep = energy_endpointer(silence_ms=silence_ms)
    events = run(ep, np.concatenate([noise(0.5), silence(1.5)]))
    end = events[-1]
    assert end.kind == "speech_end"
    assert end.offset_s == pytest.approx(0.5 + silence_ms / 1000, abs=2 * FRAME_S)


def test_silence_shorter_than_window_does_not_end_speech() -> None:
    ep = energy_endpointer(silence_ms=300)
    audio = np.concatenate([noise(0.5), silence(0.2), noise(0.5, seed=1), silence(0.2)])
    assert [e.kind for e in run(ep, audio)] == ["speech_start"]
    assert ep.in_speech


def test_no_speech_no_events() -> None:
    ep = energy_endpointer()
    assert run(ep, silence(2.0)) == []
    assert run(ep, noise(2.0, level=0.001)) == []  # below the -40 dBFS floor


def test_short_blip_is_not_speech() -> None:
    ep = energy_endpointer(min_speech_ms=96)
    assert run(ep, np.concatenate([silence(0.5), noise(0.04), silence(1.0)])) == []


def test_two_utterances_in_a_row() -> None:
    ep = energy_endpointer()
    audio = np.concatenate([noise(0.5), silence(0.5), noise(0.5, seed=1), silence(0.5)])
    events = run(ep, audio)
    assert [e.kind for e in events] == ["speech_start", "speech_end", "speech_start", "speech_end"]
    assert events[2].speech_start_s == pytest.approx(1.0, abs=2 * FRAME_S)
    assert events[3].speech_end_s == pytest.approx(1.5, abs=2 * FRAME_S)


def test_all_events_in_one_chunk_are_reported() -> None:
    # speech, a pause longer than the window, and speech again inside one chunk
    ep = energy_endpointer(silence_ms=100)
    audio = np.concatenate([noise(0.3), silence(0.3), noise(0.3, seed=1), silence(0.3)])
    events = ep.feed(audio)
    assert [e.kind for e in events] == ["speech_start", "speech_end", "speech_start", "speech_end"]


def test_reset_clears_state() -> None:
    ep = energy_endpointer()
    run(ep, noise(0.5))
    assert ep.in_speech
    ep.reset()
    assert not ep.in_speech
    assert ep.position_s == 0.0
    kinds = [e.kind for e in run(ep, np.concatenate([noise(0.3), silence(0.5)]))]
    assert kinds == ["speech_start", "speech_end"]


def test_frame_alignment_is_independent_of_chunk_size() -> None:
    audio = np.concatenate([noise(1.0), silence(1.0)])
    ends = []
    for chunk in [160, 512, 1600, 4096]:
        events = run(energy_endpointer(), audio, chunk)
        ends.append(events[-1].speech_end_s)
    assert len(set(ends)) == 1


def test_rejects_bad_parameters() -> None:
    with pytest.raises(ValueError):
        energy_endpointer(silence_ms=-1)
    with pytest.raises(ValueError):
        EnergyDetector(threshold_dbfs=0)
    with pytest.raises(ValueError):
        energy_endpointer().feed(np.zeros((2, 512), np.float32))


def test_create_endpointer_uses_config_window() -> None:
    ep = create_endpointer(STTConfig(vad_silence_ms=500), detector=EnergyDetector())
    assert ep.silence_s == pytest.approx(0.5, abs=FRAME_S)
    events = run(ep, np.concatenate([noise(0.5), silence(1.0)]))
    assert events[-1].offset_s == pytest.approx(1.0, abs=2 * FRAME_S)


def test_create_endpointer_defaults_to_silero_and_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("VOICED_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.setenv("VOICED_STT__VAD_SILENCE_MS", "320")
    ep = create_endpointer()
    assert isinstance(ep._detector, SileroDetector)
    assert ep.silence_s == pytest.approx(0.32)


def test_silero_endpoints_real_speech(speech_with_trailing_silence: NDArray[np.float32]) -> None:
    # The fixture is speech followed by 1 s of silence.
    speech_s = len(speech_with_trailing_silence) / STT_SAMPLE_RATE - 1.0
    ep = Endpointer(silence_ms=200)
    events = run(ep, speech_with_trailing_silence)
    assert events[0].kind == "speech_start"
    end = events[-1]
    assert end.kind == "speech_end"
    assert end.speech_start_s < 0.3
    assert end.speech_end_s is not None
    assert abs(end.speech_end_s - speech_s) < 0.25
    assert end.offset_s == pytest.approx(end.speech_end_s + ep.silence_s)


def test_silero_ignores_noise() -> None:
    ep = Endpointer(silence_ms=200)
    assert run(ep, noise(1.5)) == []
    assert run(ep, silence(1.0)) == []


@pytest.mark.model
def test_silero_is_fast_enough() -> None:
    """Wall-clock budget, so it runs in the local model tier only: a shared CI
    runner VM measured 80 ms per frame where an M1 Max does 0.15 ms."""
    detector = SileroDetector()
    frame = noise(1.0)[:FRAME_SAMPLES]
    t0 = time.perf_counter()
    for _ in range(100):
        detector.probability(frame)
    per_frame = (time.perf_counter() - t0) / 100
    print(f"\nsilero: {per_frame * 1000:.2f} ms per 32 ms frame")
    assert per_frame < 0.005, f"{per_frame * 1000:.1f} ms per 32 ms frame"
