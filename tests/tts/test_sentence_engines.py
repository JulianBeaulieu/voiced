"""Contract tests for every ``SentenceTTSEngine``, without a model.

Each engine is made to look loaded with a fake synthesizer, so these run on
CI. Engine-specific behaviour lives in ``test_<engine>.py``."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Generator

import pytest

from voiced.engines.base import (
    TTS_SAMPLE_RATE,
    EngineError,
    EngineNotLoaded,
    UnknownVoice,
    Voice,
)
from voiced.engines.kokoro import KokoroEngine
from voiced.engines.phonon import PhononEngine
from voiced.engines.pocket import PocketEngine
from voiced.engines.sentence import SentenceTTSEngine
from voiced.engines.soprano import SopranoEngine
from voiced.queue import InferenceQueue

ENGINES: list[type[SentenceTTSEngine]] = [KokoroEngine, SopranoEngine, PhononEngine, PocketEngine]
VOICE = "test_voice"
"""The one voice the stubbed engines accept."""

# A multi-sentence reply, used by the model-tier cache test below.
LONG_REPLY = (
    "Turn on the kitchen lights and set the thermostat to twenty degrees. "
    "What is the weather like tomorrow morning in the city? Please play some relaxing "
    "music in the living room and dim the bedroom lights to about fifty percent. "
    "Also lock the front door and arm the alarm before everyone goes to sleep tonight."
)


@pytest.fixture(params=ENGINES, ids=lambda cls: cls.name)
def engine_and_voice(request: pytest.FixtureRequest) -> tuple[SentenceTTSEngine, str]:
    return request.param(), VOICE


def _stub_loaded(
    engine: SentenceTTSEngine, voice: str, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[str, float]]:
    """Make ``engine`` look loaded with a fake synthesizer that records calls."""
    calls: list[tuple[str, float]] = []

    def fake_synth(sentence: str, voice: str, speed: float) -> bytes:
        calls.append((sentence, speed))
        return bytes(480)

    engine._model = object()
    monkeypatch.setattr(engine, "_has_voice", lambda v: v == voice)
    monkeypatch.setattr(engine, "_synth_sentence", fake_synth)

    # One piece per sentence, through whatever _synth_sentence a test patches
    # in, for engines that stream within a sentence too.
    def pieces(sentence: str, voice: str, speed: float) -> Generator[bytes, None, None]:
        yield engine._synth_sentence(sentence, voice, speed)

    monkeypatch.setattr(engine, "_synth_chunks", pieces)
    return calls


async def test_methods_require_load(engine_and_voice: tuple[SentenceTTSEngine, str]) -> None:
    engine, voice = engine_and_voice
    with pytest.raises(EngineNotLoaded):
        await engine.voices()
    with pytest.raises(EngineNotLoaded):
        async for _ in engine.synthesize("Hi.", voice):
            pass


async def test_load_propagates_errors(
    engine_and_voice: tuple[SentenceTTSEngine, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _ = engine_and_voice

    def boom() -> None:
        raise RuntimeError("weights missing")

    monkeypatch.setattr(engine, "_load_sync", boom)
    with pytest.raises(RuntimeError, match="weights missing"):
        await engine.load()
    with pytest.raises(EngineNotLoaded):
        await engine.voices()


async def test_load_runs_off_the_event_loop(
    engine_and_voice: tuple[SentenceTTSEngine, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, _ = engine_and_voice
    threads: list[int] = []

    def fake_load() -> None:
        threads.append(threading.get_ident())
        engine._model = object()

    monkeypatch.setattr(engine, "_load_sync", fake_load)
    await engine.load()
    assert threads and threads[0] != threading.get_ident()


async def test_the_load_and_every_step_run_on_one_model_thread(
    engine_and_voice: tuple[SentenceTTSEngine, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """MLX arrays an engine keeps between pieces of a sentence cannot be
    evaluated on another thread, so the load, each piece and each close run
    on the engine's one thread, never the event loop's."""
    engine, voice = engine_and_voice
    threads: list[tuple[str, int]] = []

    def fake_load() -> None:
        threads.append(("load", threading.get_ident()))
        engine._model = object()

    def pieces(sentence: str, voice: str, speed: float) -> Generator[bytes, None, None]:
        try:
            for _ in range(2):
                threads.append(("piece", threading.get_ident()))
                yield bytes(480)
        finally:
            threads.append(("close", threading.get_ident()))

    monkeypatch.setattr(engine, "_load_sync", fake_load)
    monkeypatch.setattr(engine, "_has_voice", lambda v: v == voice)
    monkeypatch.setattr(engine, "_synth_chunks", pieces)
    await engine.load()
    # A busy default pool: the shared executor would hand out other threads.
    await asyncio.gather(*(asyncio.to_thread(time.sleep, 0.01) for _ in range(8)))
    for _ in range(3):
        [c async for c in engine.synthesize("One. Two.", voice)]
    gen = engine.synthesize("One. Two.", voice)
    await gen.__anext__()
    await gen.aclose()
    assert {kind for kind, _ in threads} == {"load", "piece", "close"}
    assert len({ident for _, ident in threads}) == 1
    assert threads[0][1] != threading.get_ident()


async def test_unknown_voice_raises_before_yielding(
    engine_and_voice: tuple[SentenceTTSEngine, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, voice = engine_and_voice
    calls = _stub_loaded(engine, voice, monkeypatch)
    gen = engine.synthesize("Hi.", "xx_nobody")
    with pytest.raises(UnknownVoice):
        await gen.__anext__()
    assert calls == []


def _out_of_range(cls: type[SentenceTTSEngine]) -> list[float]:
    return [0.0, -1.0, cls.MIN_SPEED / 2, cls.MAX_SPEED * 2, float("nan")]


async def test_speed_out_of_range_is_rejected(
    engine_and_voice: tuple[SentenceTTSEngine, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, voice = engine_and_voice
    calls = _stub_loaded(engine, voice, monkeypatch)
    for speed in _out_of_range(type(engine)):
        gen = engine.synthesize("Hi.", voice, speed=speed)
        with pytest.raises(EngineError, match="speed"):
            await gen.__anext__()
    assert calls == []


async def test_speed_in_range_is_accepted(
    engine_and_voice: tuple[SentenceTTSEngine, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, voice = engine_and_voice
    calls = _stub_loaded(engine, voice, monkeypatch)
    speeds = sorted({engine.MIN_SPEED, 1.0, engine.MAX_SPEED})
    for speed in speeds:
        chunks = [c async for c in engine.synthesize("Hi.", voice, speed=speed)]
        assert len(chunks) == 1
    assert calls == [("Hi.", s) for s in speeds]


async def test_one_chunk_per_sentence_and_text_is_not_renormalized(
    engine_and_voice: tuple[SentenceTTSEngine, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, voice = engine_and_voice
    calls = _stub_loaded(engine, voice, monkeypatch)
    chunks = [c async for c in engine.synthesize("First one. Then CO2 at 5.", voice)]
    assert len(chunks) == 2
    assert [s for s, _ in calls] == ["First one.", "Then CO2 at 5."]
    assert all(c.sample_rate == TTS_SAMPLE_RATE for c in chunks)


async def test_aclose_stops_synthesis(
    engine_and_voice: tuple[SentenceTTSEngine, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, voice = engine_and_voice
    calls = _stub_loaded(engine, voice, monkeypatch)
    gen = engine.synthesize("One. Two. Three.", voice)
    await gen.__anext__()
    await gen.aclose()
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()
    assert [s for s, _ in calls] == ["One."]


async def test_synthesis_runs_off_the_event_loop(
    engine_and_voice: tuple[SentenceTTSEngine, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, voice = engine_and_voice
    threads: list[int] = []

    def fake_synth(sentence: str, voice: str, speed: float) -> bytes:
        threads.append(threading.get_ident())
        return bytes(2)

    _stub_loaded(engine, voice, monkeypatch)
    monkeypatch.setattr(engine, "_synth_sentence", fake_synth)
    async for _ in engine.synthesize("A. B.", voice):
        pass
    assert threads and all(t != threading.get_ident() for t in threads)


class _Overlap:
    """Counts concurrent model calls through a slow fake synthesizer that
    signals once it is inside a call, so the test never races on wall clock."""

    def __init__(self) -> None:
        self.active = 0
        self.peak = 0
        self.lock = threading.Lock()
        self.entered = threading.Event()

    def synth(self, sentence: str, voice: str, speed: float) -> bytes:
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        self.entered.set()
        time.sleep(0.15)  # stay in the call while the test cancels and retries
        with self.lock:
            self.active -= 1
        return bytes(2)


async def test_cancelled_request_never_overlaps_the_next_model_call(
    engine_and_voice: tuple[SentenceTTSEngine, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client disconnect cancels the awaiting task while a model call is in
    flight. ``run_blocking`` keeps the inference slot until the thread returns,
    so the next request cannot start a second concurrent model call."""
    engine, voice = engine_and_voice
    _stub_loaded(engine, voice, monkeypatch)
    overlap = _Overlap()
    monkeypatch.setattr(engine, "_synth_sentence", overlap.synth)
    queue = InferenceQueue(capacity=2)

    async def request(text: str) -> None:
        async with queue.slot():
            async for _ in engine.synthesize(text, voice):
                pass

    first = asyncio.create_task(request("One. Two."))
    await asyncio.to_thread(overlap.entered.wait, 2.0)  # first is inside its model call
    first.cancel()
    second = asyncio.create_task(request("Three."))
    with pytest.raises(asyncio.CancelledError):
        await first
    await second
    assert overlap.peak == 1


async def test_load_bounds_the_mlx_cache_even_when_it_fails(
    engine_and_voice: tuple[SentenceTTSEngine, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cache limit is set before every load (so the warm-up is bounded
    too) and the cache is dropped after it, successful or not."""
    import voiced.engines.sentence as sentence_module

    engine, _ = engine_and_voice
    events: list[str] = []
    monkeypatch.setattr(sentence_module, "_limit_mlx_cache", lambda: events.append("limit"))
    monkeypatch.setattr(sentence_module, "_clear_mlx_cache", lambda: events.append("clear"))
    monkeypatch.setattr(engine, "_load_sync", lambda: events.append("load"))
    await engine.load()
    assert events == ["limit", "load", "clear"]

    def boom() -> None:
        raise RuntimeError("no weights")

    events.clear()
    monkeypatch.setattr(engine, "_load_sync", boom)
    with pytest.raises(RuntimeError):
        await engine.load()
    assert events == ["limit", "clear"]


class _Pieces(SentenceTTSEngine):
    """Streams each sentence as three pieces, recording each step and the
    thread it ran on."""

    name = "pieces"
    model_id = "none"
    languages = ["en"]
    license = "MIT"

    def __init__(self) -> None:
        super().__init__()
        self._model = object()
        self.events: list[str] = []
        self.threads: set[int] = set()

    def _load_sync(self) -> None:
        pass

    async def voices(self) -> list[Voice]:
        return []

    def _has_voice(self, voice: str) -> bool:
        return voice == VOICE

    def _synth_sentence(self, sentence: str, voice: str, speed: float) -> bytes:
        raise AssertionError("a streaming engine is read piece by piece")

    def _synth_chunks(
        self, sentence: str, voice: str, speed: float
    ) -> Generator[bytes, None, None]:
        try:
            for i in range(3):
                self.threads.add(threading.get_ident())
                self.events.append(f"{sentence} {i}")
                yield bytes([i + 1]) * 2
        finally:
            self.threads.add(threading.get_ident())
            self.events.append(f"{sentence} closed")


async def test_a_streaming_engine_yields_each_piece_as_it_comes() -> None:
    engine = _Pieces()
    gen = engine.synthesize("One. Two.", VOICE)
    first = await gen.__anext__()
    # Nothing past the first piece was asked for before it went out.
    assert engine.events == ["One. 0"]
    rest = [c async for c in gen]
    assert [c.data for c in [first, *rest]] == [bytes([i]) * 2 for i in (1, 2, 3, 1, 2, 3)]
    assert engine.events == [
        *(f"One. {i}" for i in range(3)),
        "One. closed",
        *(f"Two. {i}" for i in range(3)),
        "Two. closed",
    ]
    assert threading.get_ident() not in engine.threads


async def test_aclose_mid_sentence_closes_the_engine_stream_off_the_loop() -> None:
    engine = _Pieces()
    gen = engine.synthesize("One. Two.", VOICE)
    await gen.__anext__()
    await gen.aclose()
    assert engine.events == ["One. 0", "One. closed"]
    assert threading.get_ident() not in engine.threads


@pytest.mark.model
@pytest.mark.parametrize(
    "fixture, voice", [("kokoro_engine", "af_heart"), ("soprano_engine", "default")]
)
def test_synthesis_keeps_the_mlx_cache_bounded(
    fixture: str, voice: str, request: pytest.FixtureRequest
) -> None:
    """Without a bound, MLX's cache of freed buffers grows with every reply
    (Kokoro held 10.2 GB after the reply below). With the limit load() sets,
    it stays near the limit after a long synthesis."""
    import mlx.core as mx

    from voiced.engines.sentence import MLX_CACHE_LIMIT_BYTES
    from voiced.text import normalize

    # Synchronous test: the session-scoped engine fixtures load with asyncio.run.
    engine = request.getfixturevalue(fixture)
    # set_cache_limit returns the previous limit: the one load() set.
    assert mx.set_cache_limit(MLX_CACHE_LIMIT_BYTES) == MLX_CACHE_LIMIT_BYTES

    async def drain() -> None:
        async for _ in engine.synthesize(normalize(LONG_REPLY, language="en"), voice, 1.0):
            pass

    asyncio.run(drain())
    cache = mx.get_cache_memory()
    # The limit is enforced on the next allocation, so the cache can end a
    # little over it; unbounded, it is many times over.
    assert cache <= MLX_CACHE_LIMIT_BYTES * 1.25, (
        f"{fixture}: MLX cache held {cache / 1e9:.2f} GB after a long synthesis"
    )
