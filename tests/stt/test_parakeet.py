"""Parakeet engine tests.

The first half runs anywhere: metadata, input validation, the planner, the
adapter and the session against a fake library that records every encoder
call, checkpoint and restore. Speech is detected by ``EnergyDetector`` so
synthetic signals work. The second half is marked ``model`` and needs the
real weights: ``VOICED_MODEL_TESTS=1 uv run pytest tests/stt -m model -s``.
"""

from __future__ import annotations

import asyncio
import itertools
import os
import statistics
import threading
import time
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from numpy.typing import NDArray

from tests.stt.conftest import word_accuracy, words
from voiced.audio import SILENCE_FLOOR, check_samples, is_silent
from voiced.config import STT_STREAMS
from voiced.engines import parakeet
from voiced.engines.base import (
    STT_SAMPLE_RATE,
    AudioInput,
    EngineError,
    EngineNotLoaded,
    STTEngine,
    STTStreamSession,
    UnsupportedLanguage,
)
from voiced.engines.parakeet import (
    CONTEXT_SIZE,
    HOLD_MAX_S,
    MAX_BACKLOG_S,
    PARAKEET_V3_LANGUAGES,
    PASS_S,
    PRE_ROLL_S,
    URGENT_BACKLOG_S,
    ParakeetEngine,
    ParakeetStreamSession,
    _Library,
    _Planner,
    _Rank,
    _StepRunner,
    _to_transcript,
)
from voiced.engines.registry import get_stt_engine
from voiced.testing import EnergyDetector
from voiced.threads import run_blocking
from voiced.vad import Endpointer

EXPECTED = "turn on the kitchen lights and set the thermostat to twenty degrees"
EXPECTED_WORDS = {"kitchen", "lights", "thermostat", "degrees"}
EXPECTED_TRAILING = "what is the weather like tomorrow morning"
TRAILING_WORDS = {"weather", "tomorrow", "morning"}
CHUNK = STT_SAMPLE_RATE // 10  # 100 ms, what the HTTP layer sends
WYOMING_CHUNK = 1024  # 64 ms, a common Wyoming chunk size
SILENCE_WINDOW_S = 0.2  # the default settings.stt.vad_silence_ms
FINALIZE_BUDGET_S = 0.05  # the interface's target for streaming finalize latency
FINALIZE_WORST_S = 0.15  # the acceptance bound in ADR 0008
PASS = round(PASS_S * STT_SAMPLE_RATE)
PRE_ROLL = round(PRE_ROLL_S * STT_SAMPLE_RATE)
FRAME = 512


def assert_transcribed(text: str, expected: str, key_words: set[str]) -> None:
    missing = key_words - set(words(text))
    assert not missing, f"missing {missing} in {text!r}"
    # "20" versus "twenty" and similar are fine; dropped or invented words are not.
    assert word_accuracy(text, expected) >= 0.8, f"{text!r} does not match {expected!r}"


def seconds(n: float) -> int:
    return round(n * STT_SAMPLE_RATE)


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


def test_pass_fits_the_right_context() -> None:
    # _LibraryStream reads the mel frames per window from the first call, which
    # holds only while one window is shorter than the right context (80 ms frames).
    assert PASS_S < CONTEXT_SIZE[1] * 0.08
    assert parakeet.LIBRARY_MIN_SAMPLES < PASS


@pytest.mark.model
def test_urgent_encoder_call_holds_the_gpu_lock(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kokoro in the TTS service waits while an urgent encoder call runs (ADR 0019)."""
    pytest.importorskip("parakeet_mlx")
    from voiced import gpu

    monkeypatch.setattr(gpu, "lock_path", lambda: tmp_path / gpu.LOCK_NAME)
    monkeypatch.setattr(gpu, "_fds", {})
    seen: list[bool] = []

    class Stream:
        mel_buffer: Any = None
        finalized_tokens: list[Any] = []
        draft_tokens: list[Any] = []
        decoding_config = SimpleNamespace(sentence=None)

        def __enter__(self) -> Stream:
            return self

        def __exit__(self, *exc: object) -> None:
            pass

        def add_audio(self, audio: Any) -> None:
            seen.append(gpu.stt_running())
            self.mel_buffer = np.zeros((1, len(audio) // 160 + 1))

    model = SimpleNamespace(
        preprocessor_config=SimpleNamespace(hop_length=160),
        encoder_config=SimpleNamespace(subsampling_factor=8),
        encoder=_FakeEncoder(),
        transcribe_stream=lambda context_size, keep_original_attention: Stream(),
    )
    lib = parakeet._LibraryStream(model, parakeet._Attention(model))
    try:
        lib.encode(np.zeros(PASS, dtype=np.float32), 0, urgent=True)
        lib.encode(np.zeros(PASS, dtype=np.float32), PASS, urgent=False)
        assert seen == [True, False]
        assert not gpu.stt_running()
    finally:
        lib.close()
        for fd in gpu._fds.values():
            os.close(fd)


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
    assert check_samples(np.zeros(10, dtype=np.float64)).dtype == np.float32
    with pytest.raises(EngineError):
        check_samples(np.zeros((10, 2), dtype=np.float32))
    with pytest.raises(EngineError):
        check_samples(np.zeros(10, dtype=np.int16))
    with pytest.raises(EngineError):
        check_samples(np.array([0.0, np.nan], dtype=np.float32))
    with pytest.raises(EngineError):
        check_samples(np.array([0.0, np.inf], dtype=np.float32))


def test_silence_gate_uses_the_loudest_frame() -> None:
    quiet_speech = speech_like(0.5, level=0.005)  # about -46 dBFS
    assert not is_silent(quiet_speech)
    assert is_silent(silence(1.0))
    assert is_silent(speech_like(1.0, level=SILENCE_FLOOR / 2))
    assert is_silent(np.zeros(0, dtype=np.float32))
    # two minutes of silence do not average half a second of quiet speech away
    assert not is_silent(np.concatenate([silence(120.0), quiet_speech]))
    # a partial trailing frame counts too
    assert not is_silent(np.concatenate([silence(1.0), speech_like(0.01)]))


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


class FakeLibrary:
    """Stands in for ``_LibraryStream``. Each window that counts becomes the word
    ``p<start>`` (its session position), timed from ``start`` to one pass later,
    so a transcript shows exactly which windows the state holds."""

    def __init__(
        self,
        *,
        fail_on: int | None = None,
        delay_s: float = 0.0,
        journal: list[tuple[int, int, bool]] | None = None,
    ) -> None:
        self.fail_on = fail_on
        self.delay_s = delay_s
        self.journal = journal  # (session, start, urgent) of every call, all sessions
        self.session = 0
        self.encoded: list[int] = []  # every call, undone ones included
        self.windows: list[NDArray[np.float32]] = []
        self.urgent: list[bool] = []  # per call: whether it held the GPU lock
        self.kept: list[int] = []  # the calls the current state is built from
        self.restores = 0
        self.closed = False
        self.in_call = False
        self.started = threading.Event()  # set when a call begins

    @property
    def text(self) -> str:
        return " ".join(f"p{start}" for start in self.kept)

    @property
    def calls(self) -> int:
        return len(self.encoded)

    def encode(self, window: NDArray[np.float32], start: int, urgent: bool) -> None:
        assert len(window) == PASS, "every call is exactly one pass"
        self.in_call = True
        self.started.set()
        try:
            time.sleep(self.delay_s)
            if self.journal is not None:
                self.journal.append((self.session, start, urgent))
            self.encoded.append(start)
            self.windows.append(np.array(window))
            self.urgent.append(urgent)
            if self.fail_on == len(self.encoded):
                raise RuntimeError("encoder exploded")
            self.kept.append(start)
        finally:
            self.in_call = False

    def checkpoint(self) -> object:
        return list(self.kept)

    def restore(self, checkpoint: object) -> None:
        assert isinstance(checkpoint, list)
        self.kept = list(checkpoint)
        self.restores += 1

    def result(self) -> Any:
        if not self.encoded:
            return None
        sentences = [
            SimpleNamespace(
                text=f"p{s}", start=s / STT_SAMPLE_RATE, end=(s + PASS) / STT_SAMPLE_RATE
            )
            for s in self.kept
        ]
        return SimpleNamespace(text=self.text, sentences=sentences)

    def close(self) -> None:
        self.closed = True


def energy_speech() -> Endpointer:
    """The engine's detector, but one that accepts synthetic signals: anything
    above -50 dBFS is speech."""
    return Endpointer(EnergyDetector(threshold_dbfs=-50.0), silence_ms=0)


def feed(a: _StepRunner, samples: NDArray[np.float32]) -> None:
    """What the session does with a chunk, minus the threads."""
    for job in a.plan(samples):
        a.run(job)


def flush(a: _StepRunner) -> Any:
    return a.finish([], np.zeros(0, dtype=np.float32))


def adapter(**library: Any) -> tuple[_StepRunner, FakeLibrary]:
    lib = FakeLibrary(**library)
    return _StepRunner(lib, _Planner(energy_speech()), lambda: _Rank.AHEAD), lib


class FakeEngine(ParakeetEngine):
    def __init__(self, **library: Any) -> None:
        super().__init__()
        self._model = object()
        self._library_args = library
        self.libraries: list[FakeLibrary] = []

    def _new_library(self, model: Any) -> _Library:
        lib = FakeLibrary(**self._library_args)
        lib.session = len(self.libraries)
        self.libraries.append(lib)
        return lib

    def _new_detector(self) -> Endpointer:
        return energy_speech()


def feed_all(a: _StepRunner, chunks: list[NDArray[np.float32]]) -> Any:
    for chunk in chunks:
        feed(a, chunk)
    return flush(a)


def chunked(audio: NDArray[np.float32], size: int = CHUNK) -> list[NDArray[np.float32]]:
    return [audio[i : i + size] for i in range(0, len(audio), size)]


def grid(audio: NDArray[np.float32]) -> list[int]:
    """Where the planner puts calls for ``audio`` that is speech from its first
    sample: every pass that contains speech; after a pass without, the grid
    restarts ``PRE_ROLL_S`` before the 32 ms frame where speech resumes."""
    loud = np.abs(audio) > 0.003
    starts, start = [], 0
    while loud[start:].any():
        if loud[start : start + PASS].any():
            starts.append(start)
            start += PASS
        else:
            onset = (start + int(np.argmax(loud[start:]))) // FRAME * FRAME
            start = onset - PRE_ROLL
    return starts


def test_regular_calls_run_on_the_pass_grid() -> None:
    a, lib = adapter()
    for _ in range(round(PASS_S * 10)):
        feed(a, speech_like(0.1))
    assert lib.encoded == [0]
    assert a.text == "p0"
    assert a.buffered_s == 0.0


def test_a_pause_runs_the_final_call_before_finalize() -> None:
    a, lib = adapter()
    feed(a, speech_like(PASS_S + 0.3))
    assert lib.encoded == [0]
    feed(a, silence(0.1))  # the speaker went quiet: everything buffered, padded, at once
    assert lib.encoded == [0, PASS]
    feed(a, silence(0.1))  # the pause is held, not encoded
    result = flush(a)
    assert result is not None and result.text == f"p0 p{PASS}"
    assert lib.encoded == [0, PASS], "finalize after a pause needs no encoder call"
    assert a.rollbacks == 0


def test_only_the_call_at_a_pause_is_urgent_while_the_encoder_keeps_up() -> None:
    a, lib = adapter()
    feed(a, speech_like(2 * PASS_S + 0.3))
    feed(a, silence(0.1))
    assert lib.encoded == [0, PASS, 2 * PASS]
    assert lib.urgent == [False, False, True]


def test_the_pause_is_the_last_whole_frame() -> None:
    a, lib = adapter()
    feed(a, np.concatenate([speech_like(0.2), silence(0.064)]))
    assert lib.encoded == [0]
    b, lib_b = adapter()
    feed(b, np.concatenate([silence(0.1), speech_like(0.2)]))  # speech to the end
    assert lib_b.encoded == []


@pytest.mark.parametrize("pause_s", [0.1, 0.2, 0.3, 0.5, 0.8, 2.0])
@pytest.mark.parametrize("chunk", [CHUNK, WYOMING_CHUNK])
def test_resumed_speech_leaves_no_trace(pause_s: float, chunk: int) -> None:
    """A speculative call followed by more speech is undone: the state holds
    exactly the grid's calls over the real audio, never the padded window."""
    audio = np.concatenate([speech_like(0.5), silence(pause_s), speech_like(0.9, level=0.2)])
    a, lib = adapter()
    result = feed_all(a, chunked(audio, chunk))
    assert a.rollbacks >= 1, "the pause should have triggered a speculative call"
    assert lib.kept == grid(audio)
    assert result.text == " ".join(f"p{s}" for s in grid(audio))
    last_window = dict(zip(lib.encoded, lib.windows, strict=True))
    for start in lib.kept:
        real = audio[start : start + PASS]
        assert np.array_equal(last_window[start][: len(real)], real)
    assert len(lib.encoded) > len(lib.kept), "the undone calls still cost encoder time"


@pytest.mark.parametrize("pause_s", [1.0, 1.5, 2.0, 2.5, 3.0, 4.0])
@pytest.mark.parametrize("chunk", [WYOMING_CHUNK, CHUNK, seconds(1.0), seconds(10.0)])
def test_a_pass_of_silence_alone_is_never_encoded(pause_s: float, chunk: int) -> None:
    """The model invents words in a call of silence. A pause inside an utterance,
    whether undone after a speculative call or arriving in one burst, never
    becomes a call of its own."""
    audio = np.concatenate(
        [speech_like(0.9), silence(pause_s), speech_like(0.9, level=0.2), silence(0.3)]
    )
    a, lib = adapter()
    feed_all(a, chunked(audio, chunk))
    half = FRAME // 2  # speech is judged per 32 ms frame, not per sample
    for start in lib.encoded:
        assert np.abs(audio[max(0, start - half) : start + PASS + half]).max() > 0.0
    if pause_s < HOLD_MAX_S:
        assert lib.kept == grid(audio)
    else:  # a pause past the hold is trimmed, and the grid restarts before the speech
        resumed = seconds(0.9 + pause_s)
        assert resumed - PASS <= lib.kept[2] <= resumed


@pytest.mark.parametrize("backlog_s", [0.65, 1.7, 2.5, 5.0])
def test_a_backlog_is_encoded_on_the_grid(backlog_s: float) -> None:
    a, lib = adapter()
    feed(a, speech_like(backlog_s))
    assert lib.encoded == list(range(0, seconds(backlog_s) - PASS + 1, PASS))
    assert a.buffered_s == pytest.approx((seconds(backlog_s) % PASS) / STT_SAMPLE_RATE)


@pytest.mark.parametrize("size", [160, 1024, 1600, 4000, 16000])
def test_pass_boundaries_do_not_depend_on_chunking(size: int) -> None:
    audio = np.concatenate([speech_like(1.3), speech_like(0.9, level=0.3)])
    reference, _ = adapter()
    ours, _ = adapter()
    ref_lib = reference._library
    our_lib = ours._library
    feed_all(reference, [audio])
    feed_all(ours, chunked(audio, size))
    assert isinstance(ref_lib, FakeLibrary) and isinstance(our_lib, FakeLibrary)
    assert our_lib.encoded == ref_lib.encoded
    for mine, theirs in zip(our_lib.windows, ref_lib.windows, strict=True):
        assert np.array_equal(mine, theirs)


def test_silence_is_never_encoded() -> None:
    a, lib = adapter()
    for _ in range(50):
        feed(a, silence(0.1))
    assert a.buffered_s < 1.0, "an open microphone does not grow the buffer"
    assert flush(a) is None
    assert lib.encoded == []
    a.close()


def test_quiet_speech_after_long_silence_is_heard() -> None:
    a, lib = adapter()
    feed(a, np.concatenate([silence(10.0), speech_like(1.0, level=0.005)]))
    assert lib.encoded


def test_a_long_pause_is_given_up_on_and_dropped() -> None:
    a, lib = adapter()
    feed(a, speech_like(0.5))
    feed(a, silence(0.1))  # speculative call
    assert lib.encoded == [0]
    for _ in range(round(HOLD_MAX_S * 10) + 5):
        feed(a, silence(0.1))
    assert a.rollbacks == 1
    assert lib.kept == [0], "after giving up, the speech is encoded on the grid"
    # a pass of judged audio, and what the detector has not judged yet
    assert a.buffered_s < PASS_S + FRAME / STT_SAMPLE_RATE
    result = flush(a)
    assert result is not None and lib.kept == [0]


def cut_run(audio: NDArray[np.float32], sizes: Iterator[int]) -> tuple[Any, FakeLibrary]:
    """Feed ``audio`` in pieces of ``sizes`` and flush: the result and the calls."""
    a, lib = adapter()
    i = 0
    while i < len(audio):
        n = next(sizes)
        feed(a, audio[i : i + n])
        i += n
    return flush(a), lib


@pytest.mark.parametrize("seed", range(1, 25))
def test_what_is_encoded_does_not_depend_on_where_the_audio_is_cut(seed: int) -> None:
    """Fed in pieces cut anywhere (what a session that is behind plans at once
    depends on timing), the planner keeps the same calls on the same windows
    as when fed in the client's chunks: no window over a pass, the same grid
    after long pauses, the same final call. Each clip ends in a pause shorter
    than HOLD_MAX_S, so its last call is the one at that pause, whichever pass
    end it falls near. Calls at a pause that are undone may differ: one that
    begins and ends inside a single piece is never made."""
    rng = np.random.default_rng(seed)
    clip = np.concatenate([varied(seed, 4.0 + seed % 9), silence(rng.uniform(0.25, 2.0))])
    audio = clip + noise(len(clip) / STT_SAMPLE_RATE, -70.0, seed)[: len(clip)]

    def kept(sizes: Iterator[int]) -> tuple[list[int], list[bytes], str]:
        result, lib = cut_run(audio, sizes)
        last = {s: w.tobytes() for s, w in zip(lib.encoded, lib.windows, strict=True)}
        return lib.kept, [last[s] for s in lib.kept], result.text

    want = kept(itertools.repeat(CHUNK))
    for size in (FRAME, 7 * FRAME, WYOMING_CHUNK, PASS):
        assert kept(itertools.repeat(size)) == want, size
    for cut in range(10):
        sizes = np.random.default_rng(cut).integers(1, PASS + 1, 10_000)
        assert kept(iter(int(n) for n in sizes)) == want, cut


def test_a_pause_that_ends_inside_one_feed_gets_no_call() -> None:
    """A quiet frame inside one chunk is a pause that began and ended within
    the feed: no call at it, which would run as urgent and be undone at once.
    Fed frame by frame, the same pause gets a call that is undone."""
    frames = FRAME / STT_SAMPLE_RATE
    audio = np.concatenate([speech_like(16 * frames), silence(frames), speech_like(16 * frames)])
    whole = _Planner(energy_speech()).feed(audio)
    assert "checkpoint" not in whole and "restore" not in whole
    by_frame, lib = cut_run(audio, itertools.repeat(FRAME))
    assert lib.restores == 1
    once, lib_once = cut_run(audio, iter([len(audio)]))
    assert lib_once.restores == 0
    assert lib_once.kept == lib.kept and once.text == by_frame.text


@pytest.mark.parametrize("chunk", [CHUNK, FRAME, 7])
def test_a_speech_frame_reaching_just_past_the_buffer_start_is_not_speech(chunk: int) -> None:
    """A frame judged speech that reaches under half a frame past the start of
    the buffer belongs to the pass before (``_speech_in``); counting it as
    speech left ``_skip_to_speech`` without a frame to restart at, and a
    pause outgrowing HOLD_MAX_S then raised IndexError."""
    result, _ = cut_run(varied(35, 14.0), itertools.repeat(chunk))
    assert result is not None


def onset_offset(window: NDArray[np.float32]) -> int:
    """Index of the first sample at speech level in ``window``."""
    return int(np.argmax(np.abs(window) > 0.05))


def test_gate_encodes_nothing_before_speech_and_keeps_the_pre_roll() -> None:
    a, lib = adapter()
    for chunk in chunked(noise(1.0, -55.0)):
        feed(a, chunk)
    assert lib.encoded == [], "room noise alone is never encoded"
    feed_all(a, chunked(speech_like(1.0)))
    # The first call starts PRE_ROLL_S before the onset, which is frame-aligned.
    assert abs(onset_offset(lib.windows[0]) - PRE_ROLL) < FRAME
    assert lib.encoded[0] == seconds(1.0) - onset_offset(lib.windows[0])


def test_gate_finds_the_onset_inside_one_large_chunk() -> None:
    a, lib = adapter()
    feed_all(a, [np.concatenate([noise(2.0, -55.0), speech_like(1.0)])])
    assert abs(onset_offset(lib.windows[0]) - PRE_ROLL) < FRAME


def test_gate_never_encodes_a_session_of_room_noise() -> None:
    a, lib = adapter()
    for chunk in chunked(noise(3.0, -55.0)):
        feed(a, chunk)
    assert flush(a) is None
    assert lib.encoded == []


def test_calls_carry_their_session_position() -> None:
    """The library maps token times through these, so segments after a lead-in
    or a dropped pause are in session time (ADR 0008)."""
    lead = 1.5
    audio = np.concatenate([noise(lead, -55.0), speech_like(1.0), silence(5.0), speech_like(1.0)])
    a, lib = adapter()
    result = feed_all(a, chunked(audio))
    assert lib.kept[0] == pytest.approx(seconds(lead - PRE_ROLL_S), abs=FRAME)
    second = seconds(lead + 1.0 + 5.0)
    assert lib.kept[-1] >= second - PASS, "the dropped pause shifts later calls"
    assert result.sentences[0].start == pytest.approx(lead - PRE_ROLL_S, abs=0.04)


def test_every_session_has_its_own_speech_detector() -> None:
    engine = ParakeetEngine()
    assert engine._new_detector() is not engine._new_detector()


def test_close_is_idempotent() -> None:
    a, lib = adapter()
    feed(a, speech_like(1.0))
    a.close()
    assert lib.closed
    a.close()


async def consumed(session: STTStreamSession) -> None:
    """Wait until the session's consumer has run everything added so far."""
    assert isinstance(session, ParakeetStreamSession)
    if session._consumer is not None:
        await asyncio.wait({session._consumer})


async def assert_engine_accepts_a_new_stream(engine: ParakeetEngine) -> None:
    session = await engine.stream("en")
    await session.close()


async def test_engine_serves_several_sessions_but_no_batch_meanwhile() -> None:
    engine = FakeEngine()
    first = await engine.stream("en")
    second = await engine.stream("en")
    with pytest.raises(EngineError):
        await engine.transcribe(AudioInput(speech_like(0.5)))
    await first.close()
    with pytest.raises(EngineError):
        await engine.transcribe(AudioInput(speech_like(0.5)))
    await second.close()
    assert engine._sessions == set()
    await assert_engine_accepts_a_new_stream(engine)


async def test_session_streams_through_the_adapter() -> None:
    engine = FakeEngine()
    async with await engine.stream("en") as session:
        for _ in range(round(PASS_S * 10)):
            await session.add_audio(speech_like(0.1))
        await asyncio.sleep(0.05)
        assert session.partial() == "p0"
        assert session.backlog_s == 0.0
        await session.add_audio(speech_like(0.1))
        await session.add_audio(silence(0.1))  # the speaker went quiet: tail encoded now
        await asyncio.sleep(0.05)
        transcript = await session.finalize()
    assert transcript.text == f"p0 p{PASS}"
    assert transcript.language == "en"
    assert transcript.duration_s == pytest.approx(PASS_S + 0.2)
    assert [s.text for s in transcript.segments] == ["p0", f"p{PASS}"]
    assert engine.libraries[0].closed
    assert engine.libraries[0].encoded == [0, PASS]
    await assert_engine_accepts_a_new_stream(engine)


async def test_session_calls_are_urgent_once_the_client_waits() -> None:
    """Calls while the client streams and the encoder keeps up leave the GPU to
    synthesis; after ``finalize``, or behind by over URGENT_BACKLOG_S, they go
    first (ADR 0019)."""
    engine = FakeEngine()
    async with await engine.stream("en") as session:
        lib = engine.libraries[0]
        for _ in range(round(PASS_S * 10) + 3):
            await session.add_audio(speech_like(0.1))
            await asyncio.sleep(0.005)
        await consumed(session)
        assert lib.urgent == [False]
        await session.finalize()  # the rest of the speech, without a pause
    assert lib.urgent == [False, True]

    engine = FakeEngine()
    async with await engine.stream("en") as session:
        lib = engine.libraries[0]
        await session.add_audio(speech_like(URGENT_BACKLOG_S + 3 * PASS_S))  # a late start
        await consumed(session)  # caught up
        # Urgent while what is not yet encoded, the call's own pass included,
        # is over URGENT_BACKLOG_S: 5, 4 and 3 passes, not 2 and 1.
        assert lib.urgent == [True] * 3 + [False] * 2
        for _ in range(round(PASS_S * 10)):  # caught up: streaming again
            await session.add_audio(speech_like(0.1))
            await asyncio.sleep(0.005)
        await consumed(session)
        assert lib.urgent == [True] * 3 + [False] * 3
        await session.finalize()


async def test_session_audio_pending_at_finalize_is_encoded_as_urgent() -> None:
    engine = FakeEngine(delay_s=0.1)
    async with await engine.stream("en") as session:
        lib = engine.libraries[0]
        await session.add_audio(speech_like(PASS_S))
        await asyncio.to_thread(lib.started.wait, 2)
        await session.add_audio(speech_like(PASS_S + 0.1))  # pending: under the backlog bound
        await session.finalize()
    assert lib.encoded == [0, PASS, 2 * PASS]
    assert lib.urgent == [False, True, True]


async def test_session_runs_every_call_on_the_engine_thread() -> None:
    engine = FakeEngine()
    threads: set[int] = set()
    async with await engine.stream("en") as session:
        lib = engine.libraries[0]
        encode = lib.encode

        def spy(window: NDArray[np.float32], start: int, urgent: bool) -> None:
            threads.add(threading.get_ident())
            encode(window, start, urgent)

        lib.encode = spy  # type: ignore[method-assign]
        busy = threading.Event()
        hold = asyncio.create_task(run_blocking(busy.wait, 5))  # grows the default pool
        for _ in range(30):
            await session.add_audio(speech_like(0.1))
            await asyncio.sleep(0)
        await session.finalize()
        busy.set()
        await hold
    assert len(threads) == 1


async def test_session_backlog_counts_the_call_in_flight() -> None:
    engine = FakeEngine(delay_s=0.3)
    async with await engine.stream("en") as session:
        await session.add_audio(speech_like(20.0))
        await asyncio.to_thread(engine.libraries[0].started.wait, 2)  # in flight, none pending
        # The running call's remainder under one pass is also in the planner.
        assert 20.0 <= session.backlog_s < 20.0 + PASS_S
        await session.add_audio(speech_like(5.0))
        assert 25.0 <= session.backlog_s < 25.0 + PASS_S


async def test_session_over_the_bound_waits_for_the_call_in_flight() -> None:
    engine = FakeEngine(delay_s=0.3)
    async with await engine.stream("en") as session:
        lib = engine.libraries[0]
        await session.add_audio(speech_like(20.0))
        await asyncio.to_thread(lib.started.wait, 2)
        assert lib.calls == 0
        await session.add_audio(speech_like(11.0))  # 31 s: over the bound
        assert lib.calls >= 1  # one call is one pass
        assert session.backlog_s <= MAX_BACKLOG_S


async def test_session_runs_a_burst_one_encoder_call_at_a_time() -> None:
    """A burst is planned a pass at a time and run one encoder call per
    model-thread call, so another session's urgent call, a waiting
    ``add_audio`` and ``close`` each wait for one call at most."""
    engine = FakeEngine()
    planned: list[int] = []
    encodes: list[int] = []
    async with await engine.stream("en") as session:
        assert isinstance(session, ParakeetStreamSession)
        adapter = session._adapter
        plan, run = adapter.plan, adapter.run

        def plan_spy(samples: NDArray[np.float32]) -> Any:
            planned.append(len(samples))
            return plan(samples)

        def run_spy(job: Any) -> None:
            encodes.append(sum(isinstance(step, parakeet._Encode) for step in job.steps))
            run(job)

        adapter.plan = plan_spy  # type: ignore[method-assign]
        adapter.run = run_spy  # type: ignore[method-assign]
        await session.add_audio(speech_like(MAX_BACKLOG_S))
        await consumed(session)
        await session.finalize()
    assert max(planned) == PASS
    assert sum(planned) == round(MAX_BACKLOG_S * STT_SAMPLE_RATE)
    assert max(encodes) == 1
    assert sum(encodes) == len(engine.libraries[0].encoded)


async def test_session_rejects_one_chunk_over_the_backlog_bound() -> None:
    engine = FakeEngine()
    async with await engine.stream("en") as session:
        with pytest.raises(EngineError, match="chunk over"):
            await session.add_audio(speech_like(MAX_BACKLOG_S + 1))


async def test_session_behind_waits_for_the_encoder_instead_of_failing() -> None:
    """A session that took the slot late gets everything its client sent
    meanwhile at once: more than the bound in total, in chunks that never take
    the backlog past the bound while the encoder is busy, and nothing is lost."""
    engine = FakeEngine(delay_s=0.002)
    total_s, chunk_s = 2.5 * MAX_BACKLOG_S, 1.0
    worst = 0.0
    async with await engine.stream("en") as session:
        for _ in range(round(total_s / chunk_s)):
            await session.add_audio(speech_like(chunk_s))
            worst = max(worst, session.backlog_s)
        transcript = await session.finalize()
    assert worst <= MAX_BACKLOG_S
    assert transcript.duration_s == pytest.approx(total_s)
    assert engine.libraries[0].encoded == grid(speech_like(total_s))


async def test_session_waiting_for_the_encoder_reports_its_failure() -> None:
    engine = FakeEngine(fail_on=2, delay_s=0.01)
    async with await engine.stream("en") as session:
        await session.add_audio(speech_like(MAX_BACKLOG_S - 1))
        with pytest.raises(RuntimeError, match="encoder exploded"):
            await session.add_audio(speech_like(2.0))


async def test_session_reports_an_encoder_failure_once() -> None:
    engine = FakeEngine(fail_on=1)
    session = await engine.stream("en")
    async with session:
        await session.add_audio(speech_like(PASS_S))
        await asyncio.sleep(0.05)
        with pytest.raises(RuntimeError, match="encoder exploded"):
            await session.add_audio(speech_like(0.1))
        with pytest.raises(RuntimeError, match="encoder exploded"):
            await session.finalize()
    assert engine.libraries[0].closed
    consumer = session._consumer
    assert consumer is not None and consumer.done()
    assert consumer.exception() is session._adapter.failure  # held once, by the adapter
    await assert_engine_accepts_a_new_stream(engine)


async def test_session_close_after_a_failure_without_finalize() -> None:
    engine = FakeEngine(fail_on=1)
    session = await engine.stream("en")
    await session.add_audio(speech_like(PASS_S))
    await asyncio.sleep(0.05)
    await session.close()  # no "Task exception was never retrieved"
    consumer = session._consumer
    assert consumer is not None and consumer.exception() is session._adapter.failure
    assert engine.libraries[0].closed
    await assert_engine_accepts_a_new_stream(engine)


async def test_session_failure_during_finalize_is_raised() -> None:
    engine = FakeEngine(fail_on=1, delay_s=0.1)
    async with await engine.stream("en") as session:
        await session.add_audio(speech_like(PASS_S + 0.3))  # a tail left to flush
        with pytest.raises(RuntimeError, match="encoder exploded"):
            await session.finalize()
    assert engine.libraries[0].calls == 1, "nothing is encoded after the failure"


async def test_session_rejects_use_after_close() -> None:
    engine = FakeEngine()
    session = await engine.stream()
    await session.close()
    with pytest.raises(EngineError):
        await session.add_audio(silence(0.1))
    with pytest.raises(EngineError):
        await session.finalize()
    await session.close()  # idempotent


async def test_session_finalize_twice_raises() -> None:
    engine = FakeEngine()
    async with await engine.stream() as session:
        await session.add_audio(speech_like(0.5))
        await session.finalize()
        with pytest.raises(EngineError):
            await session.finalize()


async def test_empty_session_finalizes_to_empty_transcript() -> None:
    engine = FakeEngine()
    async with await engine.stream() as session:
        transcript = await session.finalize()
    assert transcript.text == ""
    assert transcript.duration_s == 0.0
    assert transcript.segments == ()


async def test_cancelled_finalize_waits_for_the_call_and_closes() -> None:
    engine = FakeEngine(delay_s=0.3)
    session = await engine.stream("en")

    async def run() -> None:
        async with session:
            await session.add_audio(speech_like(PASS_S))
            await session.finalize()

    task = asyncio.create_task(run())
    await asyncio.sleep(0)
    lib = engine.libraries[0]
    await asyncio.to_thread(lib.started.wait, 2)
    assert lib.in_call
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not lib.in_call, "close() must wait for the running call"
    assert lib.closed
    await assert_engine_accepts_a_new_stream(engine)


async def test_cancelled_close_still_closes() -> None:
    engine = FakeEngine(delay_s=0.3)
    session = await engine.stream("en")
    await session.add_audio(speech_like(PASS_S))
    await asyncio.to_thread(engine.libraries[0].started.wait, 2)
    task = asyncio.create_task(session.close())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    lib = engine.libraries[0]
    assert lib.closed
    assert not lib.in_call
    await assert_engine_accepts_a_new_stream(engine)


# --- several sessions at once ---------------------------------------------------


def varied(seed: int, s: float) -> NDArray[np.float32]:
    """Speech-like audio whose every window differs, with pauses and a quiet
    lead-in, so a transcript of window positions and contents is specific to
    it."""
    rng = np.random.default_rng(seed)
    parts = [noise(rng.uniform(0.2, 0.9), -60.0, seed)]
    while sum(len(p) for p in parts) < seconds(s):
        parts.append(speech_like(rng.uniform(0.3, 2.5), level=rng.uniform(0.05, 0.5)))
        parts.append(silence(rng.choice([0.1, 0.25, 0.6, 1.5, 3.0])))
    return np.concatenate(parts)[: seconds(s)]


async def stream_paced(
    session: STTStreamSession, audio: NDArray[np.float32], chunk: int, speed: float = 20.0
) -> Any:
    """Stream ``audio`` at ``speed`` times real time and finalize."""
    for i in range(0, len(audio), chunk):
        await session.add_audio(audio[i : i + chunk])
        await asyncio.sleep(chunk / STT_SAMPLE_RATE / speed)
    return await session.finalize()


async def alone(audio: NDArray[np.float32], chunk: int) -> tuple[Any, FakeLibrary]:
    engine = FakeEngine()
    async with await engine.stream("en") as session:
        transcript = await stream_paced(session, audio, chunk)
    return transcript, engine.libraries[0]


def kept_window(lib: FakeLibrary, start: int) -> NDArray[np.float32]:
    """The window of the last call at ``start``: the one the state keeps."""
    return {s: w for s, w in zip(lib.encoded, lib.windows, strict=True)}[start]


async def test_interleaved_sessions_each_transcribe_as_if_alone() -> None:
    cases = [(1, CHUNK), (2, WYOMING_CHUNK), (3, 700)]
    clips = [(varied(seed, 14.0), chunk) for seed, chunk in cases]
    expected = [await alone(audio, chunk) for audio, chunk in clips]
    journal: list[tuple[int, int, bool]] = []
    engine = FakeEngine(delay_s=0.004, journal=journal)
    sessions = [await engine.stream("en") for _ in clips]
    transcripts = await asyncio.gather(
        *(stream_paced(s, audio, chunk) for s, (audio, chunk) in zip(sessions, clips, strict=True))
    )
    for session in sessions:
        await session.close()
    for (want, solo), got, lib in zip(expected, transcripts, engine.libraries, strict=True):
        assert got == want
        # What the state is built from; how many calls at a pause were undone
        # depends on how much audio was pending each time the consumer planned.
        assert lib.kept == solo.kept
        for start in lib.kept:
            assert np.array_equal(kept_window(lib, start), kept_window(solo, start))
    order = [session for session, _, _ in journal]
    switches = sum(a != b for a, b in itertools.pairwise(order))
    assert switches > len(order) / 3, "the sessions' calls interleave"
    assert engine._sessions == set()


async def test_sessions_behind_take_turns() -> None:
    """Two sessions that each got a burst at once share the thread call by
    call, not burst by burst."""
    journal: list[tuple[int, int, bool]] = []
    engine = FakeEngine(delay_s=0.005, journal=journal)
    sessions = [await engine.stream("en") for _ in range(2)]
    for session in sessions:
        await session.add_audio(speech_like(10 * PASS_S))
    for session in sessions:
        await consumed(session)
    order = [session for session, _, _ in journal]
    assert len(order) == 20
    runs = [len(list(group)) for _, group in itertools.groupby(order)]
    assert max(runs) <= 2, f"turns, not bursts: {order}"
    for session in sessions:
        await session.close()


async def until(predicate: Callable[[], bool], within: float = 5.0) -> None:
    """Poll ``predicate`` until it is true."""
    deadline = time.perf_counter() + within
    while not predicate():
        assert time.perf_counter() < deadline, "condition not met in time"
        await asyncio.sleep(0.001)


async def until_called(lib: FakeLibrary, calls: int) -> None:
    await until(lambda: lib.calls >= calls)


async def test_the_call_at_a_pause_goes_before_other_sessions_calls() -> None:
    journal: list[tuple[int, int, bool]] = []
    engine = FakeEngine(delay_s=0.02, journal=journal)
    busy = await engine.stream("en")
    quiet = await engine.stream("en")
    await quiet.add_audio(speech_like(0.5))
    await busy.add_audio(speech_like(MAX_BACKLOG_S - 1))  # behind: urgent, back to back
    await until_called(engine.libraries[0], 2)
    before = len(journal)
    await quiet.add_audio(silence(0.1))  # a pause: the call the client waits on
    await until_called(engine.libraries[1], 1)
    assert [session for session, _, _ in journal[before:]] in ([1], [0, 1])
    assert journal[-1][2], "and it holds the GPU lock"
    for session in (busy, quiet):
        await session.close()


async def test_finalize_runs_the_rest_right_after_the_call_in_flight() -> None:
    """No call queued after ``finalize`` (here another session's, behind on a
    backlog) starts between the session's call in flight and the rest of its
    work, under a pass, which is one call."""
    journal: list[tuple[int, int, bool]] = []
    engine = FakeEngine(delay_s=0.03, journal=journal)
    other = await engine.stream("en")
    session = await engine.stream("en")
    await other.add_audio(speech_like(MAX_BACKLOG_S - 1))
    await until_called(engine.libraries[0], 1)
    await session.add_audio(speech_like(PASS_S))
    lib = engine.libraries[1]
    await until(lambda: lib.in_call)
    await session.add_audio(speech_like(PASS_S - 0.3))  # the rest: a tail under a pass
    transcript = await session.finalize()
    mine = [i for i, (s, _, _) in enumerate(journal) if s == 1]
    assert len(mine) == 2
    assert mine[1] == mine[0] + 1, journal
    assert transcript.text == f"p0 p{PASS}"
    for s in (other, session):
        await s.close()


async def test_a_long_tail_at_finalize_lets_other_sessions_calls_at_a_pause_go_between() -> None:
    """A session that finalizes with a backlog encodes it call by call, so
    another session's call at a pause waits for one call, not for the whole
    backlog."""
    journal: list[tuple[int, int, bool]] = []
    engine = FakeEngine(delay_s=0.02, journal=journal)
    long = await engine.stream("en")
    quiet = await engine.stream("en")
    await quiet.add_audio(speech_like(0.5))
    await long.add_audio(speech_like(10.0))
    await until_called(engine.libraries[0], 1)
    finalizing = asyncio.create_task(long.finalize())
    await until_called(engine.libraries[0], 3)
    await quiet.add_audio(silence(0.1))  # its call at a pause: a client waits on it too
    await until_called(engine.libraries[1], 1)
    await finalizing
    order = [session for session, _, _ in journal]
    pause = order.index(1)
    assert order.count(0) == 13  # 12 passes and the padded tail
    assert pause < len(order) - 1 and order[pause + 1 :].count(0) > 1, order
    assert order[:pause].count(0) <= 5, "it waited for one or two calls, not the backlog"
    for s in (long, quiet):
        await s.close()


def queued_by(call: Any, session: STTStreamSession) -> bool:
    """Whether a model-thread call waiting in line is one of ``session``'s jobs."""
    assert isinstance(session, ParakeetStreamSession)
    return getattr(call.rank, "func", None) == session._adapter.rank


async def test_a_session_that_disconnects_mid_stream_leaves_the_others_intact() -> None:
    audio = varied(4, 10.0)
    want, _ = await alone(audio, CHUNK)
    engine = FakeEngine(delay_s=0.01)
    gone = await engine.stream("en")
    stays = await engine.stream("en")
    await gone.add_audio(speech_like(MAX_BACKLOG_S - 1))
    task = asyncio.create_task(stream_paced(stays, audio, CHUNK))
    await until_called(engine.libraries[0], 3)
    started, release = threading.Event(), threading.Event()

    def block() -> None:
        started.set()
        release.wait(5)

    blocker = asyncio.create_task(engine._thread.run(block))
    await asyncio.to_thread(started.wait, 2)
    # its next call waits behind the blocker
    await until(lambda: any(queued_by(call, gone) for call in engine._thread._waiting))
    calls = engine.libraries[0].calls
    closing = asyncio.create_task(gone.close())  # the client went away
    await asyncio.sleep(0.01)
    release.set()
    await asyncio.gather(blocker, closing)
    assert engine.libraries[0].calls == calls, "its queued call never ran"
    assert await task == want
    assert engine.libraries[0].closed
    assert engine._sessions == {stays}
    await stays.close()
    await assert_engine_accepts_a_new_stream(engine)


async def test_a_session_failure_leaves_the_others_intact() -> None:
    audio = varied(5, 6.0)
    want, _ = await alone(audio, CHUNK)
    engine = FakeEngine()
    failing = await engine.stream("en")
    engine.libraries[0].fail_on = 1
    fine = await engine.stream("en")
    await failing.add_audio(speech_like(2 * PASS_S))
    assert await stream_paced(fine, audio, CHUNK) == want
    with pytest.raises(RuntimeError, match="encoder exploded"):
        await failing.finalize()
    await failing.close()
    await fine.close()


class _FakeEncoder:
    def __init__(self) -> None:
        self.modes: list[str] = []

    def set_attention_model(self, mode: str, context: Any = None) -> None:
        self.modes.append(mode)


def _fake_model(seen: list[tuple[int, bool]]) -> Any:
    """A model whose streams record, per call, the thread and whether the GPU
    lock was held."""
    from voiced import gpu

    class Stream:
        mel_buffer: Any = None
        cache: list[Any] = []
        audio_buffer: Any = None
        decoder_hidden: Any = None
        last_token: Any = None
        finalized_tokens: list[Any] = []
        draft_tokens: list[Any] = []
        decoding_config = SimpleNamespace(sentence=None)

        def __enter__(self) -> Stream:
            return self

        def __exit__(self, *exc: object) -> None:
            pass

        def add_audio(self, audio: Any) -> None:
            seen.append((threading.get_ident(), gpu.stt_running()))
            self.mel_buffer = np.zeros((1, len(audio) // 160 + 1))

    def transcribe_stream(context_size: Any, keep_original_attention: bool) -> Stream:
        assert keep_original_attention, "the engine sets the attention mode, once"
        return Stream()

    return SimpleNamespace(
        preprocessor_config=SimpleNamespace(hop_length=160),
        encoder_config=SimpleNamespace(subsampling_factor=8),
        encoder=_FakeEncoder(),
        transcribe_stream=transcribe_stream,
    )


@pytest.mark.model
async def test_several_sessions_share_one_gpu_lock_holder_and_one_attention_switch(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every session's calls run on the engine's one thread, the only holder of
    the GPU lock (``voiced.gpu``), and hold it exactly when urgent; local
    attention is switched on once and back once the last session closes."""
    pytest.importorskip("parakeet_mlx")
    from voiced import gpu

    monkeypatch.setattr(gpu, "lock_path", lambda: tmp_path / gpu.LOCK_NAME)
    monkeypatch.setattr(gpu, "_fds", {})
    seen: list[tuple[int, bool]] = []
    urgent: list[bool] = []
    model = _fake_model(seen)

    class Engine(ParakeetEngine):
        def _new_library(self, model: Any) -> _Library:
            lib = super()._new_library(model)
            encode = lib.encode

            def spy(window: NDArray[np.float32], start: int, flag: bool) -> None:
                urgent.append(flag)
                encode(window, start, flag)

            lib.encode = spy  # type: ignore[method-assign]
            return lib

        def _new_detector(self) -> Endpointer:
            return energy_speech()

    engine = Engine()
    engine._model = model
    engine._attention = parakeet._Attention(model)
    try:
        sessions = [await engine.stream("en") for _ in range(3)]
        assert model.encoder.modes == []
        await sessions[0].add_audio(speech_like(URGENT_BACKLOG_S + 2 * PASS_S))  # behind
        audio = np.concatenate([speech_like(1.0), silence(0.3), speech_like(1.3)])
        await asyncio.gather(*(stream_paced(s, audio, CHUNK) for s in sessions[1:]))
        await sessions[0].finalize()
        assert model.encoder.modes == ["rel_pos_local_attn"]
        for session in sessions[:2]:
            await session.close()
        assert model.encoder.modes == ["rel_pos_local_attn"], "one session is still open"
        await sessions[2].close()
        assert model.encoder.modes == ["rel_pos_local_attn", "rel_pos"]
    finally:
        for fd in gpu._fds.values():
            os.close(fd)
    assert len({thread for thread, _ in seen}) == 1
    assert [held for _, held in seen] == urgent
    assert True in urgent and False in urgent
    assert not gpu.stt_running()


# --- real model -------------------------------------------------------------


@pytest.fixture(scope="module")
def engine() -> ParakeetEngine:
    import mlx.core as mx

    engine = ParakeetEngine()
    t0 = time.perf_counter()
    asyncio.run(engine.load())
    print(
        f"\nparakeet load and warm-up: {time.perf_counter() - t0:.1f}s, "
        f"MLX peak {mx.get_peak_memory() / 1e9:.2f} GB (fp32 weights cast to bf16)"
    )
    return engine


def attention_class(engine: ParakeetEngine) -> str:
    return type(engine._model.encoder.layers[0].self_attn).__name__


def at_peak(audio: NDArray[np.float32], dbfs: float) -> NDArray[np.float32]:
    """Scale so the loudest 32 ms frame sits at ``dbfs``, a realistic microphone level."""
    frames = audio[: len(audio) // FRAME * FRAME].reshape(-1, FRAME)
    peak = float(np.sqrt(np.mean(np.square(frames), axis=1)).max())
    return (audio / peak * 10 ** (dbfs / 20)).astype(np.float32)


def with_room(audio: NDArray[np.float32], dbfs: float, seed: int) -> NDArray[np.float32]:
    rng = np.random.default_rng(seed)
    return (audio + rng.standard_normal(len(audio)) * 10 ** (dbfs / 20)).astype(np.float32)


async def paced_stream(
    session: ParakeetStreamSession, clip: NDArray[np.float32], chunk: int = CHUNK
) -> tuple[list[float], list[str]]:
    """Feed ``chunk``-sized pieces at the rate a microphone delivers them."""
    add_times: list[float] = []
    partials: list[str] = []
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
) -> tuple[float, Any]:
    """Stream ``clip`` in real time, then finalize: the latency and the transcript."""
    async with await engine.stream("en") as session:
        await paced_stream(session, clip, chunk)
        t = time.perf_counter()
        transcript = await session.finalize()
        return time.perf_counter() - t, transcript


def report(label: str, latencies: list[float]) -> None:
    ms = sorted(x * 1000 for x in latencies)
    print(
        f"{label}: worst {ms[-1]:.0f} ms, median {statistics.median(ms):.0f} ms, "
        f"over 50 ms {sum(m > 50 for m in ms)} of {len(ms)}"
    )


def utterance(*parts: NDArray[np.float32], gap_s: float = 0.15) -> NDArray[np.float32]:
    """Clips joined by pauses shorter than the endpointer's window: one utterance."""
    gap = silence(gap_s)
    return np.concatenate([p for part in parts for p in (part, gap)][:-1])


def trimmed(audio: NDArray[np.float32]) -> NDArray[np.float32]:
    """Without leading and trailing quiet frames."""
    frames = audio[: len(audio) // FRAME * FRAME].reshape(-1, FRAME)
    loud = np.nonzero(np.sqrt(np.mean(np.square(frames), axis=1)) > 0.003)[0]
    return audio[int(loud[0]) * FRAME : (int(loud[-1]) + 1) * FRAME]


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
        clip = np.concatenate([speech, silence(SILENCE_WINDOW_S)])
        add_times, partials = await paced_stream(session, clip)
        transcript = await session.finalize()
    first_partial = next(i for i, p in enumerate(partials) if p) + 1
    print(
        f"stream: max add_audio {max(add_times) * 1000:.2f} ms, {session.passes} calls, "
        f"first partial after {first_partial} chunks: {transcript.text!r}"
    )
    assert any(partials), "no partial hypothesis before finalize"
    assert max(add_times) < 0.02, "add_audio must not block on the encoder"
    assert_transcribed(transcript.text, EXPECTED, EXPECTED_WORDS)
    assert transcript.language == "en"
    assert transcript.duration_s == pytest.approx(len(clip) / STT_SAMPLE_RATE)
    assert transcript.segments and transcript.segments[0].start < 0.5


@pytest.mark.model
@pytest.mark.parametrize("chunk", [CHUNK, WYOMING_CHUNK], ids=["100ms", "64ms"])
@pytest.mark.parametrize("noise_dbfs", [None, -55.0], ids=["silence", "noise-55dBFS"])
async def test_finalize_latency_over_alignments(
    engine: ParakeetEngine, speech: NDArray[np.float32], chunk: int, noise_dbfs: float | None
) -> None:
    """End of speech at every 20 ms offset relative to the chunk and pass grid,
    then the endpointer's silence window (digital, or room noise at -55 dBFS
    with speech at a -6 dBFS peak), then finalize. The bound assumes the GPU is
    not shared: one run at 774 ms probably overlapped another process using it,
    and did not recur in 10 runs alone (320 alignments, worst 38 ms)."""
    voice = speech if noise_dbfs is None else at_peak(speech, -6.0)
    latencies: list[float] = []
    for lead_ms in range(0, 640, 20):
        clip = np.concatenate([silence(lead_ms / 1000), voice, silence(SILENCE_WINDOW_S)])
        if noise_dbfs is not None:
            clip = with_room(clip, noise_dbfs, seed=lead_ms)
        finalize_s, transcript = await timed_finalize(engine, clip, chunk)
        latencies.append(finalize_s)
        assert_transcribed(transcript.text, EXPECTED, EXPECTED_WORDS)
    report(f"finalize, {chunk} samples, noise {noise_dbfs}", latencies)
    assert max(latencies) < FINALIZE_WORST_S


@pytest.mark.model
@pytest.mark.parametrize("peak_dbfs", [-20.0, -25.0])
async def test_quiet_speaker_over_room_noise(
    engine: ParakeetEngine, speech: NDArray[np.float32], peak_dbfs: float
) -> None:
    """Pauses are Silero's per-frame decision, not a level threshold, so a quiet
    speaker 25 to 30 dB over the room still gets the early final call."""
    latencies: list[float] = []
    for lead_ms in range(0, 640, 80):
        clip = np.concatenate([silence(lead_ms / 1000), at_peak(speech, peak_dbfs)])
        clip = with_room(np.concatenate([clip, silence(SILENCE_WINDOW_S)]), -50.0, seed=lead_ms)
        finalize_s, transcript = await timed_finalize(engine, clip, WYOMING_CHUNK)
        latencies.append(finalize_s)
        assert_transcribed(transcript.text, EXPECTED, EXPECTED_WORDS)
    report(f"finalize, {peak_dbfs} dBFS peak over -50 dBFS", latencies)
    assert max(latencies) < FINALIZE_WORST_S
    assert statistics.median(latencies) < FINALIZE_BUDGET_S


@pytest.mark.model
async def test_long_utterance_keeps_calls_bounded(
    engine: ParakeetEngine,
    speech: NDArray[np.float32],
    speech_with_trailing_silence: NDArray[np.float32],
) -> None:
    """The right context bounds what each call re-encodes, so a 20 s utterance
    costs no more per call than a short one, and finalize stays fast."""
    parts = [trimmed(speech), trimmed(speech_with_trailing_silence)] * 3
    clip = utterance(*parts)
    reference = await engine.transcribe(AudioInput(clip), "en")
    latencies: list[float] = []
    for lead_ms in (0, 270, 530):
        tail = silence(SILENCE_WINDOW_S)
        finalize_s, transcript = await timed_finalize(
            engine, np.concatenate([silence(lead_ms / 1000), clip, tail])
        )
        latencies.append(finalize_s)
        assert word_accuracy(transcript.text, reference.text) >= 0.9, transcript.text
    report(f"finalize after {len(clip) / STT_SAMPLE_RATE:.0f}s", latencies)
    assert max(latencies) < FINALIZE_WORST_S


@pytest.mark.model
async def test_streamed_timestamps_are_in_session_time(
    engine: ParakeetEngine,
    speech: NDArray[np.float32],
    speech_with_trailing_silence: NDArray[np.float32],
) -> None:
    """After a 1.5 s lead-in and across a dropped pause, the streamed transcript
    starts where the speech starts and ends where batch ends it. (Batch puts its
    first token up to 0.5 s before the speech, and the two split sentences
    differently, so only the outer times are compared, to the best reference.)"""
    clip = np.concatenate(
        [silence(1.5), speech, silence(1.5), trimmed(speech_with_trailing_silence)]
    )
    clip = np.concatenate([clip, silence(SILENCE_WINDOW_S)])
    batch = await engine.transcribe(AudioInput(clip), "en")
    _, streamed = await timed_finalize(engine, clip)
    print(f"batch  {[(s.start, s.end) for s in batch.segments]}")
    print(f"stream {[(s.start, s.end) for s in streamed.segments]}")
    onset = (len(clip) - len(trimmed(clip))) / STT_SAMPLE_RATE  # nothing loud trails
    assert streamed.segments[0].start == pytest.approx(onset, abs=0.25)
    assert streamed.segments[-1].end == pytest.approx(batch.segments[-1].end, abs=0.25)
    for earlier, later in itertools.pairwise(streamed.segments):
        assert earlier.end <= later.start + 1e-3


def with_pauses(speech: NDArray[np.float32], gap_ms: int) -> NDArray[np.float32]:
    """The fixture with ``gap_ms`` of silence inserted at every gap between words."""
    frames = speech[: len(speech) // FRAME * FRAME].reshape(-1, FRAME)
    env = np.sqrt(np.mean(np.square(frames), axis=1))
    cuts = [i * FRAME for i in range(1, len(env)) if env[i] < env.max() * 0.05 <= env[i - 1]]
    gap = silence(gap_ms / 1000)
    return np.concatenate([p for part in np.split(speech, cuts) for p in (part, gap)])


@pytest.mark.model
@pytest.mark.parametrize("gap_ms", [110, 250, 400])
async def test_undone_calls_leave_the_real_stream_exact(
    engine: ParakeetEngine, speech: NDArray[np.float32], gap_ms: int
) -> None:
    """With real weights: every speculative call that speech followed is undone,
    so the transcript equals the one from encoding the planner's kept windows
    straight through the library, with no checkpoint or restore."""

    clip = trimmed(with_pauses(speech, gap_ms))

    def run() -> tuple[str, str, int]:
        assert engine._attention is not None
        lib = parakeet._LibraryStream(engine._model, engine._attention)
        planned = _StepRunner(lib, _Planner(Endpointer(silence_ms=0)), lambda: _Rank.AHEAD)
        windows: list[tuple[NDArray[np.float32], int]] = []
        encode = lib.encode

        def spy(window: NDArray[np.float32], start: int, urgent: bool) -> None:
            windows.append((window, start))
            encode(window, start, urgent)

        lib.encode = spy  # type: ignore[method-assign]
        try:
            for chunk in chunked(clip, WYOMING_CHUNK):
                feed(planned, chunk)
            result = flush(planned)
        finally:
            planned.close()
        # what the state holds: the last call made at each grid position
        kept = {start: window for window, start in windows}
        straight = parakeet._LibraryStream(engine._model, engine._attention)
        try:
            for start, window in sorted(kept.items()):
                straight.encode(window, start, urgent=False)
            reference = straight.result()
        finally:
            straight.close()
        return result.text, reference.text, planned.rollbacks

    ours, reference, rollbacks = await engine._thread.run(run)
    print(f"{gap_ms} ms pauses, {rollbacks} undone: {ours!r}")
    assert rollbacks >= 1
    assert ours == reference


def split_at_middle_gap(audio: NDArray[np.float32]) -> tuple[NDArray[np.float32], ...]:
    """The fixture cut at the quiet frame nearest its middle, between two words."""
    frames = audio[: len(audio) // FRAME * FRAME].reshape(-1, FRAME)
    env = np.sqrt(np.mean(np.square(frames), axis=1))
    quiet = np.nonzero(env < env.max() * 0.05)[0]
    quiet = quiet[(quiet > 5) & (quiet < len(env) - 5)]
    cut = int(quiet[np.argmin(np.abs(quiet - len(env) // 2))]) * FRAME
    return audio[:cut], audio[cut:]


@pytest.mark.model
@pytest.mark.parametrize("pause_s", [1.5, 2.0, 2.5])
async def test_a_long_pause_inside_an_utterance_adds_no_words(
    engine: ParakeetEngine, speech: NDArray[np.float32], pause_s: float
) -> None:
    """A pause shorter than the hold, then more speech: the pause is never encoded
    on its own, so no words are invented or repeated around it."""
    first, second = split_at_middle_gap(speech)
    for lead_ms in (0, 200, 400, 600):
        clip = np.concatenate(
            [silence(lead_ms / 1000), first, silence(pause_s), second, silence(SILENCE_WINDOW_S)]
        )
        for chunk in (CHUNK, WYOMING_CHUNK):
            _, transcript = await timed_finalize(engine, clip, chunk)
            said = words(transcript.text)
            repeated = [w for w, nxt in itertools.pairwise(said) if w == nxt]
            assert not repeated, transcript.text
            assert word_accuracy(transcript.text, EXPECTED) >= 0.9, transcript.text


@pytest.mark.model
@pytest.mark.parametrize("lead_ms", [300, 500, 800])
async def test_room_noise_before_speech_adds_no_words(
    engine: ParakeetEngine, speech: NDArray[np.float32], lead_ms: int
) -> None:
    """Without the speech-start gate, a call of room noise alone was decoded as
    words ("We have a turn on the kitchen lights")."""
    voice = at_peak(speech, -6.0)
    for seed in range(3):
        clip = np.concatenate([silence(lead_ms / 1000), voice, silence(SILENCE_WINDOW_S)])
        _, transcript = await timed_finalize(engine, with_room(clip, -55.0, seed))
        assert words(transcript.text)[:1] == ["turn"], f"leading words: {transcript.text!r}"
        assert_transcribed(transcript.text, EXPECTED, EXPECTED_WORDS)


@pytest.mark.model
async def test_streaming_trailing_silence_fixture(
    engine: ParakeetEngine, speech_with_trailing_silence: NDArray[np.float32]
) -> None:
    finalize_s, transcript = await timed_finalize(engine, speech_with_trailing_silence)
    print(f"stream (fixture with 1 s of silence): finalize {finalize_s * 1000:.1f} ms")
    assert_transcribed(transcript.text, EXPECTED_TRAILING, TRAILING_WORDS)
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
    assert session.passes >= len(speech) // PASS


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
        await session.add_audio(speech[: 2 * PASS])
        await asyncio.sleep(0.5)
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
    transcript = await engine.transcribe(AudioInput(speech), "en")
    assert_transcribed(transcript.text, EXPECTED, EXPECTED_WORDS)


@pytest.mark.model
async def test_model_calls_survive_a_busy_default_pool(
    engine: ParakeetEngine, speech: NDArray[np.float32]
) -> None:
    """MLX arrays belong to the thread that created them. With the default pool
    grown by other work, and a close cancelled mid-call, every later batch and
    stream still runs, because the engine runs all its calls on one thread."""
    busy = threading.Event()
    hold = asyncio.create_task(run_blocking(busy.wait, 60))
    try:
        for _ in range(5):
            await engine.transcribe(AudioInput(speech), "en")
            async with await engine.stream("en") as session:
                await session.add_audio(speech)
                assert_transcribed((await session.finalize()).text, EXPECTED, EXPECTED_WORDS)
        session = await engine.stream("en")
        await session.add_audio(speech)
        await asyncio.sleep(0.05)
        closing = asyncio.create_task(session.close())
        await asyncio.sleep(0)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        _, transcript = await timed_finalize(engine, np.concatenate([speech, silence(0.2)]))
        assert_transcribed(transcript.text, EXPECTED, EXPECTED_WORDS)
    finally:
        busy.set()
        await hold


@pytest.mark.model
async def test_peak_memory_while_serving(
    engine: ParakeetEngine,
    speech: NDArray[np.float32],
    speech_with_trailing_silence: NDArray[np.float32],
) -> None:
    """MLX's peak allocation in this process while serving a 5 minute batch request
    and a 16 s stream, after loading, and what MLX still holds afterwards. Loading
    itself peaks higher, once (parakeet-mlx reads fp32 weights and casts them;
    ADR 0008). The budget for STT and TTS together is 3 GB."""
    import mlx.core as mx

    pair = [trimmed(speech), trimmed(speech_with_trailing_silence)]
    long = utterance(*pair * 60, gap_s=0.5)[: seconds(300.0)]
    clip = utterance(*pair * 3)
    mx.reset_peak_memory()
    transcript = await engine.transcribe(AudioInput(long), "en")
    batch_gb, cached_gb = mx.get_peak_memory() / 1e9, mx.get_cache_memory() / 1e9
    assert words(transcript.text).count("thermostat") >= 45, transcript.text[:200]
    async with await engine.stream("en") as session:
        for chunk in chunked(clip, seconds(1.0)):
            await session.add_audio(chunk)
        await session.finalize()
    peak_gb = mx.get_peak_memory() / 1e9
    print(
        f"MLX peak: {batch_gb:.2f} GB after a {len(long) / STT_SAMPLE_RATE:.0f}s batch "
        f"({cached_gb:.2f} GB cached after), {peak_gb:.2f} GB with a stream"
    )
    assert peak_gb < 3.0
    assert cached_gb < 0.1


# --- real model, several sessions at once -----------------------------------


@pytest.mark.model
async def test_concurrent_streams_each_transcribe_as_if_alone(
    engine: ParakeetEngine,
    speech: NDArray[np.float32],
    speech_with_trailing_silence: NDArray[np.float32],
) -> None:
    """Interleaved on one model, every session's transcript, segments
    included, is the one it gets alone: the stream states share only the
    weights."""
    other = trimmed(speech_with_trailing_silence)
    clips = [
        (np.concatenate([silence(0.3), speech, silence(SILENCE_WINDOW_S)]), WYOMING_CHUNK),
        (np.concatenate([silence(0.55), utterance(other, speech), silence(0.1)]), CHUNK),
        (np.concatenate([silence(0.1), utterance(speech, other, speech)]), WYOMING_CHUNK),
    ]
    solo = [(await timed_finalize(engine, clip, chunk))[1] for clip, chunk in clips]

    async def run(clip: NDArray[np.float32], chunk: int, lead_s: float) -> Any:
        await asyncio.sleep(lead_s)
        return (await timed_finalize(engine, clip, chunk))[1]

    together = await asyncio.gather(
        *(run(clip, chunk, 0.37 * i) for i, (clip, chunk) in enumerate(clips))
    )
    for alone_t, together_t in zip(solo, together, strict=True):
        print(f"together: {together_t.text!r}")
        assert together_t == alone_t
    assert engine._sessions == set()
    assert attention_class(engine) == "RelPositionMultiHeadAttention"
    transcript = await engine.transcribe(AudioInput(speech), "en")
    assert_transcribed(transcript.text, EXPECTED, EXPECTED_WORDS)


@pytest.mark.model
async def test_attention_stays_local_until_the_last_stream_closes(
    engine: ParakeetEngine, speech: NDArray[np.float32]
) -> None:
    first = await engine.stream("en")
    second = await engine.stream("en")
    await first.add_audio(speech[: 2 * PASS])
    await second.add_audio(speech[: 2 * PASS])
    await asyncio.sleep(0.5)
    await first.close()
    assert attention_class(engine) == "RelPositionMultiHeadLocalAttention"
    await second.add_audio(speech[2 * PASS :])
    transcript = await second.finalize()
    assert_transcribed(transcript.text, EXPECTED, EXPECTED_WORDS)
    await second.close()
    assert attention_class(engine) == "RelPositionMultiHeadAttention"


async def concurrent_finalizes(
    engine: ParakeetEngine,
    clip: NDArray[np.float32],
    streams: int,
    trailing_s: float,
    seconds_: float,
) -> list[float]:
    """``streams`` clients, each speaking ``clip`` again and again in real time
    (64 ms chunks, a random lead-in so the ends fall at every phase of the
    others' calls, ``trailing_s`` of silence), finalizing each time."""
    latencies: list[float] = []
    until = time.perf_counter() + seconds_

    async def client(seed: int) -> None:
        rng = np.random.default_rng(seed)
        while time.perf_counter() < until:
            audio = np.concatenate([silence(rng.uniform(0, 0.62)), clip, silence(trailing_s)])
            finalize_s, transcript = await timed_finalize(engine, audio, WYOMING_CHUNK)
            latencies.append(finalize_s)
            assert_transcribed(transcript.text, EXPECTED, EXPECTED_WORDS)
            await asyncio.sleep(rng.uniform(0, 0.8))

    await asyncio.gather(*(client(seed) for seed in range(streams)))
    return latencies


@pytest.mark.model
async def test_finalize_within_target_with_the_default_streams(
    engine: ParakeetEngine, speech: NDArray[np.float32]
) -> None:
    """``stt.streams`` clients at once, ends after the endpointer's silence
    window: every finalize within the 150 ms of ADR 0008. A session has at
    most one call queued or running, so the call at a pause waits for one
    call of the other session at most, and runs while the window elapses.
    Ahead of it can also be its own regular call planned in the same pass,
    and the other session's call can be its finalize, which may encode more
    than one window (ADR 0020)."""
    latencies = await concurrent_finalizes(engine, speech, STT_STREAMS, SILENCE_WINDOW_S, 45.0)
    report(f"finalize, {STT_STREAMS} streams, {SILENCE_WINDOW_S * 1000:.0f} ms window", latencies)
    assert max(latencies) < FINALIZE_WORST_S


@pytest.mark.model
async def test_finalize_with_three_streams(
    engine: ParakeetEngine, speech: NDArray[np.float32]
) -> None:
    """``stt.streams = 3``, the opt-in: the call at a pause can wait for both
    other sessions' calls, and when they coincide a finalize ends about
    190 ms after the window (ADR 0020). That is rare, so the target holds at
    the 95th percentile and the worst case is bounded at twice it."""
    latencies = await concurrent_finalizes(engine, speech, 3, SILENCE_WINDOW_S, 45.0)
    report(f"finalize, 3 streams, {SILENCE_WINDOW_S * 1000:.0f} ms window", latencies)
    assert float(np.percentile(latencies, 95)) < FINALIZE_WORST_S
    assert max(latencies) < 2 * FINALIZE_WORST_S


@pytest.mark.model
async def test_push_to_talk_finalize_with_the_default_streams(
    engine: ParakeetEngine, speech: NDArray[np.float32]
) -> None:
    """No trailing silence: the median stays the one-stream cost. The worst
    case is over the 150 ms target, the known miss in ADR 0015 and 0020 (a
    finalize also waits for one other session's call); the 2x bound only
    catches a regression past it."""
    latencies = await concurrent_finalizes(engine, speech, STT_STREAMS, 0.0, 45.0)
    report(f"push-to-talk finalize, {STT_STREAMS} streams", latencies)
    assert statistics.median(latencies) < FINALIZE_WORST_S
    assert max(latencies) < 2 * FINALIZE_WORST_S
