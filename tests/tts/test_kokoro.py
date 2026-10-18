"""Kokoro engine tests. The ``model`` tests need the weights, espeak-ng and
Metal; everything else runs on CI."""

from __future__ import annotations

import os
import statistics
import subprocess
import sys
import textwrap
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
    MAX_ESPEAK_DATA_PATH,
    EspeakNotFound,
    KokoroEngine,
    _espeak_data,
    _short_espeak_copy,
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
    monkeypatch.setattr("voiced.engines.kokoro._user_cache_dir", lambda: tmp_path / "cache")
    with pytest.raises(EspeakNotFound, match=ESPEAK_INSTALL_HINT):
        await KokoroEngine().load()


# -- espeak-ng data path -------------------------------------------------------


def _long_dir(root: Path, size: int) -> Path:
    """A real (not symlinked) directory under ``root`` whose resolved path is
    at least ``size`` bytes."""
    path = Path(os.path.realpath(root))
    while len(str(path).encode()) < size:
        path = path / ("d" * 40)
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def bundled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake espeakng-loader wheel's data at a path longer than espeak-ng
    accepts, and a short user cache directory."""
    data = _long_dir(tmp_path / "venv", MAX_ESPEAK_DATA_PATH + 10) / "espeak-ng-data"
    (data / "voices").mkdir(parents=True)
    (data / "phontab").write_bytes(b"phonemes")
    monkeypatch.setattr("voiced.engines.kokoro._bundled_espeak", lambda: (str(data), "1.0"))
    monkeypatch.setattr("voiced.engines.kokoro._user_cache_dir", lambda: tmp_path / "cache")
    return data


def test_bundled_data_is_cloned_to_the_user_cache(bundled: Path, tmp_path: Path) -> None:
    expected = tmp_path / "cache" / "voiced" / "espeak-ng-data-1.0"
    assert _espeak_data(None) == str(expected)
    assert (expected / "phontab").read_bytes() == b"phonemes"
    assert (expected / "voices").is_dir()
    assert os.listdir(tmp_path / "cache" / "voiced") == ["espeak-ng-data-1.0"]


def test_clone_is_reused_and_never_replaced(bundled: Path, tmp_path: Path) -> None:
    first = Path(_short_espeak_copy(str(bundled), "1.0"))
    (first / "marker").touch()
    assert _short_espeak_copy(str(bundled), "1.0") == str(first)
    assert (first / "marker").exists()
    # A wheel upgrade gets its own directory; the old one stays for running services.
    (bundled / "phontab").write_bytes(b"upgraded")
    second = Path(_short_espeak_copy(str(bundled), "1.1"))
    assert second != first
    assert (second / "phontab").read_bytes() == b"upgraded"
    assert (first / "marker").exists()


def test_purged_clone_is_made_again(bundled: Path) -> None:
    target = Path(_short_espeak_copy(str(bundled), "1.0"))
    (target / "phontab").unlink()  # the system purged part of the cache
    assert _short_espeak_copy(str(bundled), "1.0") == str(target)
    assert (target / "phontab").read_bytes() == b"phonemes"


def test_concurrent_clones_all_succeed(bundled: Path, tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(8) as pool:
        paths = list(pool.map(lambda _: _short_espeak_copy(str(bundled), "1.0"), range(32)))
    target = tmp_path / "cache" / "voiced" / "espeak-ng-data-1.0"
    assert set(paths) == {str(target)}
    assert (target / "phontab").read_bytes() == b"phonemes"
    assert os.listdir(target.parent) == [target.name]


def test_stale_staging_is_removed(bundled: Path, tmp_path: Path) -> None:
    root = tmp_path / "cache" / "voiced"
    stale = root / ".espeak-ng-data.crashed"
    fresh = root / ".espeak-ng-data.running"
    stale.mkdir(parents=True)
    fresh.mkdir()
    old = time.time() - 2 * 3600
    os.utime(stale, (old, old))
    _short_espeak_copy(str(bundled), "1.0")
    assert not stale.exists()
    assert fresh.exists()


def test_copy_failure_names_the_target(bundled: Path, tmp_path: Path) -> None:
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "voiced").write_text("not a directory")
    with pytest.raises(EspeakNotFound, match=r"could not copy espeak-ng data to .*voiced"):
        _espeak_data(None)


def test_no_user_cache_dir_keeps_the_wheel_path(
    bundled: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("voiced.engines.kokoro._user_cache_dir", lambda: None)
    with pytest.raises(EspeakNotFound, match=str(bundled)):
        _espeak_data(None)


def test_long_clone_path_is_refused(
    bundled: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = _long_dir(tmp_path / "longcache", MAX_ESPEAK_DATA_PATH)
    monkeypatch.setattr("voiced.engines.kokoro._user_cache_dir", lambda: cache)
    with pytest.raises(EspeakNotFound, match=f"{MAX_ESPEAK_DATA_PATH} bytes or less"):
        _espeak_data(None)


def test_explicit_data_path_is_checked(bundled: Path, tmp_path: Path) -> None:
    with pytest.raises(EspeakNotFound, match=ESPEAK_INSTALL_HINT):
        _espeak_data(str(bundled))
    short = tmp_path / "short-data"
    short.mkdir()
    assert _espeak_data(str(short)) == str(short)
    # The limit is in bytes, not characters.
    edge = Path(os.path.realpath(tmp_path)) / "é"
    edge = edge / ("x" * (MAX_ESPEAK_DATA_PATH - len(str(edge).encode()) - 1))
    assert len(str(edge).encode()) == MAX_ESPEAK_DATA_PATH
    assert _espeak_data(str(edge)) == str(edge)
    with pytest.raises(EspeakNotFound):
        _espeak_data(str(edge) + "x")


async def test_load_reports_the_long_path_instead_of_exiting(
    bundled: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("kokoro_onnx")
    library = tmp_path / "libespeak-ng.dylib"
    library.touch()
    monkeypatch.setattr(
        "voiced.engines.kokoro.espeak_candidates", lambda: [(str(library), str(bundled))]
    )
    with pytest.raises(EspeakNotFound, match=f"{bundled}.*{MAX_ESPEAK_DATA_PATH}") as info:
        await KokoroEngine().load()
    assert "not installed" not in str(info.value)


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


@pytest.mark.model
def test_loads_from_a_venv_at_a_long_path(kokoro_engine: Any, tmp_path: Path) -> None:
    """The espeakng-loader wheel at a resolved path longer than its espeak-ng
    accepts: the engine loads from the short clone and phonemizes like the
    default engine. Runs in a child process because a regression makes
    espeak-ng exit the process."""
    espeakng_loader = pytest.importorskip("espeakng_loader")
    import shutil

    wheel = _long_dir(tmp_path / "venv", MAX_ESPEAK_DATA_PATH + 20) / "espeakng_loader"
    shutil.copytree(Path(espeakng_loader.__file__).parent, wheel)
    data = wheel / "espeak-ng-data"
    assert len(str(data).encode()) > MAX_ESPEAK_DATA_PATH
    script = textwrap.dedent(
        f"""
        import asyncio
        import espeakng_loader
        espeakng_loader.get_library_path = lambda: {str(wheel / "libespeak-ng.dylib")!r}
        espeakng_loader.get_data_path = lambda: {str(data)!r}
        from voiced.engines.kokoro import KokoroEngine
        engine = KokoroEngine(warmup=False)
        asyncio.run(engine.load())
        print(engine.phonemize("hello world", "af_heart"))
        """
    )
    env = {**os.environ, ENV_ESPEAK_LIBRARY: str(wheel / "libespeak-ng.dylib")}
    env.pop(ENV_ESPEAK_DATA, None)
    env.pop("PHONEMIZER_ESPEAK_LIBRARY", None)
    done = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=300
    )
    assert done.returncode == 0, done.stderr
    assert "Error processing file" not in done.stdout + done.stderr
    assert done.stdout.strip().splitlines()[-1] == kokoro_engine.phonemize(
        "hello world", "af_heart"
    )
