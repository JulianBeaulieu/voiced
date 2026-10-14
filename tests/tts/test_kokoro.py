"""Kokoro engine tests. The ``model`` tests need the weights, espeak-ng and
Metal; everything else runs on CI."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from voiced.engines import get_tts_engine
from voiced.engines.base import TTS_SAMPLE_RATE, EngineNotLoaded, UnknownVoice
from voiced.engines.kokoro import (
    ENV_ESPEAK_DATA,
    ENV_ESPEAK_LIBRARY,
    ESPEAK_INSTALL_HINT,
    KOKORO_LANGUAGES,
    KOKORO_VOICE_IDS,
    MAX_PHONEMES,
    EspeakNotFound,
    KokoroEngine,
    _chunk_phonemes,
    find_espeak,
    kokoro_voices,
)
from voiced.tts import normalize

# -- registry and metadata ---------------------------------------------------


def test_registered_as_entry_point() -> None:
    assert get_tts_engine("kokoro") is KokoroEngine


def test_metadata() -> None:
    engine = KokoroEngine()
    assert engine.name == "kokoro"
    assert engine.model_id == "mlx-community/Kokoro-82M-bf16"
    assert engine.languages == ["en-US", "en-GB"]
    assert engine.license == "Apache-2.0"
    assert KokoroEngine("local/path").model_id == "local/path"


# -- voice table --------------------------------------------------------------


def test_voice_table_covers_all_languages() -> None:
    prefixes = {v[0] for v in KOKORO_VOICE_IDS}
    assert prefixes == set(KOKORO_LANGUAGES)
    assert len(KOKORO_VOICE_IDS) == 54
    assert len(set(KOKORO_VOICE_IDS)) == 54


def test_english_voices_only_by_default() -> None:
    voices = kokoro_voices(KokoroEngine.languages)
    ids = [v.id for v in voices]
    assert len(ids) == 28
    assert all(v[:2] in {"af", "am", "bf", "bm"} for v in ids)
    assert "af_heart" in ids
    by_id = {v.id: v for v in voices}
    assert by_id["af_heart"].language == "en-US"
    assert by_id["af_heart"].gender == "female"
    assert by_id["af_heart"].name == "Heart"
    assert by_id["bm_george"].language == "en-GB"
    assert by_id["bm_george"].gender == "male"


def test_other_languages_can_be_enabled_from_the_table() -> None:
    voices = kokoro_voices(["en-US", "es-ES"])
    assert {v.language for v in voices} == {"en-US", "es-ES"}
    assert any(v.id == "ef_dora" for v in voices)


# -- espeak discovery --------------------------------------------------------


def test_find_espeak_prefers_explicit_then_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    explicit = tmp_path / "explicit.dylib"
    from_env = tmp_path / "env.dylib"
    explicit.touch()
    from_env.touch()
    monkeypatch.setenv(ENV_ESPEAK_LIBRARY, str(from_env))
    monkeypatch.setenv(ENV_ESPEAK_DATA, str(tmp_path / "data"))
    found = find_espeak(str(explicit), None)
    assert found[0] == (str(explicit), None)
    assert found[1] == (str(from_env), str(tmp_path / "data"))


def test_find_espeak_skips_missing_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_ESPEAK_LIBRARY, str(tmp_path / "nope.dylib"))
    assert all(Path(lib).is_file() for lib, _ in find_espeak())


async def test_load_without_espeak_names_the_brew_command(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("kokoro_onnx")
    monkeypatch.setattr("voiced.engines.kokoro.find_espeak", lambda *a, **k: [])
    engine = KokoroEngine()
    with pytest.raises(EspeakNotFound, match=ESPEAK_INSTALL_HINT):
        await engine.load()


async def test_load_with_broken_espeak_names_the_brew_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("kokoro_onnx")
    bogus = tmp_path / "libespeak-ng.dylib"
    bogus.write_bytes(b"not a library")
    monkeypatch.setattr("voiced.engines.kokoro.find_espeak", lambda *a, **k: [(str(bogus), None)])
    with pytest.raises(EspeakNotFound, match=ESPEAK_INSTALL_HINT):
        await KokoroEngine().load()


# -- contract before load ---------------------------------------------------


async def test_methods_require_load() -> None:
    engine = KokoroEngine()
    with pytest.raises(EngineNotLoaded):
        await engine.voices()
    with pytest.raises(EngineNotLoaded):
        async for _ in engine.synthesize("Hi.", "af_heart"):
            pass


def test_chunk_phonemes() -> None:
    assert list(_chunk_phonemes("", 10)) == []
    assert list(_chunk_phonemes("abc def", 10)) == ["abc def"]
    assert list(_chunk_phonemes("abc def ghi", 7)) == ["abc def", "ghi"]
    assert list(_chunk_phonemes("abcdefghij", 4)) == ["abcd", "efgh", "ij"]
    long = " ".join(["ab"] * 300)
    assert all(len(c) <= MAX_PHONEMES for c in _chunk_phonemes(long, MAX_PHONEMES))


# -- model tests --------------------------------------------------------------

pytestmark_model = pytest.mark.model


def _pcm_seconds(chunks: list[Any]) -> float:
    return sum(c.duration_s for c in chunks)


@pytest.mark.model
async def test_synthesizes_plausible_duration(kokoro_engine: Any) -> None:
    text = "The quick brown fox jumps over the lazy dog."
    chunks = [c async for c in kokoro_engine.synthesize(text, "af_heart")]
    assert len(chunks) == 1
    assert chunks[0].sample_rate == TTS_SAMPLE_RATE
    seconds = _pcm_seconds(chunks)
    assert 1.5 < seconds < 5.0, seconds
    samples = np.frombuffer(chunks[0].data, dtype="<i2")
    assert np.abs(samples).max() > 1000, "audio is silent"


@pytest.mark.model
async def test_one_chunk_per_sentence_and_speed(kokoro_engine: Any) -> None:
    text = "First sentence here. Second one follows! Third, a question?"
    normal = [c async for c in kokoro_engine.synthesize(text, "af_heart")]
    fast = [c async for c in kokoro_engine.synthesize(text, "af_heart", speed=1.5)]
    assert len(normal) == 3
    assert len(fast) == 3
    assert _pcm_seconds(fast) < 0.85 * _pcm_seconds(normal)


@pytest.mark.model
async def test_first_chunk_under_200ms_when_warm(kokoro_engine: Any) -> None:
    text = (
        "Hello there, how are you today? This second sentence is here to make the request longer."
    )
    # One untimed call so the measurement reflects a warm engine, as in service.
    async for _ in kokoro_engine.synthesize("Hello again.", "af_heart"):
        break
    best = float("inf")
    for _ in range(3):
        started = time.perf_counter()
        gen = kokoro_engine.synthesize(text, "af_heart")
        first = await gen.__anext__()
        best = min(best, time.perf_counter() - started)
        await gen.aclose()
        assert first.duration_s > 0.5
    assert best < 0.2, f"first chunk took {best * 1000:.0f} ms"


@pytest.mark.model
async def test_british_voice_uses_en_gb(kokoro_engine: Any) -> None:
    us = kokoro_engine.phonemize("Hello world.", "af_heart")
    gb = kokoro_engine.phonemize("Hello world.", "bm_george")
    assert us != gb
    chunks = [c async for c in kokoro_engine.synthesize("Hello world.", "bm_george")]
    assert _pcm_seconds(chunks) > 0.5


@pytest.mark.model
def test_co2_is_pronounced_see_oh_two(kokoro_engine: Any) -> None:
    """The reason for the espeak path: misaki read CO2 as "co two"."""
    spelled = kokoro_engine.phonemize(normalize("CO2"), "af_heart")
    spoken = kokoro_engine.phonemize(normalize("see oh two"), "af_heart")
    assert spelled == spoken
    assert kokoro_engine.phonemize("CO2", "af_heart") != spoken


@pytest.mark.model
async def test_unknown_voice_raises_before_yielding(kokoro_engine: Any) -> None:
    gen = kokoro_engine.synthesize("Hi.", "xx_nobody")
    with pytest.raises(UnknownVoice):
        await gen.__anext__()


@pytest.mark.model
async def test_aclose_stops_early(kokoro_engine: Any) -> None:
    text = " ".join(f"Sentence number {i}." for i in range(20))
    gen = kokoro_engine.synthesize(text, "af_heart")
    await gen.__anext__()
    await gen.aclose()
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()


@pytest.mark.model
async def test_voices_after_load(kokoro_engine: Any) -> None:
    voices = await kokoro_engine.voices()
    assert len(voices) == 28
    assert {v.language for v in voices} == {"en-US", "en-GB"}


@pytest.mark.model
def test_resident_memory_is_reported() -> None:
    """Not an assertion on a number: prints the figure for the PR."""
    import resource

    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2**20
    print(f"\nmax RSS in this test process: {rss_mb:.0f} MB")
    assert os.environ.get("VOICED_MODEL_TESTS") == "1"
