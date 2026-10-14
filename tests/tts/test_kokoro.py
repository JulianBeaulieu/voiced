"""Kokoro engine tests. The ``model`` tests need the weights, espeak-ng and
Metal; everything else runs on CI."""

from __future__ import annotations

import statistics
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from voiced.engines import get_tts_engine
from voiced.engines.base import EngineNotLoaded
from voiced.engines.kokoro import (
    ENV_ESPEAK_DATA,
    ENV_ESPEAK_LIBRARY,
    ESPEAK_INSTALL_HINT,
    HOMEBREW_PREFIXES,
    EspeakNotFound,
    KokoroEngine,
    espeak_candidates,
    kokoro_voices,
)
from voiced.text import normalize

# -- registry, metadata, voice table -----------------------------------------


def test_registered_as_entry_point() -> None:
    assert get_tts_engine("kokoro") is KokoroEngine
    assert KokoroEngine("local/path").model_id == "local/path"


def test_english_voices_only_by_default() -> None:
    voices = kokoro_voices(KokoroEngine.languages)
    assert len(voices) == 28
    by_id = {v.id: v for v in voices}
    assert all(v[:2] in {"af", "am", "bf", "bm"} for v in by_id)
    assert by_id["af_heart"].language == "en-US"
    assert by_id["af_heart"].gender == "female"
    assert by_id["af_heart"].name == "Heart"
    assert by_id["bm_george"].language == "en-GB"
    assert by_id["bm_george"].gender == "male"


def test_other_languages_can_be_enabled_from_the_table() -> None:
    voices = kokoro_voices(["en-US", "es-ES"])
    assert {v.language for v in voices} == {"en-US", "es-ES"}
    assert any(v.id == "ef_dora" for v in voices)
    assert kokoro_voices([]) == []


# -- espeak discovery --------------------------------------------------------


def test_override_is_the_only_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lib = tmp_path / "libespeak-ng.dylib"
    lib.touch()
    data = tmp_path / "espeak-ng-data"
    data.mkdir()
    monkeypatch.setenv(ENV_ESPEAK_LIBRARY, str(lib))
    monkeypatch.setenv(ENV_ESPEAK_DATA, str(data))
    assert espeak_candidates() == [(str(lib), str(data))]
    monkeypatch.delenv(ENV_ESPEAK_DATA)
    assert espeak_candidates() == [(str(lib), None)]


def test_missing_override_is_an_error_naming_the_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "nope.dylib"
    monkeypatch.setenv(ENV_ESPEAK_LIBRARY, str(missing))
    with pytest.raises(EspeakNotFound, match=str(missing)):
        espeak_candidates()
    lib = tmp_path / "lib.dylib"
    lib.touch()
    monkeypatch.setenv(ENV_ESPEAK_LIBRARY, str(lib))
    monkeypatch.setenv(ENV_ESPEAK_DATA, str(tmp_path / "no-data"))
    with pytest.raises(EspeakNotFound, match="no-data"):
        espeak_candidates()


def test_homebrew_candidates_only_when_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_ESPEAK_LIBRARY, raising=False)
    monkeypatch.setattr("voiced.engines.kokoro.HOMEBREW_PREFIXES", ("/nonexistent/prefix",))
    assert espeak_candidates() == []
    installed = [p for p in HOMEBREW_PREFIXES if Path(f"{p}/lib/libespeak-ng.dylib").is_file()]
    monkeypatch.setattr("voiced.engines.kokoro.HOMEBREW_PREFIXES", HOMEBREW_PREFIXES)
    assert [lib for lib, _ in espeak_candidates()] == [
        f"{p}/lib/libespeak-ng.dylib" for p in installed
    ]


async def test_load_without_espeak_names_the_brew_command(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("kokoro_onnx")
    monkeypatch.setattr("voiced.engines.kokoro.espeak_candidates", lambda: [])
    engine = KokoroEngine()
    with pytest.raises(EspeakNotFound, match=ESPEAK_INSTALL_HINT):
        await engine.load()
    with pytest.raises(EngineNotLoaded):
        await engine.voices()


async def test_load_with_broken_espeak_names_the_brew_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("kokoro_onnx")
    bogus = tmp_path / "libespeak-ng.dylib"
    bogus.write_bytes(b"not a library")
    monkeypatch.setattr("voiced.engines.kokoro.espeak_candidates", lambda: [(str(bogus), None)])
    with pytest.raises(EspeakNotFound, match=ESPEAK_INSTALL_HINT):
        await KokoroEngine().load()


# -- Kokoro-specific contract (shared ones: test_sentence_engines.py) ---------


def test_phonemize_requires_load() -> None:
    with pytest.raises(EngineNotLoaded):
        KokoroEngine().phonemize("Hi.", "af_heart")


def test_has_voice_reads_the_loaded_packs() -> None:
    engine = KokoroEngine()
    assert not engine._has_voice("af_heart")
    engine._packs = {"af_heart": object()}
    assert engine._has_voice("af_heart")
    assert not engine._has_voice("xx_nobody")


# -- model tests --------------------------------------------------------------


def _pcm_seconds(chunks: list[Any]) -> float:
    return sum(c.duration_s for c in chunks)


@pytest.mark.model
async def test_synthesizes_plausible_duration(kokoro_engine: Any) -> None:
    text = "The quick brown fox jumps over the lazy dog."
    chunks = [c async for c in kokoro_engine.synthesize(text, "af_heart")]
    assert len(chunks) == 1
    seconds = _pcm_seconds(chunks)
    assert 1.5 < seconds < 5.0, seconds
    samples = np.frombuffer(chunks[0].data, dtype="<i2")
    assert np.abs(samples).max() > 1000, "audio is silent"


@pytest.mark.model
async def test_speed_shortens_audio(kokoro_engine: Any) -> None:
    text = "First sentence here. Second one follows! Third, a question?"
    normal = [c async for c in kokoro_engine.synthesize(text, "af_heart")]
    fast = [c async for c in kokoro_engine.synthesize(text, "af_heart", speed=1.5)]
    assert len(normal) == len(fast) == 3
    assert _pcm_seconds(fast) < 0.85 * _pcm_seconds(normal)


@pytest.mark.model
async def test_first_chunk_under_200ms_when_warm(kokoro_engine: Any) -> None:
    text = (
        "Hello there, how are you today? This second sentence is here to make the request longer."
    )
    # One untimed call so the measurement reflects a warm engine, as in service.
    async for _ in kokoro_engine.synthesize("Hello again.", "af_heart"):
        break
    latencies = []
    for _ in range(5):
        started = time.perf_counter()
        gen = kokoro_engine.synthesize(text, "af_heart")
        first = await gen.__anext__()
        latencies.append(time.perf_counter() - started)
        await gen.aclose()
        assert first.duration_s > 0.5
    median = statistics.median(latencies)
    assert median < 0.2, f"first chunk median {median * 1000:.0f} ms"


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
async def test_voices_after_load(kokoro_engine: Any) -> None:
    voices = await kokoro_engine.voices()
    assert len(voices) == 28
    assert {v.language for v in voices} == {"en-US", "en-GB"}


@pytest.mark.model
async def test_long_sentence_is_chunked_within_context(kokoro_engine: Any) -> None:
    # 300 characters of acronyms produce far more than 510 phonemes.
    text = normalize(" ".join(["HVAC"] * 60))
    chunks = [c async for c in kokoro_engine.synthesize(text, "af_heart")]
    assert _pcm_seconds(chunks) > 10
