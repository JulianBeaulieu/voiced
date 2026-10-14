"""Parakeet engine tests.

The first half runs anywhere (metadata, registry, input validation, the
session's windowing logic). The second half is marked ``model`` and needs the
real weights: ``VOICED_MODEL_TESTS=1 uv run pytest tests/stt -m model -s``.
"""

from __future__ import annotations

import asyncio
import resource
import time
from types import SimpleNamespace

import numpy as np
import pytest
from numpy.typing import NDArray

from tests.stt.conftest import similarity, words
from voiced.engines.base import (
    STT_SAMPLE_RATE,
    AudioInput,
    EngineError,
    EngineNotLoaded,
    STTEngine,
    UnsupportedLanguage,
)
from voiced.engines.parakeet import (
    PARAKEET_V3_LANGUAGES,
    ParakeetEngine,
    ParakeetStreamSession,
    _check_samples,
    _to_transcript,
)
from voiced.engines.registry import get_stt_engine

EXPECTED = "turn on the kitchen lights and set the thermostat to twenty degrees"
EXPECTED_WORDS = {"kitchen", "lights", "thermostat", "degrees"}
EXPECTED_TRAILING = "what is the weather like tomorrow morning"
CHUNK = STT_SAMPLE_RATE // 10  # 100 ms, what the API layers will send


def assert_transcribed(text: str, expected: str, key_words: set[str] | None = None) -> None:
    got = set(words(text))
    missing = (key_words or set()) - got
    assert not missing, f"missing {missing} in {text!r}"
    # "20" versus "twenty" and similar are fine; dropped or invented words are not.
    assert similarity(text, expected) >= 0.8, f"{text!r} does not match {expected!r}"


# --- no model needed --------------------------------------------------------


def test_metadata() -> None:
    assert ParakeetEngine.name == "parakeet"
    assert ParakeetEngine.model_id == "mlx-community/parakeet-tdt-0.6b-v3"
    assert ParakeetEngine.license == "CC-BY-4.0"
    assert ParakeetEngine.languages[0] == "en"
    assert len(ParakeetEngine.languages) == 25
    assert len(set(ParakeetEngine.languages)) == 25
    assert {"en", "de", "fr", "es", "it"} <= set(PARAKEET_V3_LANGUAGES)
    assert all(code == code.lower() and len(code) == 2 for code in PARAKEET_V3_LANGUAGES)


def test_registered_as_entry_point() -> None:
    assert get_stt_engine("parakeet") is ParakeetEngine
    assert issubclass(ParakeetEngine, STTEngine)


def test_model_id_override() -> None:
    assert ParakeetEngine("some/other-model").model_id == "some/other-model"
    assert ParakeetEngine().model_id == ParakeetEngine.model_id


def test_pass_bounds_validated() -> None:
    with pytest.raises(ValueError):
        ParakeetEngine(min_pass_s=0.5, max_pass_s=0.2)


def test_language_normalization() -> None:
    assert ParakeetEngine.normalize_language(None) is None
    assert ParakeetEngine.normalize_language("en") == "en"
    assert ParakeetEngine.normalize_language("en-US") == "en"
    assert ParakeetEngine.normalize_language("DE_de") == "de"
    for bad in ["ja", "zh-CN", "xx", ""]:
        with pytest.raises(UnsupportedLanguage):
            ParakeetEngine.normalize_language(bad)


async def test_methods_before_load_raise() -> None:
    engine = ParakeetEngine()
    audio = AudioInput(samples=np.zeros(STT_SAMPLE_RATE, dtype=np.float32))
    with pytest.raises(EngineNotLoaded):
        await engine.transcribe(audio)
    with pytest.raises(EngineNotLoaded):
        await engine.stream()


def test_check_samples_rejects_wrong_shapes_and_types() -> None:
    assert _check_samples(np.zeros(10, dtype=np.float64)).dtype == np.float32
    with pytest.raises(EngineError):
        _check_samples(np.zeros((10, 2), dtype=np.float32))
    with pytest.raises(EngineError):
        _check_samples(np.zeros(10, dtype=np.int16))


def test_to_transcript_normalizes_and_clamps() -> None:
    result = SimpleNamespace(
        text="  Hello   there.  General Kenobi. ",
        sentences=[
            SimpleNamespace(text=" Hello   there.", start=0.0, end=0.8),
            SimpleNamespace(text=" General Kenobi.", start=0.9, end=2.5),
            SimpleNamespace(text="   ", start=2.5, end=2.6),
        ],
    )
    transcript = _to_transcript(result, "en", 2.0)
    assert transcript.text == "Hello there. General Kenobi."
    assert transcript.language == "en"
    assert transcript.duration_s == 2.0
    assert [s.text for s in transcript.segments] == ["Hello there.", "General Kenobi."]
    assert transcript.segments[1].end == 2.0  # clamped to the input duration


MIN_PASS_S = 0.32
MAX_PASS_S = 0.60


def _session(
    min_pass_s: float = MIN_PASS_S, max_pass_s: float = MAX_PASS_S
) -> ParakeetStreamSession:
    return ParakeetStreamSession(
        SimpleNamespace(time_ratio=0.08),
        "en",
        min_pass=int(min_pass_s * STT_SAMPLE_RATE),
        max_pass=int(max_pass_s * STT_SAMPLE_RATE),
    )


def _push(session: ParakeetStreamSession, seconds: float) -> None:
    n = int(seconds * STT_SAMPLE_RATE)
    session._pending.append(np.ones(n, dtype=np.float32))
    session._pending_samples += n
    session.samples_added += n


def test_engine_defaults_match_measured_bounds() -> None:
    from voiced.engines import parakeet

    assert parakeet.MIN_PASS_S == MIN_PASS_S
    assert parakeet.MAX_PASS_S == MAX_PASS_S


def test_take_window_waits_for_minimum_pass() -> None:
    session = _session()
    for _ in range(3):
        _push(session, 0.1)
        assert session._take_window() is None
    _push(session, 0.1)
    window = session._take_window()
    assert window is not None
    real, audio = window
    assert real == len(audio) == int(0.4 * STT_SAMPLE_RATE)
    assert session._take_window() is None


@pytest.mark.parametrize(
    ("backlog_s", "expected_passes"), [(0.65, 2), (1.1, 2), (1.3, 3), (5.0, 9)]
)
def test_take_window_splits_backlog_evenly(backlog_s: float, expected_passes: int) -> None:
    session = _session()
    _push(session, backlog_s)
    sizes = []
    while (window := session._take_window()) is not None:
        real, audio = window
        assert real == len(audio)
        sizes.append(real / STT_SAMPLE_RATE)
    assert len(sizes) == expected_passes
    assert all(MIN_PASS_S <= s <= MAX_PASS_S for s in sizes)
    assert sum(sizes) == pytest.approx(backlog_s, abs=1e-3)


def test_take_window_flush_pads_short_tail() -> None:
    session = _session()
    _push(session, 0.05)
    assert session._take_window() is None
    session._flush = True
    window = session._take_window()
    assert window is not None
    real, audio = window
    assert real == int(0.05 * STT_SAMPLE_RATE)
    assert len(audio) == int(MIN_PASS_S * STT_SAMPLE_RATE)
    assert np.all(audio[real:] == 0)
    assert session._take_window() is None


async def test_session_rejects_use_after_close() -> None:
    session = _session()
    await session.close()
    with pytest.raises(EngineError):
        await session.add_audio(np.zeros(CHUNK, dtype=np.float32))
    with pytest.raises(EngineError):
        await session.finalize()
    await session.close()  # idempotent


async def test_empty_session_finalizes_to_empty_transcript() -> None:
    session = _session()
    transcript = await session.finalize()
    assert transcript.text == ""
    assert transcript.duration_s == 0.0
    assert transcript.segments == ()


# --- real model -------------------------------------------------------------


@pytest.fixture(scope="module")
def engine() -> ParakeetEngine:
    engine = ParakeetEngine()
    t0 = time.perf_counter()
    asyncio.run(engine.load())
    print(
        f"\nparakeet load {engine.load_seconds:.1f}s + warm-up {engine.warmup_seconds:.1f}s "
        f"(total {time.perf_counter() - t0:.1f}s)"
    )
    return engine


@pytest.mark.model
async def test_batch_transcript(engine: ParakeetEngine, speech: NDArray[np.float32]) -> None:
    t0 = time.perf_counter()
    transcript = await engine.transcribe(AudioInput(speech), "en")
    elapsed = time.perf_counter() - t0
    clip_s = len(speech) / STT_SAMPLE_RATE
    print(f"batch {clip_s:.2f}s clip in {elapsed * 1000:.0f} ms: {transcript.text!r}")
    assert_transcribed(transcript.text, EXPECTED, EXPECTED_WORDS)
    assert transcript.language == "en"
    assert transcript.duration_s == pytest.approx(len(speech) / STT_SAMPLE_RATE)
    assert transcript.segments, "Parakeet TDT should give timed segments"
    assert " ".join(s.text for s in transcript.segments) == transcript.text
    last_end = 0.0
    for segment in transcript.segments:
        assert 0.0 <= segment.start <= segment.end <= transcript.duration_s
        assert segment.start >= last_end
        last_end = segment.end
    assert elapsed < 1.0


@pytest.mark.model
async def test_batch_without_language_hint(
    engine: ParakeetEngine, speech_with_trailing_silence: NDArray[np.float32]
) -> None:
    transcript = await engine.transcribe(AudioInput(speech_with_trailing_silence))
    assert_transcribed(transcript.text, EXPECTED_TRAILING, {"weather", "tomorrow", "morning"})
    assert transcript.language is None


@pytest.mark.model
async def test_silent_and_empty_input(engine: ParakeetEngine) -> None:
    for samples in [
        np.zeros(0, dtype=np.float32),
        np.zeros(100, dtype=np.float32),
        np.zeros(STT_SAMPLE_RATE, dtype=np.float32),
        (np.random.default_rng(0).standard_normal(STT_SAMPLE_RATE) * 1e-4).astype(np.float32),
    ]:
        transcript = await engine.transcribe(AudioInput(samples))
        assert transcript.text == ""
        assert transcript.segments == ()
        assert transcript.duration_s == pytest.approx(len(samples) / STT_SAMPLE_RATE)


@pytest.mark.model
async def test_batch_rejects_bad_language_and_rate(
    engine: ParakeetEngine, speech: NDArray[np.float32]
) -> None:
    with pytest.raises(UnsupportedLanguage):
        await engine.transcribe(AudioInput(speech), "ja")
    with pytest.raises(EngineError):
        await engine.transcribe(AudioInput(speech, sample_rate=8000))
    with pytest.raises(UnsupportedLanguage):
        await engine.stream("xx")


@pytest.mark.model
async def test_streaming_partial_then_finalize(
    engine: ParakeetEngine, speech: NDArray[np.float32]
) -> None:
    add_times: list[float] = []
    partials: list[str] = []
    session = await engine.stream("en")
    assert isinstance(session, ParakeetStreamSession)
    async with session:
        t0 = time.perf_counter()
        for i in range(0, len(speech), CHUNK):
            t = time.perf_counter()
            await session.add_audio(speech[i : i + CHUNK])
            add_times.append(time.perf_counter() - t)
            partials.append(session.partial())
            # Real-time pacing, as a microphone client would deliver it.
            await asyncio.sleep(max(0.0, t0 + (i + CHUNK) / STT_SAMPLE_RATE - time.perf_counter()))
        backlog_after_speech = session.backlog_s
        # The VAD outside now waits for its 200 ms silence window while the
        # client keeps sending (silent) audio.
        for _ in range(2):
            await session.add_audio(np.zeros(CHUNK, dtype=np.float32))
            await asyncio.sleep(0.1)
        t = time.perf_counter()
        transcript = await session.finalize()
        finalize_s = time.perf_counter() - t
    print(
        f"stream: max add_audio {max(add_times) * 1000:.2f} ms, backlog after speech "
        f"{backlog_after_speech * 1000:.0f} ms, {session.passes} passes "
        f"(last {session.last_pass_s * 1000:.0f} ms), finalize {finalize_s * 1000:.0f} ms, "
        f"first partial after {next(i for i, p in enumerate(partials) if p) + 1} chunks: "
        f"{transcript.text!r}"
    )
    assert any(partials), "no partial hypothesis before finalize"
    assert max(add_times) < 0.02, "add_audio must not block on the encoder"
    assert backlog_after_speech < 1.0, "encoder passes fell behind real time"
    assert_transcribed(transcript.text, EXPECTED, EXPECTED_WORDS)
    assert transcript.language == "en"
    assert transcript.duration_s == pytest.approx((len(speech) + 2 * CHUNK) / STT_SAMPLE_RATE)
    assert transcript.segments and transcript.segments[0].start < 0.5
    # finalize is the drain: whatever is still buffered gets one encoder pass.
    assert finalize_s < 0.8


@pytest.mark.model
async def test_finalize_latency_when_drained(
    engine: ParakeetEngine, speech_with_trailing_silence: NDArray[np.float32]
) -> None:
    """The reference number (3 ms) is finalize after the encoder has seen everything."""
    session = await engine.stream("en")
    async with session:
        for i in range(0, len(speech_with_trailing_silence), CHUNK):
            await session.add_audio(speech_with_trailing_silence[i : i + CHUNK])
            await asyncio.sleep(CHUNK / STT_SAMPLE_RATE)
        # Let the pass loop catch up; only a sub-minimum tail can remain.
        for _ in range(100):
            if session.backlog_s < MIN_PASS_S:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.4)
        tail_s = session.backlog_s
        t = time.perf_counter()
        transcript = await session.finalize()
        finalize_s = time.perf_counter() - t
    pass_s = session.last_pass_s
    overhead_s = finalize_s - pass_s
    print(
        f"finalize with {tail_s * 1000:.0f} ms tail: {finalize_s * 1000:.0f} ms total, "
        f"of which encoder pass {pass_s * 1000:.0f} ms, overhead {overhead_s * 1000:.1f} ms"
    )
    assert_transcribed(transcript.text, EXPECTED_TRAILING, {"weather", "tomorrow", "morning"})
    assert overhead_s < 0.05


@pytest.mark.model
async def test_stream_whole_clip_at_once(
    engine: ParakeetEngine, speech: NDArray[np.float32]
) -> None:
    """A client that uploads faster than real time (Wyoming with a file) still works."""
    session = await engine.stream("en")
    async with session:
        await session.add_audio(speech)
        t = time.perf_counter()
        transcript = await session.finalize()
        burst_ms = (time.perf_counter() - t) * 1000
        print(f"burst finalize of {len(speech) / STT_SAMPLE_RATE:.1f}s: {burst_ms:.0f} ms")
    assert_transcribed(transcript.text, EXPECTED, EXPECTED_WORDS)
    assert session.passes >= 5
    lo, hi = MIN_PASS_S * STT_SAMPLE_RATE, MAX_PASS_S * STT_SAMPLE_RATE
    assert all(lo <= n <= hi for n in session.pass_sizes)


@pytest.mark.model
async def test_stream_silence_only(engine: ParakeetEngine) -> None:
    session = await engine.stream()
    async with session:
        for _ in range(10):
            await session.add_audio(np.zeros(CHUNK, dtype=np.float32))
        transcript = await session.finalize()
    assert transcript.text == ""
    assert transcript.duration_s == pytest.approx(1.0)


@pytest.mark.model
async def test_close_without_finalize_restores_batch_path(
    engine: ParakeetEngine, speech: NDArray[np.float32]
) -> None:
    session = await engine.stream("en")
    async with session:
        await session.add_audio(speech[:STT_SAMPLE_RATE])
        # exiting without finalize discards the audio
    with pytest.raises(EngineError):
        await session.finalize()
    transcript = await engine.transcribe(AudioInput(speech), "en")
    assert_transcribed(transcript.text, EXPECTED, EXPECTED_WORDS)


@pytest.mark.model
async def test_cancelled_finalize_leaves_engine_usable(
    engine: ParakeetEngine, speech: NDArray[np.float32]
) -> None:
    session = await engine.stream("en")

    async def run() -> None:
        async with session:
            await session.add_audio(speech)
            await session.finalize()

    task = asyncio.create_task(run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    transcript = await engine.transcribe(AudioInput(speech), "en")
    assert_transcribed(transcript.text, EXPECTED, EXPECTED_WORDS)


@pytest.mark.model
def test_resident_memory(engine: ParakeetEngine) -> None:
    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6
    print(f"peak resident memory with the model loaded: {rss_mb:.0f} MB")
    assert rss_mb < 2000
