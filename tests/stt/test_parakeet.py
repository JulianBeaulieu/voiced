"""Parakeet engine tests.

The first half runs anywhere: metadata, input validation, and the streaming
adapter and session against a fake model that records every encoder call and
keeps the library's mutable state fields, so snapshots and rollbacks are
observable. The second half is marked ``model`` and needs the real weights:
``VOICED_MODEL_TESTS=1 uv run pytest tests/stt -m model -s``.
"""

from __future__ import annotations

import asyncio
import resource
import statistics
import time
from collections.abc import Callable
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
    MAX_BACKLOG_S,
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
    _to_transcript,
)
from voiced.engines.registry import get_stt_engine

EXPECTED = "turn on the kitchen lights and set the thermostat to twenty degrees"
EXPECTED_WORDS = {"kitchen", "lights", "thermostat", "degrees"}
EXPECTED_TRAILING = "what is the weather like tomorrow morning"
TRAILING_WORDS = {"weather", "tomorrow", "morning"}
CHUNK = STT_SAMPLE_RATE // 10  # 100 ms, what the HTTP layer sends
WYOMING_CHUNK = 1024  # 64 ms, a common Wyoming chunk size
SILENCE_WINDOW_S = 0.2  # the default settings.stt.vad_silence_ms
FINALIZE_BUDGET_S = 0.05  # the interface's target for streaming finalize latency
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


def noise(s: float, dbfs: float, seed: int = 0) -> NDArray[np.float32]:
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(seconds(s)) * 10 ** (dbfs / 20)).astype(np.float32)


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


def test_check_samples_rejects_wrong_shapes_types_and_values() -> None:
    assert _check_samples(np.zeros(10, dtype=np.float64)).dtype == np.float32
    with pytest.raises(EngineError):
        _check_samples(np.zeros((10, 2), dtype=np.float32))
    with pytest.raises(EngineError):
        _check_samples(np.zeros(10, dtype=np.int16))
    with pytest.raises(EngineError):
        _check_samples(np.array([0.0, np.nan], dtype=np.float32))
    with pytest.raises(EngineError):
        _check_samples(np.array([0.0, np.inf], dtype=np.float32))


def test_silence_gate_uses_the_loudest_frame() -> None:
    quiet_speech = speech_like(0.5, level=0.005)  # about -46 dBFS
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
    """Stands in for the library's StreamingParakeet.

    ``calls`` records every encoder call, including ones a rollback undoes.
    ``finalized_tokens`` is the state the transcript is built from: the sizes
    of the passes that count. Snapshot and restore act on it like on the real
    library's token list.
    """

    def __init__(self, model: FakeModel) -> None:
        self.model = model
        self.calls: list[int] = []
        self.entered = False
        self.exited = False
        self.in_call = False
        # the library's mutable state, as the adapter's snapshot expects
        self.cache: list[Any] = []
        self.audio_buffer: Any = None
        self.mel_buffer: Any = None
        self.decoder_hidden: Any = None
        self.last_token: Any = None
        self.finalized_tokens: list[int] = []
        self.draft_tokens: list[int] = []

    def __enter__(self) -> FakeStream:
        self.entered = True
        return self

    def __exit__(self, *exc: object) -> None:
        self.exited = True

    def add_audio(self, audio: NDArray[np.float32]) -> None:
        self.in_call = True
        try:
            time.sleep(self.model.delay_s)
            self.calls.append(len(audio))
            if self.model.fail_on == len(self.calls):
                raise RuntimeError("encoder exploded")
            self.finalized_tokens.append(len(audio))
        finally:
            self.in_call = False

    @property
    def result(self) -> Any:
        sentences, t = [], 0.0
        for n in self.finalized_tokens:
            sentences.append(SimpleNamespace(text=f"p{n}", start=t, end=t + n / STT_SAMPLE_RATE))
            t += n / STT_SAMPLE_RATE
        return SimpleNamespace(text=" ".join(s.text for s in sentences), sentences=sentences)


class FakeModel:
    def __init__(
        self, *, fail_on: int | None = None, delay_s: float = 0.0, time_ratio: float = 0.08
    ) -> None:
        self.fail_on = fail_on
        self.delay_s = delay_s
        self.time_ratio = time_ratio
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


def adapter(model: FakeModel | None = None, *, flush: bool = True) -> _ChunkedStream:
    a = _ChunkedStream(model or FakeModel(), _identity)
    if not flush:
        a._tail_is_pause = lambda chunk: False  # type: ignore[method-assign]
    return a


def feed_all(a: _ChunkedStream, chunks: list[NDArray[np.float32]]) -> Any:
    for chunk in chunks:
        a.feed(chunk)
    return a.flush()


def chunked(audio: NDArray[np.float32], size: int = CHUNK) -> list[NDArray[np.float32]]:
    return [audio[i : i + size] for i in range(0, len(audio), size)]


def test_adapter_runs_regular_passes_at_the_minimum() -> None:
    model = FakeModel()
    a = adapter(model)
    ran = [a.feed(speech_like(0.1)) for _ in range(4)]
    assert ran == [False, False, False, True]
    assert model.streams[0].calls == [seconds(0.4)]
    assert a.text == f"p{seconds(0.4)}"


def test_adapter_flushes_speculatively_on_a_pause() -> None:
    model = FakeModel()
    a = adapter(model)
    for _ in range(4):
        a.feed(speech_like(0.1))
    a.feed(speech_like(0.1))  # a short tail of speech
    assert a.feed(silence(0.1))  # the speaker went quiet: flush now
    calls = model.streams[0].calls
    assert calls == [seconds(0.4), MIN_PASS]  # the tail, padded to the minimum
    assert not a.feed(silence(0.1))  # the pause is held, not encoded
    result = a.flush()
    assert result is not None
    assert model.streams[0].calls == calls, "finalize after a pause needs no encoder call"
    assert a.rollbacks == 0


def test_adapter_flushes_on_a_silent_chunk_tail() -> None:
    model = FakeModel()
    a = adapter(model)
    assert a.feed(np.concatenate([speech_like(0.1), silence(0.032)]))
    assert model.streams[0].calls == [MIN_PASS]
    a.flush()
    b = adapter(FakeModel())
    assert not b.feed(np.concatenate([silence(0.1), speech_like(0.1)]))  # speech to the end


@pytest.mark.parametrize("pause_chunks", [1, 2, 3, 5, 8, 30])
@pytest.mark.parametrize("chunk", [CHUNK, WYOMING_CHUNK])
def test_resumed_speech_matches_no_flush_exactly(pause_chunks: int, chunk: int) -> None:
    """A speculative flush followed by more speech leaves no trace in the transcript."""
    audio = np.concatenate(
        [speech_like(0.5), silence(pause_chunks * 0.1), speech_like(0.7, level=0.2)]
    )
    on_model, off_model = FakeModel(), FakeModel()
    on = adapter(on_model)
    off = adapter(off_model, flush=False)
    on_result = feed_all(on, chunked(audio, chunk))
    off_result = feed_all(off, chunked(audio, chunk))
    assert on.rollbacks >= 1, "the pause should have triggered a speculative flush"
    assert on_result.text == off_result.text
    assert on_model.streams[0].finalized_tokens == off_model.streams[0].finalized_tokens
    # the rolled-back speculative passes still cost encoder calls
    assert len(on_model.streams[0].calls) > len(off_model.streams[0].calls)


def test_adapter_never_makes_a_call_shorter_than_the_minimum() -> None:
    # shorter calls corrupt the library's encoder cache (per-call mel normalization)
    audio = np.concatenate(
        [speech_like(0.05), silence(0.05), speech_like(0.37), silence(0.3), speech_like(0.02)]
    )
    for size in (CHUNK, WYOMING_CHUNK, 160, 4000):
        model = FakeModel()
        feed_all(adapter(model), chunked(audio, size))
        assert model.streams[0].calls
        assert min(model.streams[0].calls) >= MIN_PASS


@pytest.mark.parametrize(
    ("backlog_s", "expected_passes"), [(0.65, 2), (1.1, 2), (1.3, 3), (5.0, 8)]
)
def test_adapter_splits_a_backlog_evenly(backlog_s: float, expected_passes: int) -> None:
    model = FakeModel()
    assert adapter(model).feed(speech_like(backlog_s))
    calls = model.streams[0].calls
    assert len(calls) == expected_passes
    assert all(MIN_PASS <= n <= MAX_PASS for n in calls)
    assert sum(calls) == seconds(backlog_s)


def test_adapter_never_encodes_pure_silence() -> None:
    model = FakeModel()
    a = adapter(model)
    for _ in range(20):
        assert not a.feed(silence(0.1))
    assert a.buffered_s <= MAX_PASS_S  # an open microphone does not grow the buffer
    assert a.flush() is None
    assert model.streams == []
    a.close()  # nothing to exit


def test_adapter_hears_quiet_speech_after_long_silence() -> None:
    model = FakeModel()
    a = adapter(model)
    assert a.feed(np.concatenate([silence(10.0), speech_like(0.5, level=0.005)]))
    assert model.streams[0].calls


@pytest.mark.parametrize("noise_dbfs", [-70.0, -60.0, -55.0, -50.0])
def test_noise_floor_after_speech_neither_rearms_nor_blocks_the_flush(noise_dbfs: float) -> None:
    model = FakeModel()
    a = adapter(model)
    loud = speech_like(0.1, level=0.5)  # about -6 dBFS
    for _ in range(5):
        a.feed(loud)
    for i in range(6):  # the endpointer's window of room noise
        a.feed(noise(0.1, noise_dbfs, seed=i))
    calls = list(model.streams[0].calls)
    assert len(calls) == 2, "one regular pass, one speculative flush on the first noise chunk"
    assert a.flush() is not None
    assert model.streams[0].calls == calls, "the noise must not need another encoder call"


def test_adapter_replays_a_pause_that_outgrows_the_hold() -> None:
    model = FakeModel()
    a = adapter(model)
    for _ in range(5):
        a.feed(speech_like(0.1))
    a.feed(silence(0.1))  # speculative flush
    assert a._speculation is not None
    for _ in range(30):  # 3 s, past the hold limit
        a.feed(silence(0.1))
    assert a._speculation is None
    assert a.rollbacks == 1
    assert a.buffered_s <= MAX_PASS_S


def test_adapter_trims_segments_past_the_timestamp_horizon() -> None:
    model = FakeModel(time_ratio=0.002)  # horizon 256 * 0.002 = 0.512 s
    a = adapter(model)
    for _ in range(12):
        a.feed(speech_like(0.1))
    result = a.flush()
    assert result is not None
    assert result.sentences, "segments inside the horizon are kept"
    assert all(s.end <= 0.512 for s in result.sentences)
    assert len(result.sentences) < len(model.streams[0].finalized_tokens)


def test_adapter_close_restores_the_stream_context() -> None:
    model = FakeModel()
    a = adapter(model)
    a.feed(speech_like(0.5))
    assert model.streams[0].entered and not model.streams[0].exited
    a.close()
    assert model.streams[0].exited
    a.close()  # idempotent


async def assert_engine_accepts_a_new_stream(engine: ParakeetEngine) -> None:
    session = await engine.stream("en")
    await session.close()


async def test_engine_allows_one_session_at_a_time() -> None:
    engine = fake_engine()
    session = await engine.stream("en")
    with pytest.raises(EngineError):
        await engine.stream("en")
    with pytest.raises(EngineError):
        await engine.transcribe(AudioInput(speech_like(0.5)))
    await session.close()
    await assert_engine_accepts_a_new_stream(engine)


async def test_session_streams_through_the_adapter() -> None:
    model = FakeModel()
    engine = fake_engine(model)
    async with await engine.stream("en") as session:
        for _ in range(4):
            await session.add_audio(speech_like(0.1))
        await asyncio.sleep(0.05)
        assert session.partial() == f"p{seconds(0.4)}"
        assert session.backlog_s == 0.0
        await session.add_audio(speech_like(0.1))
        await session.add_audio(silence(0.1))  # the speaker went quiet: tail flushed now
        await asyncio.sleep(0.05)
        transcript = await session.finalize()
    assert transcript.text == f"p{seconds(0.4)} p{MIN_PASS}"
    assert transcript.language == "en"
    assert transcript.duration_s == pytest.approx(0.6)
    assert [s.text for s in transcript.segments] == [f"p{seconds(0.4)}", f"p{MIN_PASS}"]
    assert model.streams[0].exited
    await assert_engine_accepts_a_new_stream(engine)


async def test_session_rejects_audio_past_the_backlog_bound() -> None:
    engine = fake_engine(FakeModel(delay_s=0.2))
    async with await engine.stream("en") as session:
        with pytest.raises(EngineError, match="backlog"):
            await session.add_audio(speech_like(MAX_BACKLOG_S + 1))
        await session.add_audio(speech_like(1.0))  # the consumer is now busy for a while
        with pytest.raises(EngineError, match="backlog"):
            for _ in range(int(MAX_BACKLOG_S) + 1):
                await session.add_audio(speech_like(1.0))


async def test_session_reports_an_encoder_failure_once() -> None:
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
    consumer = session._consumer
    assert consumer is not None and consumer.done() and consumer.exception() is None
    await assert_engine_accepts_a_new_stream(engine)


async def test_session_close_after_a_failure_without_finalize() -> None:
    model = FakeModel(fail_on=1)
    engine = fake_engine(model)
    session = await engine.stream("en")
    await session.add_audio(speech_like(0.4))
    await asyncio.sleep(0.05)
    await session.close()  # no "Task exception was never retrieved"
    consumer = session._consumer
    assert consumer is not None and consumer.exception() is None
    assert model.streams[0].exited
    await assert_engine_accepts_a_new_stream(engine)


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
    await assert_engine_accepts_a_new_stream(engine)


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
    await assert_engine_accepts_a_new_stream(engine)


# --- real model -------------------------------------------------------------


@pytest.fixture(scope="module")
def engine() -> ParakeetEngine:
    engine = ParakeetEngine()
    t0 = time.perf_counter()
    asyncio.run(engine.load())
    print(f"\nparakeet load and warm-up: {time.perf_counter() - t0:.1f}s")
    return engine


def attention_class(engine: ParakeetEngine) -> str:
    return type(engine._model.encoder.layers[0].self_attn).__name__


def at_peak(audio: NDArray[np.float32], dbfs: float) -> NDArray[np.float32]:
    """Scale so the loudest 32 ms frame sits at ``dbfs``, a realistic microphone level."""
    frames = audio[: len(audio) // 512 * 512].reshape(-1, 512)
    peak = float(np.sqrt(np.mean(np.square(frames), axis=1)).max())
    return (audio / peak * 10 ** (dbfs / 20)).astype(np.float32)


async def paced_stream(
    session: ParakeetStreamSession,
    audio: NDArray[np.float32],
    trailing_silence_s: float = 0.0,
    chunk: int = CHUNK,
) -> tuple[list[float], list[str]]:
    """Feed ``chunk``-sized pieces at the rate a microphone delivers them."""
    add_times: list[float] = []
    partials: list[str] = []
    clip = np.concatenate([audio, silence(trailing_silence_s)])
    t0 = time.perf_counter()
    for i in range(0, len(clip), chunk):
        t = time.perf_counter()
        await session.add_audio(clip[i : i + chunk])
        add_times.append(time.perf_counter() - t)
        partials.append(session.partial())
        await asyncio.sleep(max(0.0, t0 + (i + chunk) / STT_SAMPLE_RATE - time.perf_counter()))
    return add_times, partials


async def timed_finalize(
    engine: ParakeetEngine, clip: NDArray[np.float32], chunk: int = CHUNK
) -> tuple[float, str]:
    async with await engine.stream("en") as session:
        await paced_stream(session, clip, chunk=chunk)
        t = time.perf_counter()
        transcript = await session.finalize()
        return time.perf_counter() - t, transcript.text


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
async def test_streaming_partials_and_transcript(
    engine: ParakeetEngine, speech: NDArray[np.float32]
) -> None:
    session = await engine.stream("en")
    assert isinstance(session, ParakeetStreamSession)
    async with session:
        add_times, partials = await paced_stream(session, speech, SILENCE_WINDOW_S)
        transcript = await session.finalize()
    first_partial = next(i for i, p in enumerate(partials) if p) + 1
    print(
        f"stream: max add_audio {max(add_times) * 1000:.2f} ms, {session.passes} passes, "
        f"first partial after {first_partial} chunks: {transcript.text!r}"
    )
    assert any(partials), "no partial hypothesis before finalize"
    assert max(add_times) < 0.02, "add_audio must not block on the encoder"
    assert_transcribed(transcript.text, EXPECTED, EXPECTED_WORDS)
    assert transcript.language == "en"
    assert transcript.duration_s == pytest.approx(len(speech) / STT_SAMPLE_RATE + SILENCE_WINDOW_S)
    assert transcript.segments and transcript.segments[0].start < 0.5


@pytest.mark.model
@pytest.mark.parametrize("chunk", [CHUNK, WYOMING_CHUNK], ids=["100ms", "64ms"])
@pytest.mark.parametrize("noise_dbfs", [None, -55.0], ids=["silence", "noise-55dBFS"])
async def test_finalize_latency_worst_case_over_alignments(
    engine: ParakeetEngine, speech: NDArray[np.float32], chunk: int, noise_dbfs: float | None
) -> None:
    """End of speech at every 20 ms offset relative to the chunk and pass grid,
    then the endpointer's silence window (digital, or room noise at -55 dBFS
    with speech at a -6 dBFS peak), then finalize."""
    voice = speech if noise_dbfs is None else at_peak(speech, -6.0)
    rng = np.random.default_rng(1)
    latencies: list[float] = []
    texts: list[str] = []
    for lead_ms in range(0, 640, 20):
        clip = np.concatenate([silence(lead_ms / 1000), voice, silence(SILENCE_WINDOW_S)])
        if noise_dbfs is not None:
            room = rng.standard_normal(len(clip)) * 10 ** (noise_dbfs / 20)
            clip = (clip + room).astype(np.float32)
        finalize_s, text = await timed_finalize(engine, clip, chunk)
        latencies.append(finalize_s)
        texts.append(text)
    ms = sorted(x * 1000 for x in latencies)
    print(
        f"finalize over {len(ms)} alignments ({chunk} samples, noise {noise_dbfs}): "
        f"worst {ms[-1]:.1f} ms, p95 {ms[int(len(ms) * 0.95)]:.1f}, "
        f"median {statistics.median(ms):.1f}, over budget {sum(m > 50 for m in ms)}"
    )
    for text in texts:
        assert_transcribed(text, EXPECTED, EXPECTED_WORDS)
    assert max(latencies) < FINALIZE_BUDGET_S


@pytest.mark.model
@pytest.mark.parametrize("gap_ms", [110, 250])
async def test_pauses_inside_an_utterance_do_not_change_the_transcript(
    engine: ParakeetEngine,
    speech: NDArray[np.float32],
    monkeypatch: pytest.MonkeyPatch,
    gap_ms: int,
) -> None:
    """With speech after each pause the speculative flushes are all rolled back,
    so the transcript is exactly the one with the flush disabled."""
    frames = speech[: len(speech) // 512 * 512].reshape(-1, 512)
    env = np.sqrt(np.mean(np.square(frames), axis=1))
    cuts = [i * 512 for i in range(1, len(env)) if env[i] < env.max() * 0.05 <= env[i - 1]]
    gap = silence(gap_ms / 1000)
    pieces = [p for part in np.split(speech, cuts) for p in (part, gap)]
    clip = np.concatenate([*pieces, silence(SILENCE_WINDOW_S)]).astype(np.float32)

    _, with_flush = await timed_finalize(engine, clip)
    no_flush: Callable[[Any, NDArray[np.float32]], bool] = lambda self, chunk: False  # noqa: E731
    monkeypatch.setattr(_ChunkedStream, "_tail_is_pause", no_flush)
    _, without_flush = await timed_finalize(engine, clip)
    print(f"{gap_ms} ms pauses, {len(cuts)} of them: {with_flush!r}")
    assert with_flush == without_flush
    assert_transcribed(with_flush, EXPECTED, EXPECTED_WORDS)


@pytest.mark.model
async def test_streaming_trailing_silence_fixture(
    engine: ParakeetEngine, speech_with_trailing_silence: NDArray[np.float32]
) -> None:
    finalize_s, text = await timed_finalize(engine, speech_with_trailing_silence)
    print(f"stream (fixture with 1 s of silence): finalize {finalize_s * 1000:.1f} ms")
    assert_transcribed(text, EXPECTED_TRAILING, TRAILING_WORDS)
    assert finalize_s < FINALIZE_BUDGET_S


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
    assert not session._adapter._lock.locked(), "an encoder pass is still running"
    transcript = await engine.transcribe(AudioInput(speech), "en")
    assert_transcribed(transcript.text, EXPECTED, EXPECTED_WORDS)


@pytest.mark.model
def test_resident_memory(engine: ParakeetEngine) -> None:
    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6
    print(f"peak resident memory with the model loaded: {rss_mb:.0f} MB")
    assert rss_mb < 2000
