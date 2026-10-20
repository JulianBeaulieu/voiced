"""Kokoro engine tests. The ``model`` tests need the weights, espeak-ng and
Metal; everything else runs on CI."""

from __future__ import annotations

import os
import statistics
import subprocess
import sys
import textwrap
import time
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from voiced.engines import get_tts_engine
from voiced.engines.base import EngineNotLoaded
from voiced.engines.kokoro import (
    ENV_ESPEAK_DATA,
    ENV_ESPEAK_LIBRARY,
    ESPEAK_COPIES,
    ESPEAK_DATA_FILES,
    ESPEAK_REMEDY,
    HOMEBREW_PREFIXES,
    MAX_ESPEAK_DATA_PATH,
    EspeakCandidate,
    EspeakNotFound,
    KokoroEngine,
    _bundled_espeak,
    _short_espeak_copy,
    espeak_candidates,
    espeak_data_path,
    kokoro_voices,
)
from voiced.text import normalize

# -- registry, metadata, voice table -----------------------------------------


def test_load_asks_mlx_audio_for_the_pinned_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The voice packs come from the pinned snapshot, and the weights through
    mlx-audio's own download at the same revision and patterns."""
    import types

    from voiced import models

    pin = models.PINS[1]
    asked: dict[str, Any] = {}

    class Loaded(Exception):
        pass

    def load_model(repo: str, **kwargs: Any) -> Any:
        asked.update(repo=repo, **kwargs)
        raise Loaded

    utils = types.ModuleType("mlx_audio.tts.utils")
    utils.load_model = load_model  # type: ignore[attr-defined]
    for name in ("mlx", "mlx.core", "mlx_audio", "mlx_audio.tts"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "mlx_audio.tts.utils", utils)
    monkeypatch.setattr("voiced.engines.kokoro._load_espeak", lambda: None)
    snapshots: list[tuple[str, list[str] | None]] = []
    monkeypatch.setattr(
        models, "snapshot", lambda repo, patterns: snapshots.append((repo, patterns)) or tmp_path
    )
    with pytest.raises(Loaded):
        KokoroEngine(pin.repo)._load_sync()
    patterns = KokoroEngine.download_patterns()
    assert snapshots == [(pin.repo, patterns)]
    assert asked == {"repo": pin.repo, "revision": pin.revision, "allow_patterns": patterns}


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


def _data_dir(path: Path, content: bytes = b"phonemes") -> Path:
    """An espeak-ng data directory with every file English needs."""
    (path / "voices").mkdir(parents=True)
    for name in ESPEAK_DATA_FILES:
        (path / name).write_bytes(content)
    return path


def _fake_wheel(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An espeakng-loader wheel at ``root``: its library and data directory."""
    _data_dir(root / "espeak-ng-data")
    (root / "libespeak-ng.dylib").touch()
    wheel = EspeakCandidate(
        str(root / "libespeak-ng.dylib"), str(root / "espeak-ng-data"), "wheel", "1.0"
    )
    monkeypatch.setattr("voiced.engines.kokoro._bundled_espeak", lambda: wheel)
    return root


def _keg(root: Path) -> Path:
    (root / "lib").mkdir(parents=True)
    (root / "lib" / "libespeak-ng.dylib").touch()
    return root


def _wheel_candidate(data: Path) -> EspeakCandidate:
    return EspeakCandidate("libespeak-ng.dylib", str(data), "wheel", "1.0")


@pytest.fixture
def no_wheel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("voiced.engines.kokoro._bundled_espeak", lambda: None)


@pytest.mark.usefixtures("no_wheel")
def test_override_is_the_only_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lib = tmp_path / "libespeak-ng.dylib"
    lib.touch()
    data = tmp_path / "espeak-ng-data"
    data.mkdir()
    monkeypatch.setenv(ENV_ESPEAK_LIBRARY, str(lib))
    monkeypatch.setenv(ENV_ESPEAK_DATA, str(data))
    assert espeak_candidates() == [EspeakCandidate(str(lib), str(data), "override")]
    monkeypatch.delenv(ENV_ESPEAK_DATA)
    assert espeak_candidates() == [EspeakCandidate(str(lib), None, "override")]


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


def test_override_wins_over_the_wheel_and_homebrew(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV_ESPEAK_LIBRARY, raising=False)
    wheel = _fake_wheel(tmp_path / "wheel", monkeypatch)
    keg = _keg(tmp_path / "keg")
    monkeypatch.setattr("voiced.engines.kokoro.HOMEBREW_PREFIXES", (str(keg),))
    assert [(c.library, c.source) for c in espeak_candidates()] == [
        (str(wheel / "libespeak-ng.dylib"), "wheel"),
        (f"{keg}/lib/libespeak-ng.dylib", "homebrew"),
    ]
    lib = tmp_path / "override.dylib"
    lib.touch()
    monkeypatch.setenv(ENV_ESPEAK_LIBRARY, str(lib))
    # Without espeak_data the override reads the wheel's data, through the clone.
    assert espeak_candidates() == [
        EspeakCandidate(str(lib), str(wheel / "espeak-ng-data"), "override", "1.0")
    ]


def test_wheel_library_comes_first_with_a_short_data_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no Homebrew keg the wheel's library is the one candidate, and its
    data is the short clone, not the path inside the environment."""
    monkeypatch.delenv(ENV_ESPEAK_LIBRARY, raising=False)
    monkeypatch.setattr("voiced.engines.kokoro.HOMEBREW_PREFIXES", ("/nonexistent/prefix",))
    wheel = _fake_wheel(_long_dir(tmp_path / "venv", MAX_ESPEAK_DATA_PATH + 10), monkeypatch)
    monkeypatch.setattr("voiced.engines.kokoro._user_cache_dir", lambda: tmp_path / "cache")
    [candidate] = espeak_candidates()
    assert candidate == EspeakCandidate(
        str(wheel / "libespeak-ng.dylib"), str(wheel / "espeak-ng-data"), "wheel", "1.0"
    )
    clone = tmp_path / "cache" / "voiced" / "espeak-ng-data-1.0"
    assert espeak_data_path(candidate) == str(clone)
    assert not clone.exists()  # finding copies nothing


def test_wheel_without_its_library_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV_ESPEAK_LIBRARY, raising=False)
    wheel = _fake_wheel(tmp_path / "wheel", monkeypatch)
    (wheel / "libespeak-ng.dylib").unlink()
    keg = _keg(tmp_path / "keg")
    monkeypatch.setattr("voiced.engines.kokoro.HOMEBREW_PREFIXES", (str(keg),))
    assert [c.source for c in espeak_candidates()] == ["homebrew"]


def test_wheel_that_does_not_say_where_it_is_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An espeakng-loader without get_library_path, or whose data is missing."""

    def missing() -> str:
        raise RuntimeError("data path not exists")

    monkeypatch.setitem(sys.modules, "espeakng_loader", types.SimpleNamespace())
    assert _bundled_espeak() is None
    loader = types.SimpleNamespace(get_library_path=lambda: "lib.dylib", get_data_path=missing)
    monkeypatch.setitem(sys.modules, "espeakng_loader", loader)
    assert _bundled_espeak() is None


def test_real_wheel_library_is_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """espeakng-loader as locked: its library is where get_library_path() says."""
    pytest.importorskip("espeakng_loader")
    monkeypatch.delenv(ENV_ESPEAK_LIBRARY, raising=False)
    monkeypatch.setattr("voiced.engines.kokoro.HOMEBREW_PREFIXES", ())
    [candidate] = espeak_candidates()
    library = Path(candidate.library)
    assert library.name == "libespeak-ng.dylib" and library.is_file()
    assert library.parent.name == "espeakng_loader" and candidate.source == "wheel"


@pytest.mark.usefixtures("no_wheel")
def test_homebrew_candidates_only_when_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_ESPEAK_LIBRARY, raising=False)
    monkeypatch.setattr("voiced.engines.kokoro.HOMEBREW_PREFIXES", ("/nonexistent/prefix",))
    assert espeak_candidates() == []
    installed = [p for p in HOMEBREW_PREFIXES if Path(f"{p}/lib/libespeak-ng.dylib").is_file()]
    monkeypatch.setattr("voiced.engines.kokoro.HOMEBREW_PREFIXES", HOMEBREW_PREFIXES)
    assert [c.library for c in espeak_candidates()] == [
        f"{p}/lib/libespeak-ng.dylib" for p in installed
    ]


async def test_load_without_espeak_names_the_remedies(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("kokoro_onnx")
    monkeypatch.setattr("voiced.engines.kokoro.espeak_candidates", lambda: [])
    engine = KokoroEngine()
    with pytest.raises(EspeakNotFound) as info:
        await engine.load()
    assert str(info.value).endswith(f"found {ESPEAK_COPIES}. {ESPEAK_REMEDY}")
    assert "brew install espeak-ng" in ESPEAK_REMEDY and "Repair" in ESPEAK_REMEDY
    with pytest.raises(EngineNotLoaded):
        await engine.voices()


async def test_load_with_broken_espeak_names_the_remedies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("kokoro_onnx")
    bogus = tmp_path / "libespeak-ng.dylib"
    bogus.write_bytes(b"not a library")
    monkeypatch.setattr(
        "voiced.engines.kokoro.espeak_candidates",
        lambda: [EspeakCandidate(str(bogus), None, "override")],
    )
    with pytest.raises(EspeakNotFound) as info:
        await KokoroEngine().load()
    assert f"no copy it tried works ({bogus}: cannot load the library: " in str(info.value)
    assert str(info.value).endswith(ESPEAK_REMEDY)


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
    data = _data_dir(_long_dir(tmp_path / "venv", MAX_ESPEAK_DATA_PATH + 10) / "espeak-ng-data")
    monkeypatch.setattr("voiced.engines.kokoro._user_cache_dir", lambda: tmp_path / "cache")
    return data


def _clone(source: Path, version: str = "1.0") -> Path:
    """Clone ``source`` where :func:`espeak_data_path` puts the wheel's data
    of ``version``, as load does, and return that path."""
    candidate = EspeakCandidate("libespeak-ng.dylib", str(source), "wheel", version)
    target = Path(espeak_data_path(candidate) or "")
    _short_espeak_copy(str(source), target)
    return target


def test_bundled_data_is_cloned_to_the_user_cache(bundled: Path, tmp_path: Path) -> None:
    expected = tmp_path / "cache" / "voiced" / "espeak-ng-data-1.0"
    assert _clone(bundled) == expected
    assert (expected / "phontab").read_bytes() == b"phonemes"
    assert (expected / "voices").is_dir()
    assert sorted(os.listdir(tmp_path / "cache" / "voiced")) == [".lock", "espeak-ng-data-1.0"]


def test_clone_is_reused_and_never_replaced(bundled: Path, tmp_path: Path) -> None:
    first = _clone(bundled)
    (first / "marker").touch()
    assert _clone(bundled) == first
    assert (first / "marker").exists()
    # A wheel upgrade gets its own directory; the old one stays for running services.
    (bundled / "phontab").write_bytes(b"upgraded")
    second = _clone(bundled, "1.1")
    assert second != first
    assert (second / "phontab").read_bytes() == b"upgraded"
    assert (first / "marker").exists()


@pytest.mark.parametrize("purged", ESPEAK_DATA_FILES)
def test_partly_purged_clone_is_made_again(bundled: Path, purged: str) -> None:
    """espeak-ng exits the process on a missing data file, so a clone that
    lost any one of them is not reused."""
    target = _clone(bundled)
    (target / purged).unlink()  # the system purged part of the cache
    assert _clone(bundled) == target
    assert all((target / name).read_bytes() == b"phonemes" for name in ESPEAK_DATA_FILES)


def test_concurrent_clones_all_succeed(bundled: Path, tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(8) as pool:
        paths = list(pool.map(lambda _: _clone(bundled), range(32)))
    target = tmp_path / "cache" / "voiced" / "espeak-ng-data-1.0"
    assert set(paths) == {target}
    assert (target / "phontab").read_bytes() == b"phonemes"
    assert sorted(os.listdir(target.parent)) == [".lock", target.name]


def test_concurrent_repairs_of_a_purged_clone_all_succeed(bundled: Path) -> None:
    """Several loads find the same incomplete clone: one repairs it, the
    others wait and reuse that copy, and none fails or moves it aside."""
    from concurrent.futures import ThreadPoolExecutor

    target = _clone(bundled)
    for _ in range(5):
        (target / "phondata").unlink()
        with ThreadPoolExecutor(8) as pool:
            assert set(pool.map(lambda _: _clone(bundled), range(16))) == {target}
        assert (target / "phondata").read_bytes() == b"phonemes"


def test_stale_staging_is_removed(bundled: Path, tmp_path: Path) -> None:
    root = tmp_path / "cache" / "voiced"
    stale = root / ".espeak-ng-data.crashed"
    fresh = root / ".espeak-ng-data.running"
    stale.mkdir(parents=True)
    fresh.mkdir()
    old = time.time() - 2 * 3600
    os.utime(stale, (old, old))
    _clone(bundled)
    assert not stale.exists()
    assert fresh.exists()


def test_copy_failure_names_the_target(bundled: Path, tmp_path: Path) -> None:
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "voiced").write_text("not a directory")
    target = tmp_path / "cache" / "voiced" / "espeak-ng-data-1.0"
    with pytest.raises(EspeakNotFound, match=r"could not copy espeak-ng data to .*voiced"):
        _short_espeak_copy(str(bundled), target)
    # Found before load tries: the nearest existing parent is not a directory.
    with pytest.raises(EspeakNotFound, match=r"cannot copy espeak-ng data to .*voiced"):
        espeak_data_path(_wheel_candidate(bundled))


def test_unwritable_cache_is_refused_without_copying(bundled: Path, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    cache.chmod(0o500)
    try:
        with pytest.raises(EspeakNotFound, match=r"cannot copy espeak-ng data to .*not writable"):
            espeak_data_path(_wheel_candidate(bundled))
    finally:
        cache.chmod(0o700)


def test_no_user_cache_dir_keeps_the_wheel_path(
    bundled: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("voiced.engines.kokoro._user_cache_dir", lambda: None)
    with pytest.raises(EspeakNotFound, match=str(bundled)):
        espeak_data_path(_wheel_candidate(bundled))


def test_long_clone_path_is_refused(
    bundled: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = _long_dir(tmp_path / "longcache", MAX_ESPEAK_DATA_PATH)
    monkeypatch.setattr("voiced.engines.kokoro._user_cache_dir", lambda: cache)
    with pytest.raises(EspeakNotFound, match=f"longer than the {MAX_ESPEAK_DATA_PATH}"):
        espeak_data_path(_wheel_candidate(bundled))
    assert not (cache / "voiced").exists()  # refused before copying


def test_explicit_data_path_is_checked(bundled: Path, tmp_path: Path) -> None:
    def keg(data: Path | str) -> EspeakCandidate:
        return EspeakCandidate("libespeak-ng.dylib", str(data), "homebrew")

    with pytest.raises(EspeakNotFound, match=f"longer than the {MAX_ESPEAK_DATA_PATH}"):
        espeak_data_path(keg(bundled))
    short = tmp_path / "short-data"
    short.mkdir()
    assert espeak_data_path(keg(short)) == str(short)
    # The limit is in bytes, not characters.
    edge = Path(os.path.realpath(tmp_path)) / "é"
    edge = edge / ("x" * (MAX_ESPEAK_DATA_PATH - len(str(edge).encode()) - 1))
    assert len(str(edge).encode()) == MAX_ESPEAK_DATA_PATH
    assert espeak_data_path(keg(edge)) == str(edge)
    with pytest.raises(EspeakNotFound):
        espeak_data_path(keg(str(edge) + "x"))


async def test_load_reports_the_long_path_instead_of_exiting(
    bundled: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("kokoro_onnx")
    library = tmp_path / "libespeak-ng.dylib"
    library.touch()
    monkeypatch.setattr(
        "voiced.engines.kokoro.espeak_candidates",
        lambda: [EspeakCandidate(str(library), str(bundled), "override")],
    )
    with pytest.raises(EspeakNotFound, match=f"{bundled}.*{MAX_ESPEAK_DATA_PATH}") as info:
        await KokoroEngine().load()
    assert "found neither" not in str(info.value)


def test_load_falls_back_to_homebrew_when_the_wheel_data_cannot_be_used(
    bundled: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A library that does not load is skipped before the tokenizer, which
    would quietly run a system-wide espeak-ng instead; the wheel's data cannot
    be cloned (no user cache directory, so its long path is refused): the
    Homebrew keg after both is loaded."""
    tokenizer = pytest.importorskip("kokoro_onnx.tokenizer")
    from voiced.engines.kokoro import _load_espeak

    class FakeTokenizer:
        def __init__(self, config: Any) -> None:
            self.config = config

        def phonemize(self, text: str, lang: str) -> str:
            return "hello"

    def cdll(path: str) -> None:
        if path == "broken.dylib":
            raise OSError("slice is not valid mach-o file")

    monkeypatch.setattr(tokenizer, "Tokenizer", FakeTokenizer)
    monkeypatch.setattr("voiced.engines.kokoro.ctypes.CDLL", cdll)
    monkeypatch.setattr("voiced.engines.kokoro._user_cache_dir", lambda: None)
    keg_data = tmp_path / "keg-data"
    keg_data.mkdir()
    monkeypatch.setattr(
        "voiced.engines.kokoro.espeak_candidates",
        lambda: [
            EspeakCandidate("broken.dylib", str(keg_data), "override"),
            _wheel_candidate(bundled),
            EspeakCandidate("keg.dylib", str(keg_data), "homebrew"),
        ],
    )
    loaded = _load_espeak()
    assert loaded.config.lib_path == "keg.dylib"
    assert loaded.config.data_path == str(keg_data)


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
def test_yield_points_wait_then_submit_each_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each residual block calls the sentence's yielder, then submits its graph,
    keeping at most BLOCKS_IN_FLIGHT unfinished, and computes what it did
    before (ADR 0019). Needs the tts extra: the model tier."""
    pytest.importorskip("mlx_audio")
    import mlx.core as mx
    from mlx_audio.tts.models.kokoro import istftnet

    from voiced.engines import kokoro

    kokoro._install_yield_points()
    kokoro._install_yield_points()  # once per process, however often it is called
    for name in kokoro.YIELD_POINT_BLOCKS:
        assert getattr(istftnet, name)._voiced_yield_point
    original = istftnet.AdaINResBlock1.__call__.__kwdefaults__["_call"]
    assert not getattr(original, "__kwdefaults__", None)  # not wrapped twice

    submitted: list[object] = []
    monkeypatch.setattr(kokoro, "_submit", submitted.append)
    block = istftnet.AdaINResBlock1(channels=4, kernel_size=3, dilation=(1, 3, 5), style_dim=8)
    x = mx.random.normal((1, 4, 32))
    s = mx.random.normal((1, 8))
    sentence = kokoro._Submissions()
    calls: list[int] = []
    sentence.yielder = lambda: calls.append(1)  # type: ignore[assignment]
    token = kokoro._submissions.set(sentence)
    try:
        outs = [block(x, s) for _ in range(kokoro.BLOCKS_IN_FLIGHT + 1)]
        outs.append(block(x, s=s))  # keyword arguments pass through
        expected = outs[-kokoro.BLOCKS_IN_FLIGHT :]
        assert len(sentence.in_flight) == len(expected)
        assert all(a is b for a, b in zip(sentence.in_flight, expected, strict=True))
    finally:
        kokoro._submissions.reset(token)
    assert len(calls) == len(outs)
    assert len(submitted) == len(outs)
    assert all(a is b for a, b in zip(submitted, outs, strict=True))
    assert mx.allclose(outs[0], original(block, x, s)).item()
    block(x, s)  # outside a sentence: plain block
    assert len(calls) == len(outs)
    assert len(submitted) == len(outs)


@pytest.mark.model
def test_submit_queues_the_block_without_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("mlx_audio")
    import mlx.core as mx

    from voiced.engines import kokoro

    queued: list[object] = []
    monkeypatch.setattr(mx, "async_eval", queued.append)
    out = mx.zeros((2,))
    kokoro._submit(out)
    assert queued == [out]


@pytest.mark.model
def test_missing_block_class_is_skipped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A changed mlx-audio loses the GPU priority, not the service."""
    pytest.importorskip("mlx_audio")
    from mlx_audio.tts.models.kokoro import istftnet

    from voiced.engines import kokoro

    monkeypatch.delattr(istftnet, "AdainResBlk1d")
    with caplog.at_level("WARNING", logger="voiced.engines.kokoro"):
        kokoro._install_yield_points()
    assert any("AdainResBlk1d" in r.getMessage() for r in caplog.records)


def test_synth_waits_before_each_chunk_inside_its_own_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_synth`` gives each sentence one yielder, calls it before every chunk
    and exposes it to the blocks only while the sentence runs. No MLX needed."""
    from voiced.engines import kokoro

    waits: list[int] = []

    class CountingYielder:
        def __call__(self) -> None:
            waits.append(1)

    monkeypatch.setattr(kokoro.gpu, "Yielder", CountingYielder)
    seen: list[object] = []

    def model(chunk: str, style: object, speed: float, return_output: bool) -> object:
        current = kokoro._submissions.get()
        assert current is not None
        seen.append(current)
        assert len(waits) == len(seen)  # waited before this chunk
        return type("Out", (), {"audio": np.zeros(240, dtype=np.float32)})()

    phonemes = " ".join(["abcdefghi"] * 80)  # about 800 phonemes: two chunks
    tokenizer = type("Tok", (), {"phonemize": lambda self, text, lang: phonemes})()
    pcm = kokoro._synth(model, tokenizer, [object()] * 600, "text", "af_heart", 1.0)
    assert len(seen) == 2
    assert seen[0] is seen[1]
    assert kokoro._submissions.get() is None
    assert len(pcm) == 2 * 480


@pytest.mark.model
def test_block_submission_does_not_change_the_audio(kokoro_engine: Any) -> None:
    """With the same seed, a sentence synthesized through the yield points gives
    the same PCM as the model called outside a sentence, where the blocks run
    as mlx-audio wrote them (ADR 0019). Within 1 LSB: two unpatched runs with
    the same seed already differ by that at a few samples."""
    import mlx.core as mx

    from voiced.engines import kokoro

    voice = "af_heart"
    sentence = "Please play some relaxing music in the living room."
    pack = kokoro_engine._packs[voice]
    phonemes = kokoro_engine.phonemize(sentence, voice)

    def through_yield_points() -> bytes:
        mx.random.seed(7)
        return kokoro._synth(
            kokoro_engine._model, kokoro_engine._tokenizer, pack, sentence, voice, 1.0
        )

    def unpatched() -> bytes:
        mx.random.seed(7)
        out = kokoro_engine._model(phonemes, pack[len(phonemes) - 1], 1.0, return_output=True)
        return kokoro.float32_to_pcm16(np.array(out.audio, copy=False).reshape(-1))

    # On a worker thread, as the service runs it: MLX work left on pytest's
    # main thread kept the process from exiting after the session.
    with ThreadPoolExecutor(max_workers=1) as pool:
        patched = pool.submit(through_yield_points).result()
        plain = pool.submit(unpatched).result()
    assert len(patched) == len(plain)
    diff = np.frombuffer(patched, "<i2").astype(np.int32) - np.frombuffer(plain, "<i2")
    assert np.abs(diff).max() <= 1


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
    spelled = kokoro_engine.phonemize(normalize("CO2", language="en"), "af_heart")
    spoken = kokoro_engine.phonemize(normalize("see oh two", language="en"), "af_heart")
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
    text = normalize(" ".join(["HVAC"] * 60), language="en")
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
