from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from voiced.engines import get_tts_engine
from voiced.engines.base import TTS_SAMPLE_RATE, EngineNotLoaded, UnknownVoice
from voiced.engines.soprano import (
    EXTRA_SECONDS,
    SECONDS_PER_CHAR,
    SOPRANO_VOICE,
    SopranoEngine,
    max_seconds_for,
)
from voiced.tts import normalize


def test_registered_as_entry_point() -> None:
    assert get_tts_engine("soprano") is SopranoEngine


def test_metadata() -> None:
    engine = SopranoEngine()
    assert engine.name == "soprano"
    assert engine.model_id == "mlx-community/Soprano-80M-bf16"
    assert engine.languages == ["en-US"]
    assert engine.license == "Apache-2.0"


def test_duration_bound_grows_with_text() -> None:
    assert max_seconds_for("") == EXTRA_SECONDS
    assert max_seconds_for("Hello there, how are you today?") == pytest.approx(
        31 * SECONDS_PER_CHAR + EXTRA_SECONDS
    )
    assert max_seconds_for("x" * 100, seconds_per_char=0.1) == pytest.approx(11.0)


def test_token_cap_derives_from_duration_bound() -> None:
    engine = SopranoEngine()
    engine._token_seconds = 0.064  # 2048 samples at 32 kHz
    assert engine.token_cap("") == 16
    assert engine.token_cap("Hello there, how are you today?") == 74


async def test_methods_require_load() -> None:
    engine = SopranoEngine()
    with pytest.raises(EngineNotLoaded):
        await engine.voices()
    with pytest.raises(EngineNotLoaded):
        async for _ in engine.synthesize("Hi.", SOPRANO_VOICE.id):
            pass


# -- model tests --------------------------------------------------------------


@pytest.mark.model
async def test_voices_and_unknown_voice(soprano_engine: Any) -> None:
    assert await soprano_engine.voices() == [SOPRANO_VOICE]
    gen = soprano_engine.synthesize("Hi.", "af_heart")
    with pytest.raises(UnknownVoice):
        await gen.__anext__()


@pytest.mark.model
async def test_synthesizes_plausible_duration(soprano_engine: Any) -> None:
    text = "The quick brown fox jumps over the lazy dog."
    chunks = [c async for c in soprano_engine.synthesize(text, "default")]
    assert len(chunks) == 1
    assert chunks[0].sample_rate == TTS_SAMPLE_RATE
    assert 1.5 < chunks[0].duration_s <= max_seconds_for(text)
    samples = np.frombuffer(chunks[0].data, dtype="<i2")
    assert np.abs(samples).max() > 1000


@pytest.mark.model
async def test_babble_prone_input_respects_duration_cap(soprano_engine: Any) -> None:
    text = "ha ha ha ha ha ha ha ha ha ha ha ha ha ha ha ha"
    limit = max_seconds_for(normalize(text))
    for _ in range(3):
        chunks = [c async for c in soprano_engine.synthesize(text, "default")]
        assert len(chunks) == 1
        assert chunks[0].duration_s <= limit + 0.01


@pytest.mark.model
async def test_multiple_sentences_stream_in_order(soprano_engine: Any) -> None:
    text = "First one. Second one. Third one."
    chunks = [c async for c in soprano_engine.synthesize(text, "default")]
    assert len(chunks) == 3
    assert all(c.duration_s > 0.3 for c in chunks)
