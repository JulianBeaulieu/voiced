from __future__ import annotations

import numpy as np
import pytest
from numpy.typing import NDArray

from voiced.engines.base import STT_SAMPLE_RATE
from voiced.stt.vad import (
    FRAME_SAMPLES,
    EndpointEvent,
    EnergyDetector,
    SilenceEndpointer,
    SileroDetector,
    create_endpointer,
    silero_available,
)

CHUNK = STT_SAMPLE_RATE // 10  # 100 ms, deliberately not a multiple of the 512-sample frame
FRAME_S = FRAME_SAMPLES / STT_SAMPLE_RATE


def noise(seconds: float, level: float = 0.3, seed: int = 0) -> NDArray[np.float32]:
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(seconds * STT_SAMPLE_RATE)) * level).astype(np.float32)


def silence(seconds: float) -> NDArray[np.float32]:
    return np.zeros(int(seconds * STT_SAMPLE_RATE), dtype=np.float32)


def run(endpointer: SilenceEndpointer, audio: NDArray[np.float32]) -> list[EndpointEvent]:
    events = []
    for i in range(0, len(audio), CHUNK):
        event = endpointer.feed(audio[i : i + CHUNK])
        if event is not None:
            events.append(event)
    return events


def energy_endpointer(**kwargs: object) -> SilenceEndpointer:
    return SilenceEndpointer(EnergyDetector(), **kwargs)  # type: ignore[arg-type]


def test_speech_then_silence_triggers_endpoint_after_window() -> None:
    ep = energy_endpointer(silence_ms=200)
    events = run(ep, np.concatenate([noise(1.0), silence(1.0)]))
    assert [e.kind for e in events] == ["speech_start", "speech_end"]
    start, end = events
    assert start.speech_start_s == pytest.approx(0.0, abs=FRAME_S)
    assert start.speech_end_s is None
    # End of speech is reported once 200 ms of silence have elapsed after it.
    assert end.speech_end_s == pytest.approx(1.0, abs=2 * FRAME_S)
    assert end.offset_s == pytest.approx(1.2, abs=2 * FRAME_S + CHUNK / STT_SAMPLE_RATE)
    assert end.offset_s - end.speech_end_s >= 0.2
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
    events = run(ep, audio)
    assert [e.kind for e in events] == ["speech_start"]
    assert ep.in_speech


def test_no_speech_no_events() -> None:
    ep = energy_endpointer(silence_ms=200)
    assert run(ep, silence(2.0)) == []
    assert run(ep, noise(2.0, level=0.001)) == []  # below the -40 dBFS floor


def test_short_blip_is_not_speech() -> None:
    ep = energy_endpointer(silence_ms=200, min_speech_ms=96)
    audio = np.concatenate([silence(0.5), noise(0.04), silence(1.0)])
    assert run(ep, audio) == []


def test_two_utterances_in_a_row() -> None:
    ep = energy_endpointer(silence_ms=200)
    audio = np.concatenate([noise(0.5), silence(0.5), noise(0.5, seed=1), silence(0.5)])
    events = run(ep, audio)
    assert [e.kind for e in events] == ["speech_start", "speech_end", "speech_start", "speech_end"]
    assert events[2].speech_start_s == pytest.approx(1.0, abs=2 * FRAME_S)
    assert events[3].speech_end_s == pytest.approx(1.5, abs=2 * FRAME_S)


def test_reset_clears_state() -> None:
    ep = energy_endpointer(silence_ms=200)
    run(ep, noise(0.5))
    assert ep.in_speech
    ep.reset()
    assert not ep.in_speech
    assert ep.position_s == 0.0
    assert [e.kind for e in run(ep, np.concatenate([noise(0.3), silence(0.5)]))] == [
        "speech_start",
        "speech_end",
    ]


def test_frame_alignment_is_independent_of_chunk_size() -> None:
    audio = np.concatenate([noise(1.0), silence(1.0)])
    ends = []
    for chunk in [160, 512, 1600, 4096]:
        ep = energy_endpointer(silence_ms=200)
        for i in range(0, len(audio), chunk):
            event = ep.feed(audio[i : i + chunk])
            if event is not None and event.kind == "speech_end":
                ends.append(event.speech_end_s)
    assert len(ends) == 4
    assert max(ends) - min(ends) <= FRAME_S


class _Turn:
    """Stub semantic end-of-turn model: says 'not done' for the first N asks."""

    def __init__(self, incomplete_answers: int) -> None:
        self.incomplete_answers = incomplete_answers
        self.calls: list[int] = []

    def is_turn_complete(self, utterance: NDArray[np.float32]) -> bool:
        self.calls.append(len(utterance))
        return len(self.calls) > self.incomplete_answers


def test_turn_detector_can_extend_the_silence_window() -> None:
    turn = _Turn(incomplete_answers=1)
    ep = energy_endpointer(silence_ms=200, max_silence_ms=1000, turn_detector=turn)
    events = run(ep, np.concatenate([noise(0.5), silence(1.5)]))
    end = events[-1]
    assert end.kind == "speech_end"
    # Asked after one silence window (said no) and again after two (said yes).
    # 200 ms rounds up to 7 frames of 32 ms, so the end lands at 0.5 + 2 * 0.224.
    assert end.offset_s == pytest.approx(0.95, abs=2 * FRAME_S)
    assert end.speech_end_s == pytest.approx(0.5, abs=2 * FRAME_S)
    assert len(turn.calls) == 2
    # The utterance handed to the detector covers the speech so far.
    assert turn.calls[0] >= int(0.5 * STT_SAMPLE_RATE) - FRAME_SAMPLES


def test_turn_detector_is_capped_by_max_silence() -> None:
    turn = _Turn(incomplete_answers=100)
    ep = energy_endpointer(silence_ms=200, max_silence_ms=600, turn_detector=turn)
    events = run(ep, np.concatenate([noise(0.5), silence(1.5)]))
    end = events[-1]
    assert end.kind == "speech_end"
    assert end.offset_s == pytest.approx(1.1, abs=2 * FRAME_S)


def test_rejects_bad_parameters() -> None:
    with pytest.raises(ValueError):
        energy_endpointer(silence_ms=-1)
    with pytest.raises(ValueError):
        energy_endpointer(silence_ms=300, max_silence_ms=200)
    with pytest.raises(ValueError):
        EnergyDetector(threshold=0)
    with pytest.raises(ValueError):
        energy_endpointer().feed(np.zeros((2, 512), np.float32))


def test_create_endpointer_energy_backend() -> None:
    ep = create_endpointer(250, backend="energy")
    assert isinstance(ep._detector, EnergyDetector)
    events = run(ep, np.concatenate([noise(0.5), silence(1.0)]))
    assert events[-1].offset_s == pytest.approx(0.75, abs=2 * FRAME_S)


@pytest.mark.skipif(not silero_available(), reason="pysilero-vad not installed")
def test_silero_is_the_default_backend() -> None:
    assert isinstance(create_endpointer(200)._detector, SileroDetector)


@pytest.mark.skipif(not silero_available(), reason="pysilero-vad not installed")
def test_silero_endpoints_real_speech(speech_with_trailing_silence: NDArray[np.float32]) -> None:
    # The fixture is 1.85 s of speech followed by 1 s of silence.
    ep = create_endpointer(200, backend="silero")
    events = run(ep, speech_with_trailing_silence)
    kinds = [e.kind for e in events]
    assert kinds[0] == "speech_start"
    assert kinds[-1] == "speech_end"
    end = events[-1]
    assert end.speech_start_s < 0.3
    assert end.speech_end_s is not None and 1.6 < end.speech_end_s < 2.1
    assert end.offset_s - end.speech_end_s >= 0.2
    assert end.offset_s < 2.4


@pytest.mark.skipif(not silero_available(), reason="pysilero-vad not installed")
def test_silero_ignores_noise() -> None:
    ep = create_endpointer(200, backend="silero")
    assert run(ep, noise(1.5)) == []
    assert run(ep, silence(1.0)) == []


@pytest.mark.skipif(not silero_available(), reason="pysilero-vad not installed")
def test_silero_is_fast_enough() -> None:
    import time

    detector = SileroDetector()
    frame = noise(1.0)[:FRAME_SAMPLES]
    t0 = time.perf_counter()
    for _ in range(100):
        detector.probability(frame)
    per_frame = (time.perf_counter() - t0) / 100
    assert per_frame < 0.005, f"{per_frame * 1000:.1f} ms per 32 ms frame"
