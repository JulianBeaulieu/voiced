"""Soprano engine tests. The shared contract tests are in
``test_sentence_engines.py``; the ``model`` tests need the weights and Metal;
everything else runs on CI with a fake model."""

from __future__ import annotations

import logging
import statistics
import time
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from voiced.engines import get_tts_engine
from voiced.engines.base import TTS_SAMPLE_RATE, EngineError, EngineNotLoaded
from voiced.engines.soprano import (
    EXTRA_SECONDS,
    FADE_SECONDS,
    REPETITION_CONTEXT,
    SECONDS_PER_CHAR,
    SOPRANO_VOICE,
    SopranoEngine,
    penalized_stream,
    sentence_bound,
)

# -- registry, metadata --------------------------------------------------------


def test_registered_as_entry_point() -> None:
    assert get_tts_engine("soprano") is SopranoEngine
    assert SopranoEngine("local/path").model_id == "local/path"


# -- the sentence bound ----------------------------------------------------------


def test_bound_grows_with_text() -> None:
    assert sentence_bound("", 0.064).seconds == EXTRA_SECONDS
    text = "Hello there, how are you today?"
    bound = sentence_bound(text, 0.064)
    assert bound.seconds == pytest.approx(31 * SECONDS_PER_CHAR + EXTRA_SECONDS)
    # 64 ms per token is 2048 samples at 32 kHz; the cap covers the seconds.
    assert bound.tokens == 74
    assert bound.tokens * 0.064 >= bound.seconds


# -- repetition penalty, on CI with numpy ----------------------------------------

STOP = 0
FAVOURED = 7


class _RepeatingLM:
    """Stands in for the language model: whatever came before, token 7 scores
    2.0, the stop token 1.8 and everything else 0. Greedy decoding without a
    penalty repeats 7 forever; the penalty (2.0 / 1.2 < 1.8) stops it once 7
    is inside the last 64 tokens."""

    config = SimpleNamespace(num_hidden_layers=1, max_position_embeddings=512)
    tokenizer = None
    _stop_token_id = STOP

    def _forward_with_hidden_states(self, ids: Any, cache: Any) -> tuple[Any, Any]:
        logits = np.zeros((1, ids.shape[1], 16), dtype=np.float32)
        logits[:, :, FAVOURED] = 2.0
        logits[:, :, STOP] = 1.8
        return logits, np.zeros((1, ids.shape[1], 4), dtype=np.float32)


def _generated(prompt: list[int], max_tokens: int = 20, lm: Any = None) -> list[int]:
    stream = penalized_stream(
        lm or _RepeatingLM(),
        np.array(prompt),
        max_tokens,
        sample=None,
        new_cache=lambda: None,
        xp=np,
        evaluate=lambda *_: None,
    )
    return [int(token[0, 0]) for token, _ in stream if token is not None]


def test_penalty_stops_a_repeating_token() -> None:
    # Without the penalty this is [7] * 20: the runaway.
    assert _generated([3, 4, 5]) == [FAVOURED]


def test_penalty_only_looks_at_the_last_64_tokens() -> None:
    # 7 is in the prompt but 64 tokens back: not penalized, so it is generated
    # once, then penalized. Penalizing the whole history (the 2-D bug) would
    # stop at once; no penalty would never stop.
    prompt = [FAVOURED] + [1] * REPETITION_CONTEXT
    assert _generated(prompt) == [FAVOURED]
    assert _generated([FAVOURED] + [1] * (REPETITION_CONTEXT - 1)) == []


class _FreshTokenLM(_RepeatingLM):
    """Never stops: favours a token it has not produced before on every step,
    so the penalty never bites and only the context limit ends generation."""

    def __init__(self) -> None:
        self.steps = 0

    def _forward_with_hidden_states(self, ids: Any, cache: Any) -> tuple[Any, Any]:
        self.steps += 1
        logits = np.zeros((1, ids.shape[1], 1024), dtype=np.float32)
        logits[:, :, 100 + self.steps] = 2.0
        return logits, np.zeros((1, ids.shape[1], 4), dtype=np.float32)


def test_cap_leaves_room_for_the_prompt_in_the_context(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="voiced.engines.soprano"):
        assert len(_generated([1] * 400, max_tokens=100, lm=_FreshTokenLM())) == 100
        assert "stopped by the context" not in caplog.text  # the cap bound, not the context
        assert len(_generated([1] * 500, max_tokens=100, lm=_FreshTokenLM())) == 512 - 500
    assert "runaway generation stopped by the context" in caplog.text


def test_prompt_that_fills_the_context_is_a_client_error() -> None:
    with pytest.raises(EngineError, match="context"):
        _generated([1] * 512, lm=_FreshTokenLM())


# -- the engine with a fake model --------------------------------------------------


class _FakeSoprano:
    """Stands in for mlx-audio's Soprano model. ``tokens_wanted`` is how many
    tokens the fake would generate if not capped (a large number babbles);
    audio is 0.5 amplitude, 2048 samples per token like the real model, and
    ``token_count`` includes the prefill step as the real ``generate()`` does."""

    def __init__(self, tokens_wanted: int) -> None:
        self.sample_rate = TTS_SAMPLE_RATE  # keeps resample an identity: no scipy
        self.config = SimpleNamespace(decoder_config=SimpleNamespace(token_size=2048))
        self.tokens_wanted = tokens_wanted
        self.calls: list[int] = []

    def generate(
        self, *, text: str, max_tokens: int, temperature: float, top_p: float
    ) -> Iterator[Any]:
        self.calls.append(max_tokens)
        n = min(self.tokens_wanted, max_tokens)
        audio = np.full(n * 2048, 0.5, dtype=np.float32)
        yield SimpleNamespace(audio=audio, token_count=n + 1)


async def _loaded(monkeypatch: pytest.MonkeyPatch, model: _FakeSoprano) -> SopranoEngine:
    monkeypatch.setattr("voiced.engines.soprano._load_model", lambda _id: model)
    engine = SopranoEngine()
    await engine.load()
    model.calls.clear()  # drop the warm-up
    return engine


async def _pcm(engine: SopranoEngine, text: str) -> np.ndarray:
    chunks = [c async for c in engine.synthesize(text, SOPRANO_VOICE.id)]
    assert len(chunks) == 1
    return np.frombuffer(chunks[0].data, dtype="<i2").astype(np.float32) / 32767


async def test_load_warms_up_and_failed_load_stays_unloaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _FakeSoprano(tokens_wanted=20)
    monkeypatch.setattr("voiced.engines.soprano._load_model", lambda _id: model)
    engine = SopranoEngine()
    await engine.load()
    assert len(model.calls) == 1
    assert await engine.voices() == [SOPRANO_VOICE]

    def missing(_id: str) -> Any:
        raise FileNotFoundError("model.safetensors")

    monkeypatch.setattr("voiced.engines.soprano._load_model", missing)
    engine = SopranoEngine()
    with pytest.raises(FileNotFoundError):
        await engine.load()
    with pytest.raises(EngineNotLoaded):
        await engine.voices()


async def test_speed_in_bounds_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _FakeSoprano(tokens_wanted=5)
    engine = await _loaded(monkeypatch, model)
    lengths = set()
    for speed in (SopranoEngine.MIN_SPEED, 1.0, SopranoEngine.MAX_SPEED):
        chunks = [c async for c in engine.synthesize("Hi.", SOPRANO_VOICE.id, speed=speed)]
        lengths.add(len(chunks[0].data))
    assert lengths == {5 * 2048 * 2}


async def test_sentence_that_stops_on_its_own_is_untouched(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    engine = await _loaded(monkeypatch, _FakeSoprano(tokens_wanted=5))
    samples = await _pcm(engine, "Hello there, friend.")
    assert len(samples) == 5 * 2048
    assert samples[-1] == pytest.approx(0.5, abs=1e-3)
    assert "runaway" not in caplog.text


async def test_runaway_is_capped_cut_to_the_bound_and_faded(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    model = _FakeSoprano(tokens_wanted=10_000)
    engine = await _loaded(monkeypatch, model)
    sentence = "Hi."
    bound = sentence_bound(sentence, 2048 / TTS_SAMPLE_RATE)
    with caplog.at_level(logging.WARNING, logger="voiced.engines.soprano"):
        samples = await _pcm(engine, sentence)
    assert model.calls == [bound.tokens]
    assert len(samples) == int(bound.seconds * TTS_SAMPLE_RATE)
    fade = int(FADE_SECONDS * TTS_SAMPLE_RATE)
    assert samples[-1] == pytest.approx(0.0, abs=1e-3)
    assert samples[-fade - 1] == pytest.approx(0.5, abs=1e-3)
    assert "runaway generation" in caplog.text


# -- model tests ---------------------------------------------------------------------


@pytest.mark.model
def test_penalized_loop_is_installed(soprano_engine: Any) -> None:
    # Patched per instance: a plain function, not the class's bound method.
    model = soprano_engine._model
    assert getattr(model.stream_generate, "__func__", None) is not type(model).stream_generate


@pytest.mark.model
async def test_synthesizes_plausible_duration(soprano_engine: Any) -> None:
    text = "The quick brown fox jumps over the lazy dog."
    chunks = [c async for c in soprano_engine.synthesize(text, "default")]
    assert len(chunks) == 1
    assert chunks[0].sample_rate == TTS_SAMPLE_RATE
    assert 1.5 < chunks[0].duration_s <= sentence_bound(text, 0.064).seconds
    samples = np.frombuffer(chunks[0].data, dtype="<i2")
    assert np.abs(samples).max() > 1000, "audio is silent"


@pytest.mark.model
async def test_babble_prone_input_respects_the_bound(soprano_engine: Any) -> None:
    # A repetitive prompt is what makes the model run away; every chunk must
    # still end within the bound.
    text = "ha ha ha ha ha ha ha ha ha ha ha ha ha ha ha ha"
    limit = sentence_bound(text, 0.064).seconds
    for _ in range(3):
        chunks = [c async for c in soprano_engine.synthesize(text, "default")]
        assert len(chunks) == 1
        assert chunks[0].duration_s <= limit + 0.01


@pytest.mark.model
async def test_multiple_sentences_stream_in_order(soprano_engine: Any) -> None:
    chunks = [c async for c in soprano_engine.synthesize("First one. Second one.", "default")]
    assert len(chunks) == 2
    assert all(c.duration_s > 0.3 for c in chunks)


@pytest.mark.model
async def test_first_chunk_latency_when_warm(soprano_engine: Any) -> None:
    text = "Hello there, how are you today? This second sentence makes the request longer."
    async for _ in soprano_engine.synthesize("Hello again.", "default"):
        break  # one untimed call so the measurement reflects a warm engine
    latencies = []
    for _ in range(3):
        started = time.perf_counter()
        gen = soprano_engine.synthesize(text, "default")
        first = await gen.__anext__()
        latencies.append(time.perf_counter() - started)
        await gen.aclose()
        assert first.duration_s > 0.3
    median = statistics.median(latencies)
    assert median < 0.5, f"first chunk median {median * 1000:.0f} ms"
