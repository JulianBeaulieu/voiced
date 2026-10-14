"""Parakeet engine tests.

The first half runs anywhere: metadata, input validation, and the streaming
adapter and session against a fake model that records encoder passes. The
second half is marked ``model`` and needs the real weights:
``VOICED_MODEL_TESTS=1 uv run pytest tests/stt -m model -s``.
"""

from __future__ import annotations

import asyncio
import resource
import time
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from numpy.typing import NDArray

from tests.stt.conftest import similarity, words
from voiced.engines import parakeet
from voiced.engines.base import (
    STT_SAMPLE_RATE,
    AudioInput,
    EngineError,
    EngineNotLoaded,
    STTEngine,
    UnsupportedLanguage,
)
from voiced.engines.parakeet import (
    MAX_PASS_S,
    MIN_PASS_S,
    PARAKEET_V3_LANGUAGES,
    SILENCE_FLOOR,
    ParakeetEngine,
    ParakeetStreamSession,
    _check_samples,
    _ChunkedStream,
    _identity,
    _is_silent,
    _peak_frame_rms,
    _to_transcript,
)
from voiced.engines.registry import get_stt_engine

EXPECTED = "turn on the kitchen lights and set the thermostat to twenty degrees"
EXPECTED_WORDS = {"kitchen", "lights", "thermostat", "degrees"}
EXPECTED_TRAILING = "what is the weather like tomorrow morning"
TRAILING_WORDS = {"weather", "tomorrow", "morning"}
CHUNK = STT_SAMPLE_RATE // 10  # 100 ms, what the API layers send
SILENCE_WINDOW_S = 0.2  # the default settings.stt.vad_silence_ms
MIN_PASS = int(MIN_PASS_S * STT_SAMPLE_RATE)
MAX_PASS = int(MAX_PASS_S * STT_SAMPLE_RATE)


def assert_transcribed(text: str, expected: str, key_words: set[str]) -> None:
    missing = key_words - set(words(text))
    assert not missing, f"missing {missing} in {text!r}"
    # "20" versus "twenty" and similar are fine; dropped or invented words are not.
    assert similarity(text, expected) >= 0.8, f"{text!r} does not match {expected!r}"


def seconds(n: float) -> int:
    return int(n * STT_SAMPLE_RATE)


def speech_like(s: float, level: float = 0.1) -> NDArray[np.float32]:
    return np.full(seconds(s), level, dtype=np.float32)


def silence(s: float) -> NDArray[np.float32]:
    return np.zeros(seconds(s), dtype=np.float32)


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


def test_pass_bounds_allow_even_splitting() -> None:
    assert MAX_PASS_S >= 2 * MIN_PASS_S
    assert parakeet.LIBRARY_MIN_SAMPLES < MIN_PASS


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
    audio = AudioInput(samples=silence(1.0))
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


def test_silence_gate_uses_the_loudest_frame() -> None:
    quiet_speech = speech_like(0.5, level=0.005)  # about -46 dBFS
    assert _peak_frame_rms(quiet_speech) == pytest.approx(0.005)
    assert not _is_silent(quiet_speech)
    assert _is_silent(silence(1.0))
    assert _is_silent(speech_like(1.0, level=SILENCE_FLOOR / 2))
    assert _is_silent(np.zeros(0, dtype=np.float32))
    # two minutes of silence do not average half a second of quiet speech away
    assert not _is_silent(np.concatenate([silence(120.0), quiet_speech]))
    # a partial trailing frame counts too
    assert not _is_silent(np.concatenate([silence(1.0), speech_like(0.01)]))


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


class FakeStream:
    """Stands in for the library's StreamingParakeet: records every pass."""

    def __init__(self, model: FakeModel) -> None:
        self.model = model
        self.sizes: list[int] = []
        self.entered = False
        self.exited = False
        self.in_call = False

    def __enter__(self) -> FakeStream:
        self.entered = True
        return self

    def __exit__(self, *exc: object) -> None:
        self.exited = True

    def add_audio(self, audio: NDArray[np.float32]) -> None:
        self.in_call = True
        try:
            time.sleep(self.model.delay_s)
            self.sizes.append(len(audio))
            if self.model.fail_on == len(self.sizes):
                raise RuntimeError("encoder exploded")
        finally:
            self.in_call = False

    @property
    def result(self) -> Any:
        text = f"pass {len(self.sizes)}"
        return SimpleNamespace(
            text=text, sentences=[SimpleNamespace(text=text, start=0.0, end=0.5)]
        )


class FakeModel:
    time_ratio = 0.08

    def __init__(self, *, fail_on: int | None = None, delay_s: float = 0.0) -> None:
        self.fail_on = fail_on
        self.delay_s = delay_s
        self.streams: list[FakeStream] = []

    def transcribe_stream(self, context_size: tuple[int, int], depth: int) -> FakeStream:
        assert context_size == parakeet.CONTEXT_SIZE and depth == parakeet.CONTEXT_DEPTH
        stream = FakeStream(self)
        self.streams.append(stream)
        return stream


def fake_engine(model: FakeModel | None = None) -> ParakeetEngine:
    engine = ParakeetEngine()
    engine._model = model or FakeModel()
    engine._to_array = _identity
    return engine


def adapter(model: FakeModel | None = None) -> _ChunkedStream:
    return _ChunkedStream(model or FakeModel(), _identity)


def test_adapter_passes_on_minimum_then_flushes_on_silence() -> None:
    model = FakeModel()
    a = adapter(model)
    ran = [a.feed(speech_like(0.1)) for _ in range(4)]
    assert ran == [False, False, False, True]
    assert model.streams[0].sizes == [seconds(0.4)]
    assert a.text == "pass 1"
    a.feed(speech_like(0.1))  # a short tail of speech
    assert a.feed(silence(0.1))  # the speaker went quiet: flush now, zero-padded
    assert model.streams[0].sizes == [seconds(0.4), MIN_PASS]
    assert not a.feed(silence(0.1))  # more silence is buffered, not encoded
    assert not a.feed(silence(0.1))
    assert a.flush() is not None  # nothing left but silence: no further pass
    assert a.passes == 2
    assert a.buffered_s == 0.0


def test_adapter_flushes_on_a_silent_chunk_tail() -> None:
    # The consumer may hand the adapter several network chunks merged together.
    model = FakeModel()
    a = adapter(model)
    assert a.feed(np.concatenate([speech_like(0.1), silence(0.1)]))
    assert model.streams[0].sizes == [MIN_PASS]
    # Speech right up to the chunk end is not a flush, though.
    assert not a.feed(np.concatenate([silence(0.1), speech_like(0.1)]))
    assert not a.feed(np.concatenate([speech_like(0.05), silence(0.05)]))  # tail too short
    assert model.streams[0].sizes == [MIN_PASS]


def test_adapter_speech_after_a_pause_includes_the_pause() -> None:
    model = FakeModel()
    a = adapter(model)
    for _ in range(4):
        a.feed(speech_like(0.1))
    a.feed(silence(0.1))
    a.feed(silence(0.1))
    assert model.streams[0].sizes == [seconds(0.4)]
    a.feed(speech_like(0.1))
    assert model.streams[0].sizes == [seconds(0.4)]  # 0.3 s buffered: pause plus speech
    a.feed(speech_like(0.1))
    assert model.streams[0].sizes == [seconds(0.4), seconds(0.4)]  # the pause is encoded too


@pytest.mark.parametrize(
    ("backlog_s", "expected_passes"), [(0.65, 2), (1.1, 2), (1.3, 3), (5.0, 8)]
)
def test_adapter_splits_a_backlog_evenly(backlog_s: float, expected_passes: int) -> None:
    model = FakeModel()
    assert adapter(model).feed(speech_like(backlog_s))
    sizes = model.streams[0].sizes
    assert len(sizes) == expected_passes
    assert all(MIN_PASS <= n <= MAX_PASS for n in sizes)
    assert sum(sizes) == seconds(backlog_s)


def test_adapter_flush_pads_a_short_speech_tail() -> None:
    model = FakeModel()
    a = adapter(model)
    assert not a.feed(speech_like(0.05))
    assert a.flush() is not None
    assert model.streams[0].sizes == [MIN_PASS]


def test_adapter_never_encodes_pure_silence() -> None:
    model = FakeModel()
    a = adapter(model)
    for _ in range(20):
        assert not a.feed(silence(0.1))
    assert a.buffered_s <= MAX_PASS_S  # an open microphone does not grow the buffer
    assert a.flush() is None
    assert a.passes == 0
    assert model.streams == []
    a.close()  # nothing to exit


def test_adapter_hears_quiet_speech_after_long_silence() -> None:
    model = FakeModel()
    a = adapter(model)
    assert a.feed(np.concatenate([silence(10.0), speech_like(0.5, level=0.005)]))
    assert a.passes > 0


def test_adapter_close_restores_the_stream_context() -> None:
    model = FakeModel()
    a = adapter(model)
    a.feed(speech_like(0.5))
    assert model.streams[0].entered and not model.streams[0].exited
    a.close()
    assert model.streams[0].exited
    a.close()  # idempotent


async def test_engine_allows_one_session_at_a_time() -> None:
    engine = fake_engine()
    session = await engine.stream("en")
    with pytest.raises(EngineError):
        await engine.stream("en")
    with pytest.raises(EngineError):
        await engine.transcribe(AudioInput(speech_like(0.5)))
    await session.close()
    second = await engine.stream("en")
    assert isinstance(second, ParakeetStreamSession)
    await second.close()


async def test_session_streams_through_the_adapter() -> None:
    model = FakeModel()
    engine = fake_engine(model)
    async with await engine.stream("en") as session:
        for _ in range(4):
            await session.add_audio(speech_like(0.1))
        await asyncio.sleep(0.05)
        assert session.partial() == "pass 1"
        assert session.backlog_s == 0.0
        await session.add_audio(speech_like(0.1))
        await session.add_audio(silence(0.1))  # the speaker went quiet: tail flushed now
        await asyncio.sleep(0.05)
        assert session.partial() == "pass 2"
        transcript = await session.finalize()
    assert transcript.text == "pass 2"
    assert transcript.language == "en"
    assert transcript.duration_s == pytest.approx(0.6)
    assert transcript.segments[0].text == "pass 2"
    assert model.streams[0].exited
    assert engine._session is None


async def test_session_reports_an_encoder_failure() -> None:
    model = FakeModel(fail_on=1)
    engine = fake_engine(model)
    session = await engine.stream("en")
    async with session:
        await session.add_audio(speech_like(0.4))
        await asyncio.sleep(0.05)
        with pytest.raises(RuntimeError, match="encoder exploded"):
            await session.add_audio(speech_like(0.1))
        with pytest.raises(RuntimeError, match="encoder exploded"):
            await session.finalize()
    assert model.streams[0].exited
    assert engine._session is None


async def test_session_failure_during_finalize_is_raised() -> None:
    model = FakeModel(fail_on=1, delay_s=0.1)
    engine = fake_engine(model)
    async with await engine.stream("en") as session:
        await session.add_audio(speech_like(0.4))
        with pytest.raises(RuntimeError, match="encoder exploded"):
            await session.finalize()


async def test_session_rejects_use_after_close() -> None:
    engine = fake_engine()
    session = await engine.stream()
    await session.close()
    with pytest.raises(EngineError):
        await session.add_audio(silence(0.1))
    with pytest.raises(EngineError):
        await session.finalize()
    await session.close()  # idempotent


async def test_session_finalize_twice_raises() -> None:
    engine = fake_engine()
    async with await engine.stream() as session:
        await session.add_audio(speech_like(0.5))
        await session.finalize()
        with pytest.raises(EngineError):
            await session.finalize()


async def test_empty_session_finalizes_to_empty_transcript() -> None:
    engine = fake_engine()
    async with await engine.stream() as session:
        transcript = await session.finalize()
    assert transcript.text == ""
    assert transcript.duration_s == 0.0
    assert transcript.segments == ()


async def test_cancelled_finalize_waits_for_the_pass_and_exits_the_stream() -> None:
    model = FakeModel(delay_s=0.3)
    engine = fake_engine(model)
    session = await engine.stream("en")

    async def run() -> None:
        async with session:
            await session.add_audio(speech_like(0.4))
            await session.finalize()

    task = asyncio.create_task(run())
    await asyncio.sleep(0.05)
    assert model.streams[0].in_call
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    stream = model.streams[0]
    assert not stream.in_call, "close() must wait for the running pass"
    assert stream.exited
    assert engine._session is None
    await engine.transcribe(AudioInput(silence(0.1)))  # the engine is usable again


async def test_cancelled_close_still_exits_the_stream() -> None:
    model = FakeModel(delay_s=0.3)
    engine = fake_engine(model)
    session = await engine.stream("en")
    await session.add_audio(speech_like(0.4))
    await asyncio.sleep(0.05)
    task = asyncio.create_task(session.close())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert model.streams[0].exited
    assert not model.streams[0].in_call
    assert engine._session is None


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


def attention_class(engine: ParakeetEngine) -> str:
    return type(engine._model.encoder.layers[0].self_attn).__name__


async def paced_stream(
    session: ParakeetStreamSession, audio: NDArray[np.float32], trailing_silence_s: float
) -> tuple[list[float], list[str]]:
    """Feed 100 ms chunks at the rate a microphone delivers them."""
    add_times: list[float] = []
    partials: list[str] = []
    clip = np.concatenate([audio, silence(trailing_silence_s)])
    t0 = time.perf_counter()
    for i in range(0, len(clip), CHUNK):
        t = time.perf_counter()
        await session.add_audio(clip[i : i + CHUNK])
        add_times.append(time.perf_counter() - t)
        partials.append(session.partial())
        await asyncio.sleep(max(0.0, t0 + (i + CHUNK) / STT_SAMPLE_RATE - time.perf_counter()))
    return add_times, partials


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
async def test_batch_ten_second_clip_under_300_ms(
    engine: ParakeetEngine,
    speech: NDArray[np.float32],
    speech_with_trailing_silence: NDArray[np.float32],
) -> None:
    clip = np.concatenate([speech, speech_with_trailing_silence, speech])
    await engine.transcribe(AudioInput(clip), "en")  # warm
    t0 = time.perf_counter()
    transcript = await engine.transcribe(AudioInput(clip), "en")
    elapsed = time.perf_counter() - t0
    print(f"batch {len(clip) / STT_SAMPLE_RATE:.1f}s clip in {elapsed * 1000:.0f} ms")
    assert_transcribed(
        transcript.text,
        f"{EXPECTED} {EXPECTED_TRAILING} {EXPECTED}",
        EXPECTED_WORDS | TRAILING_WORDS,
    )
    assert elapsed < 0.3


@pytest.mark.model
async def test_batch_without_language_hint(
    engine: ParakeetEngine, speech_with_trailing_silence: NDArray[np.float32]
) -> None:
    transcript = await engine.transcribe(AudioInput(speech_with_trailing_silence))
    assert_transcribed(transcript.text, EXPECTED_TRAILING, TRAILING_WORDS)
    assert transcript.language is None


@pytest.mark.model
async def test_silent_and_empty_input(engine: ParakeetEngine) -> None:
    rng = np.random.default_rng(0)
    for samples in [
        np.zeros(0, dtype=np.float32),
        silence(0.01),
        silence(1.0),
        (rng.standard_normal(STT_SAMPLE_RATE) * 1e-4).astype(np.float32),
    ]:
        transcript = await engine.transcribe(AudioInput(samples))
        assert transcript.text == ""
        assert transcript.segments == ()
        assert transcript.duration_s == pytest.approx(len(samples) / STT_SAMPLE_RATE)


@pytest.mark.model
async def test_quiet_speech_after_long_silence_is_transcribed(
    engine: ParakeetEngine, speech: NDArray[np.float32]
) -> None:
    quiet = speech * (0.02 / np.abs(speech).max())  # peaks at -34 dBFS, RMS well below
    clip = np.concatenate([silence(10.0), quiet])
    transcript = await engine.transcribe(AudioInput(clip), "en")
    assert_transcribed(transcript.text, EXPECTED, EXPECTED_WORDS)


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
async def test_streaming_finalize_after_the_silence_window(
    engine: ParakeetEngine, speech: NDArray[np.float32]
) -> None:
    """Real-time paced clip, then the endpointer's silence window, then finalize."""
    session = await engine.stream("en")
    assert isinstance(session, ParakeetStreamSession)
    async with session:
        add_times, partials = await paced_stream(session, speech, SILENCE_WINDOW_S)
        backlog = session.backlog_s
        t = time.perf_counter()
        transcript = await session.finalize()
        finalize_s = time.perf_counter() - t
    first_partial = next(i for i, p in enumerate(partials) if p) + 1
    print(
        f"stream: max add_audio {max(add_times) * 1000:.2f} ms, {session.passes} passes, "
        f"backlog at finalize {backlog * 1000:.0f} ms, finalize {finalize_s * 1000:.1f} ms, "
        f"first partial after {first_partial} chunks: {transcript.text!r}"
    )
    assert any(partials), "no partial hypothesis before finalize"
    assert max(add_times) < 0.02, "add_audio must not block on the encoder"
    assert_transcribed(transcript.text, EXPECTED, EXPECTED_WORDS)
    assert transcript.language == "en"
    assert transcript.duration_s == pytest.approx(len(speech) / STT_SAMPLE_RATE + SILENCE_WINDOW_S)
    assert transcript.segments and transcript.segments[0].start < 0.5
    assert finalize_s < 0.05


@pytest.mark.model
async def test_streaming_trailing_silence_fixture(
    engine: ParakeetEngine, speech_with_trailing_silence: NDArray[np.float32]
) -> None:
    async with await engine.stream("en") as session:
        await paced_stream(session, speech_with_trailing_silence, 0.0)
        t = time.perf_counter()
        transcript = await session.finalize()
        finalize_s = time.perf_counter() - t
    print(f"stream (fixture with 1 s of silence): finalize {finalize_s * 1000:.1f} ms")
    assert_transcribed(transcript.text, EXPECTED_TRAILING, TRAILING_WORDS)
    assert finalize_s < 0.05


@pytest.mark.model
async def test_stream_whole_clip_at_once(
    engine: ParakeetEngine, speech: NDArray[np.float32]
) -> None:
    """A client that uploads faster than real time still gets a transcript."""
    async with await engine.stream("en") as session:
        await session.add_audio(speech)
        t = time.perf_counter()
        transcript = await session.finalize()
        burst_ms = (time.perf_counter() - t) * 1000
    print(f"burst finalize of {len(speech) / STT_SAMPLE_RATE:.1f}s: {burst_ms:.0f} ms")
    assert_transcribed(transcript.text, EXPECTED, EXPECTED_WORDS)
    assert session.passes >= 5


@pytest.mark.model
async def test_stream_silence_only(engine: ParakeetEngine) -> None:
    async with await engine.stream() as session:
        for _ in range(10):
            await session.add_audio(silence(0.1))
        transcript = await session.finalize()
    assert transcript.text == ""
    assert transcript.duration_s == pytest.approx(1.0)
    assert session.passes == 0


@pytest.mark.model
async def test_close_without_finalize_restores_batch_path(
    engine: ParakeetEngine, speech: NDArray[np.float32]
) -> None:
    async with await engine.stream("en") as session:
        await session.add_audio(speech[:STT_SAMPLE_RATE])
        await asyncio.sleep(0.3)
        assert attention_class(engine) == "RelPositionMultiHeadLocalAttention"
    assert attention_class(engine) == "RelPositionMultiHeadAttention"
    with pytest.raises(EngineError):
        await session.finalize()
    transcript = await engine.transcribe(AudioInput(speech), "en")
    assert_transcribed(transcript.text, EXPECTED, EXPECTED_WORDS)


@pytest.mark.model
async def test_cancelled_finalize_restores_the_model(
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
    assert attention_class(engine) == "RelPositionMultiHeadAttention"
    assert engine._session is None
    assert not session._adapter._lock.locked(), "an encoder pass is still running"
    transcript = await engine.transcribe(AudioInput(speech), "en")
    assert_transcribed(transcript.text, EXPECTED, EXPECTED_WORDS)


@pytest.mark.model
def test_resident_memory(engine: ParakeetEngine) -> None:
    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6
    print(f"peak resident memory with the model loaded: {rss_mb:.0f} MB")
    assert rss_mb < 2000
