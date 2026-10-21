"""Nemotron engine tests.

The first half runs anywhere: metadata, languages, and the decoder and the
stream session against a fake mlx-audio session that holds audio back the
way the library's front end and encoder do. The second half is marked
``model`` and needs the real weights, both catalogued models:
``VOICED_MODEL_TESTS=1 uv run pytest tests/stt/test_nemotron.py -m model -s``.
"""

from __future__ import annotations

import asyncio
import gc
import statistics
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from numpy.typing import NDArray

from tests.stt.conftest import words
from voiced.config import Settings
from voiced.context import ServiceContext
from voiced.engines import nemotron
from voiced.engines.base import (
    STT_SAMPLE_RATE,
    AudioInput,
    EngineError,
    EngineNotLoaded,
    STTEngine,
    Transcript,
    UnsupportedLanguage,
)
from voiced.engines.nemotron import AUTO, CHUNK_MS, NemotronEngine, _Decoder, _to_transcript
from voiced.engines.registry import ModelRefused, create_stt_engine, get_stt_engine
from voiced.health import HealthState
from voiced.models.catalogue import NEMOTRON_LOCALES
from voiced.queue import InferenceQueue
from voiced.threads import run_blocking

MULTILINGUAL = "mlx-community/nemotron-3.5-asr-streaming-0.6b"
ENGLISH = "nvidia/nemotron-speech-streaming-en-0.6b"
HOP = 160
CHUNK_MEL = 16  # two 80 ms encoder frames of 10 ms mel frames
CHUNK = CHUNK_MEL * HOP
FIRST = (CHUNK_MEL - 1) * HOP + 256  # the front end's half window


def speech_like(s: float, level: float = 0.1) -> NDArray[np.float32]:
    t = np.arange(round(s * STT_SAMPLE_RATE)) / STT_SAMPLE_RATE
    return (level * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


# --- no model needed --------------------------------------------------------


def test_metadata() -> None:
    assert NemotronEngine.name == "nemotron"
    assert NemotronEngine.model_id == MULTILINGUAL
    assert NemotronEngine.license == "OpenMDW-1.1"
    assert NemotronEngine.max_streams == 1
    assert NemotronEngine.languages == list(NEMOTRON_LOCALES)
    assert len(set(NEMOTRON_LOCALES)) == 32
    assert len({t.split("-")[0] for t in NEMOTRON_LOCALES}) == 28
    assert NEMOTRON_LOCALES[0] == "en-US"
    assert CHUNK_MS == 160


def test_registered_as_entry_point() -> None:
    assert get_stt_engine("nemotron") is NemotronEngine
    assert issubclass(NemotronEngine, STTEngine)


def test_the_catalogue_decides_licence_languages_and_models() -> None:
    english = create_stt_engine("nemotron", ENGLISH)
    assert english.license == "NVIDIA Open Model License"
    assert english.languages == ["en-US"]
    with pytest.raises(ModelRefused, match="not in voiced's model catalogue"):
        NemotronEngine("nvidia/nemotron-speech-streaming-en-1.1b")
    assert NemotronEngine("/models/my-nemotron").languages == list(NEMOTRON_LOCALES)


def test_language_prompts() -> None:
    engine = NemotronEngine()
    assert engine.prompt(None) == AUTO
    assert engine.prompt("en") == "en-US"
    assert engine.prompt("en_GB") == "en-GB"
    assert engine.prompt("pt") == "pt-BR"
    assert engine.prompt("pt-PT") == "pt-PT"
    assert engine.prompt("fr-CA") == "fr-CA"
    assert engine.prompt("de-AT") == "de-DE"  # an unserved region: its language's locale
    for bad in ["el", "he-IL", "xx", ""]:  # adaptation-ready locales are not offered
        with pytest.raises(UnsupportedLanguage):
            engine.prompt(bad)
    english = NemotronEngine(ENGLISH)
    assert english.prompt("en-GB") == "en-US"
    with pytest.raises(UnsupportedLanguage):
        english.prompt("de")


def test_region_hints_reach_the_engine_through_the_service(settings: Settings) -> None:
    """The transports resolve a hint with ``check_language``; the locale
    must survive it, or ``pt-PT`` would be transcribed as Brazilian."""
    settings.stt.languages = ["en", "pt", "es"]
    engine = NemotronEngine()
    ctx = ServiceContext(
        service="stt",
        settings=settings,
        engine=engine,
        queue=InferenceQueue(1),
        health=HealthState(
            service="stt", engine="nemotron", model="m", version="0", queue_capacity=1
        ),
    )
    assert ctx.languages[:2] == ["en-US", "en-GB"]
    for hint, prompt in [("pt-PT", "pt-PT"), ("es-ES", "es-ES"), ("en", "en-US"), ("pt", "pt-BR")]:
        tag = ctx.check_language(hint)
        assert tag is not None and engine.prompt(tag) == prompt


async def test_methods_before_load_raise() -> None:
    engine = NemotronEngine()
    with pytest.raises(EngineNotLoaded):
        await engine.transcribe(AudioInput(samples=speech_like(1.0)))
    with pytest.raises(EngineNotLoaded):
        await engine.stream()


def test_to_transcript_normalizes_and_clamps() -> None:
    def token(text: str, start: float) -> SimpleNamespace:
        return SimpleNamespace(text=text, start=start, end=start + 0.08)

    def sentence(text: str, *tokens: SimpleNamespace) -> SimpleNamespace:
        return SimpleNamespace(text=text, start=tokens[0].start, end=tokens[-1].end, tokens=tokens)

    result = SimpleNamespace(
        text=" Hello  there. Bye. ",
        sentences=[
            # The period comes a look-ahead late; the times are the words'.
            sentence(
                " Hello  there.", token(" Hello", -0.1), token(" there", 0.72), token(".", 1.6)
            ),
            sentence("  ", token(" ", 1.6)),
            sentence("Bye.", token(" ", 1.6), token("Bye", 1.68), token(".", 9.0)),
        ],
    )
    t = _to_transcript(result, "en", 2.0)
    assert t.text == "Hello there. Bye."
    assert [(s.start, s.end, s.text) for s in t.segments] == [
        (0.0, 0.8, "Hello there."),
        (1.68, 1.76, "Bye."),
    ]


class FakeLibrarySession:
    """mlx-audio's ``NemotronStreamingSession`` as far as the decoder sees it.

    ``step`` ingests at most one chunk of queued audio, as the library does.
    The front end emits a 10 ms frame once the audio covers half an FFT
    window past its centre, and the encoder encodes whole chunks of
    :data:`CHUNK_MEL` frames: each encoded chunk decodes to one word. After
    ``close`` the rest is encoded and decoded over two steps."""

    def __init__(self, held: list[bool], first_chunk_mel: int = CHUNK_MEL) -> None:
        self._encoder = SimpleNamespace(chunk_mel=CHUNK_MEL, first_chunk_mel=first_chunk_mel)
        self._frontend = SimpleNamespace(lookahead_samples=256)
        self.fed: list[int] = []
        self.held = held  # whether the GPU lock was held at each feed
        self.closed = False
        self.done = False
        self._queued = 0
        self._ingested = 0
        self._encoded = 0
        self._flush = 2

    def feed(self, samples: NDArray[np.float32]) -> None:
        assert not self.closed
        self.fed.append(len(samples))
        self._queued += len(samples)
        self.held.append(_held[0])

    def step(self, *, max_decode_tokens: int) -> list[str]:
        assert max_decode_tokens >= 1000
        take = min(self._queued, CHUNK)
        self._queued -= take
        self._ingested += take
        frames = max(0, (self._ingested - 256) // HOP + 1)
        first = self._encoder.first_chunk_mel
        words = []
        while first + self._encoded * CHUNK_MEL <= frames:
            self._encoded += 1
            words.append(f" w{self._encoded}")
        if self.closed and not self._queued:
            self._flush -= 1
            self.done = self._flush == 0
            if self.done:
                words.append(" end")
        return words

    def close(self) -> None:
        self.closed = True


def fake_model(held: list[bool] | None = None, first_chunk_mel: int = CHUNK_MEL) -> Any:
    """Just what the engine reads of a model: the mel hop and a session."""
    sessions: list[FakeLibrarySession] = []

    def create_streaming_session(language: str) -> FakeLibrarySession:
        sessions.append(FakeLibrarySession(held if held is not None else [], first_chunk_mel))
        return sessions[-1]

    return SimpleNamespace(
        preprocessor_config=SimpleNamespace(hop_length=HOP),
        create_streaming_session=create_streaming_session,
        sessions=sessions,
    )


_held = [False]


@contextmanager
def fake_turn() -> Iterator[None]:
    _held[0] = True
    try:
        yield
    finally:
        _held[0] = False


@pytest.fixture
def fake_mlx(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The GPU lock as a flag, and MLX's cache clears counted."""
    monkeypatch.setattr(nemotron, "stt_turn", fake_turn)
    cleared: list[int] = []
    monkeypatch.setattr(nemotron, "_clear_cache", lambda: cleared.append(1))
    return cleared


def test_each_push_is_encoded_as_it_is_fed(fake_mlx: list[int]) -> None:
    """Fed whole chunks from the start, the fake, like the library, would
    encode each chunk only when the next arrives: the front end's half
    window keeps the last frame back. The longer first piece ends on a
    chunk of frames, so nothing is held back."""
    held: list[bool] = []
    decoder = _Decoder(fake_model(held), "en-US")
    assert (decoder.first, decoder.chunk) == (FIRST, CHUNK)
    decoder.push(np.zeros(FIRST, dtype=np.float32))
    assert decoder.text == " w1"  # nothing held back
    decoder.push(np.zeros(CHUNK, dtype=np.float32))
    assert decoder.text == " w1 w2"
    text = decoder.finish(np.zeros(CHUNK // 2, dtype=np.float32))
    assert text == " w1 w2 end"
    # The first piece is over a chunk: fed in two, one step after each.
    assert decoder._session.fed == [CHUNK, FIRST - CHUNK, CHUNK, CHUNK // 2]
    assert held == [False, False, False, True]  # only the finalize takes the GPU lock


def test_the_english_models_shorter_first_chunk() -> None:
    """NVIDIA's English checkpoint encodes a first chunk of 9 mel frames:
    the first piece is sized by it, and each piece is still encoded as fed."""
    decoder = _Decoder(fake_model(first_chunk_mel=9), "en-US")
    assert (decoder.first, decoder.chunk) == (8 * HOP + 256, CHUNK)
    decoder.push(np.zeros(decoder.first, dtype=np.float32))
    assert decoder.text == " w1"
    decoder.push(np.zeros(CHUNK, dtype=np.float32))
    assert decoder.text == " w1 w2"


class FakeEngine(NemotronEngine):
    """Loaded with the fake model; every MLX call is the fake library's."""

    def __init__(self, model_id: str | None = None) -> None:
        super().__init__(model_id)
        self._model = fake_model()
        self._first, self._chunk = FIRST, CHUNK


async def test_a_session_encodes_whole_pieces_and_finalize_the_rest(
    fake_mlx: list[int],
) -> None:
    engine = FakeEngine()
    async with await engine.stream("de") as session:
        assert session.partial() == ""
        for _ in range(10):  # 64 ms pieces: 640 ms
            await session.add_audio(speech_like(0.064))
        library = engine._model.sessions[0]
        assert library.fed == [CHUNK, FIRST - CHUNK, CHUNK, CHUNK]
        assert session.partial() == "w1 w2 w3"
        transcript = await session.finalize()
    assert library.fed[-1] == 10 * 1024 - FIRST - 2 * CHUNK
    assert transcript.text == "w1 w2 w3 end"
    assert transcript.language == "de"
    assert transcript.duration_s == pytest.approx(0.64)
    assert [(s.start, s.end) for s in transcript.segments] == [(0.0, 0.64)]
    assert engine._session is None
    assert engine._clearing is not None
    await engine._clearing  # MLX's cache is emptied once the session is closed
    assert fake_mlx == [1]


async def test_a_session_shorter_than_a_piece_is_decoded_at_finalize(
    fake_mlx: list[int],
) -> None:
    engine = FakeEngine()
    async with await engine.stream() as session:
        await session.add_audio(speech_like(0.05))
        transcript = await session.finalize()
    assert engine._model.sessions[0].fed == [800]
    assert transcript.text == "end" and transcript.language is None


async def test_an_empty_transcript_has_no_segment(fake_mlx: list[int]) -> None:
    engine = FakeEngine()
    async with await engine.stream("en") as session:
        transcript = await session.finalize()
    assert transcript.text == "end" and len(transcript.segments) == 1
    engine._model.create_streaming_session = lambda language: SimpleNamespace(
        _encoder=SimpleNamespace(chunk_mel=CHUNK_MEL, first_chunk_mel=CHUNK_MEL),
        _frontend=SimpleNamespace(lookahead_samples=256),
        feed=lambda samples: None,
        step=lambda max_decode_tokens: [],
        close=lambda: None,
        done=True,
    )
    async with await engine.stream("en") as session:
        transcript = await session.finalize()
    assert transcript.text == "" and transcript.segments == ()


async def test_every_call_runs_on_the_engine_thread(
    fake_mlx: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    threads: set[int] = set()
    step = FakeLibrarySession.step

    def recorded(self: FakeLibrarySession, *, max_decode_tokens: int) -> list[str]:
        threads.add(threading.get_ident())
        return step(self, max_decode_tokens=max_decode_tokens)

    monkeypatch.setattr(FakeLibrarySession, "step", recorded)
    engine = FakeEngine()
    async with await engine.stream("en") as session:
        busy = threading.Event()
        hold = asyncio.create_task(run_blocking(busy.wait, 5))  # grows the default pool
        for _ in range(30):
            await session.add_audio(speech_like(0.1))
        await session.finalize()
        busy.set()
        await hold
    assert len(threads) == 1


async def test_one_session_at_a_time_and_closed_sessions_refuse_audio(
    fake_mlx: list[int],
) -> None:
    engine = FakeEngine()
    session = await engine.stream("en")
    with pytest.raises(EngineError, match="session is open"):
        await engine.stream("en")
    with pytest.raises(EngineError, match="session is open"):
        await engine.transcribe(AudioInput(speech_like(0.5)))
    await session.add_audio(speech_like(1.0))
    await session.finalize()
    with pytest.raises(EngineError):
        await session.add_audio(speech_like(0.1))
    with pytest.raises(EngineError):
        await session.finalize()
    await session.close()
    await session.close()
    assert engine._session is None and engine._clearing is not None
    await engine._clearing
    assert fake_mlx == [1]
    silent = await engine.transcribe(AudioInput(np.zeros(1600, dtype=np.float32)), "en-GB")
    assert silent.text == "" and silent.language == "en" and silent.duration_s == 0.1


async def test_batch_empties_the_cache_even_when_it_fails(fake_mlx: list[int]) -> None:
    engine = FakeEngine()

    def generate(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Metal went away")

    engine._model.default_att_context_size = [70, 1]
    engine._model.generate = generate
    with pytest.raises(RuntimeError, match="Metal"):
        await engine.transcribe(AudioInput(speech_like(0.5)), "en")
    assert fake_mlx == [1]


def test_the_service_runs_one_stream_at_a_time(settings: Settings) -> None:
    from voiced.stt import build_stt_app

    settings.stt.streams = 2
    app = build_stt_app(settings, NemotronEngine())
    assert app.state.ctx.queue.max_shared == 1


# --- real model ---------------------------------------------------------------

EXPECTED_WORDS = {"kitchen", "lights", "thermostat", "degrees"}
TRAILING_WORDS = {"weather", "tomorrow", "morning"}


def assert_heard(text: str, key_words: set[str]) -> None:
    assert key_words <= set(words(text)), text


@pytest.fixture(scope="module", params=[MULTILINGUAL, ENGLISH], ids=["3.5", "en"])
def engine(request: pytest.FixtureRequest) -> Iterator[NemotronEngine]:
    pytest.importorskip("mlx_audio")
    import mlx.core as mx
    from mlx.utils import tree_flatten

    engine = create_stt_engine("nemotron", request.param)
    assert isinstance(engine, NemotronEngine)
    mx.reset_peak_memory()
    t0 = time.perf_counter()
    asyncio.run(engine.load())
    print(
        f"\n{request.param} load and warm-up: {time.perf_counter() - t0:.1f}s, "
        f"MLX active {mx.get_active_memory() / 1e9:.2f} GB, "
        f"peak {mx.get_peak_memory() / 1e9:.2f} GB"
    )
    dtypes = {p.dtype for _, p in tree_flatten(engine._model.parameters())}
    assert dtypes == {mx.bfloat16}
    # The warm-up's batch peaks at about 2.15 GB; the fp32 English weights
    # beside their bf16 copy would be over 3.7 GB.
    assert mx.get_peak_memory() < 2.5e9
    yield engine
    engine._model = None  # the next model's load peak is its own
    gc.collect()
    mx.clear_cache()


async def paced_finalize(
    engine: NemotronEngine, clip: NDArray[np.float32], piece: int
) -> tuple[float, Transcript, list[str]]:
    """Stream ``clip`` in ``piece``-sized parts at the rate a microphone
    delivers them, then finalize: the latency, the transcript, the partials."""
    partials: list[str] = []
    async with await engine.stream("en") as session:
        t0 = time.perf_counter()
        for i in range(0, len(clip), piece):
            await session.add_audio(clip[i : i + piece])
            partials.append(session.partial())
            await asyncio.sleep(max(0.0, t0 + (i + piece) / STT_SAMPLE_RATE - time.perf_counter()))
        t = time.perf_counter()
        transcript = await session.finalize()
        return time.perf_counter() - t, transcript, partials


@pytest.mark.model
async def test_batch_transcript_with_sentence_times(
    engine: NemotronEngine,
    speech: NDArray[np.float32],
    speech_with_trailing_silence: NDArray[np.float32],
) -> None:
    gap = np.zeros(STT_SAMPLE_RATE, dtype=np.float32)
    clip = np.concatenate([speech, gap, speech_with_trailing_silence])
    await engine.transcribe(AudioInput(clip), "en")  # warm
    t0 = time.perf_counter()
    transcript = await engine.transcribe(AudioInput(clip), "en")
    elapsed = time.perf_counter() - t0
    print(
        f"batch {len(clip) / STT_SAMPLE_RATE:.1f}s in {elapsed * 1000:.0f} ms: {transcript.text!r}"
    )
    for s in transcript.segments:
        print(f"  [{s.start:.2f}-{s.end:.2f}] {s.text}")
    assert_heard(transcript.text, EXPECTED_WORDS | TRAILING_WORDS)
    assert transcript.language == "en"
    assert len(transcript.segments) == 2
    first, second = transcript.segments
    # Each sentence lies within its clip: timed by its words, not its punctuation.
    boundary = (len(speech) + len(gap)) / STT_SAMPLE_RATE
    assert first.end <= len(speech) / STT_SAMPLE_RATE + 0.1
    assert boundary - 0.1 <= second.start <= boundary + 0.5
    assert second.end <= transcript.duration_s
    # ADR 0025: batch misses ADR 0008's 300 ms for 10 s (about 840 ms).
    assert elapsed < transcript.duration_s / 4


@pytest.mark.model
async def test_batch_without_a_hint(
    engine: NemotronEngine, speech_with_trailing_silence: NDArray[np.float32]
) -> None:
    transcript = await engine.transcribe(AudioInput(speech_with_trailing_silence))
    assert_heard(transcript.text, TRAILING_WORDS)
    assert "<" not in transcript.text  # 3.5's language tag is stripped
    assert transcript.language is None


@pytest.mark.model
async def test_silent_and_empty_input(engine: NemotronEngine) -> None:
    rng = np.random.default_rng(0)
    for samples in [
        np.zeros(0, dtype=np.float32),
        np.zeros(160, dtype=np.float32),
        np.zeros(STT_SAMPLE_RATE, dtype=np.float32),
        (rng.standard_normal(STT_SAMPLE_RATE) * 1e-4).astype(np.float32),
    ]:
        transcript = await engine.transcribe(AudioInput(samples))
        assert transcript.text == ""
        assert transcript.duration_s == pytest.approx(len(samples) / STT_SAMPLE_RATE)
        async with await engine.stream() as session:
            await session.add_audio(samples)
            assert (await session.finalize()).text == ""


@pytest.mark.model
@pytest.mark.parametrize("tail", ["at the last word", "after 200 ms of silence"])
@pytest.mark.parametrize("piece", [1024, 1600])  # Wyoming's 64 ms, the WebSocket's 100 ms
async def test_streamed_finalize(
    engine: NemotronEngine,
    speech: NDArray[np.float32],
    speech_with_trailing_silence: NDArray[np.float32],
    piece: int,
    tail: str,
) -> None:
    """ADR 0008: 150 ms at worst after the end of input, over end-of-input
    alignments 20 ms apart, with or without the endpointer's silence. Its
    50 ms median is a known miss for 3.5 (ADR 0025); this keeps it from
    growing."""
    # Ending 20 to 140 ms early cuts into the last word, "degrees".
    clip, heard = (
        (speech, EXPECTED_WORDS - {"degrees"}) if tail == "at the last word" else
        (speech_with_trailing_silence, TRAILING_WORDS)
    )  # fmt: skip
    latencies = []
    for k in range(8):
        latency, transcript, partials = await paced_finalize(
            engine, clip[: len(clip) - k * 320], piece
        )
        latencies.append(latency)
        assert_heard(transcript.text, heard)
        assert [(s.start, s.text) for s in transcript.segments] == [(0.0, transcript.text)]
        assert partials[-1] and transcript.text.startswith(partials[-1])
    ms = sorted(x * 1000 for x in latencies)
    print(
        f"{piece}-sample pieces, {tail}: "
        f"finalize median {statistics.median(ms):.0f}, worst {ms[-1]:.0f} ms"
    )
    assert statistics.median(ms) <= 100
    assert ms[-1] <= 150


@pytest.mark.model
def test_a_chunk_costs_the_same_late_in_a_long_utterance(
    engine: NemotronEngine, speech: NDArray[np.float32]
) -> None:
    """Cache-aware: the 30th second of an utterance encodes no more than the first."""
    clip = np.concatenate([speech] * 10)[: 31 * STT_SAMPLE_RATE]
    costs: list[tuple[float, float]] = []

    def run() -> None:
        decoder = _Decoder(engine._model, "en-US")
        start, size = 0, decoder.first
        while start + size <= len(clip):
            t = time.perf_counter()
            decoder.push(clip[start : start + size])
            costs.append((start / STT_SAMPLE_RATE, time.perf_counter() - t))
            start, size = start + size, decoder.chunk
        decoder.finish(np.zeros(0, dtype=np.float32))

    asyncio.run(engine._thread.run(run))
    at = {m: statistics.median(c for s, c in costs if m <= s < m + 1) for m in (0, 5, 10, 20, 30)}
    print("per chunk:", ", ".join(f"{m} s {c * 1000:.1f} ms" for m, c in at.items()))
    assert at[30] < at[0] * 1.5 + 0.005
    assert max(at.values()) < CHUNK_MS / 1000 / 2  # well inside real time
