"""Contract tests for every ``SentenceTTSEngine``, without a model.

Each engine is made to look loaded with a fake synthesizer, so these run on
CI. Engine-specific behaviour lives in ``test_<engine>.py``."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from voiced.engines import SentenceTTSEngine
from voiced.engines.base import TTS_SAMPLE_RATE, EngineError, EngineNotLoaded, UnknownVoice
from voiced.engines.kokoro import KokoroEngine
from voiced.queue import InferenceQueue

# (engine class, a voice id it serves)
ENGINES: list[tuple[type[SentenceTTSEngine], str]] = [
    (KokoroEngine, "af_heart"),
]


@pytest.fixture(params=ENGINES, ids=lambda e: e[0].name)
def engine_and_voice(request: pytest.FixtureRequest) -> tuple[SentenceTTSEngine, str]:
    cls, voice = request.param
    return cls(), voice


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
    return calls


async def test_methods_require_load(engine_and_voice: tuple[SentenceTTSEngine, str]) -> None:
    engine, voice = engine_and_voice
    with pytest.raises(EngineNotLoaded):
        await engine.voices()
    with pytest.raises(EngineNotLoaded):
        async for _ in engine.synthesize("Hi.", voice):
            pass


async def test_failed_load_leaves_engine_unloaded(
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
