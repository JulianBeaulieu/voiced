"""The model catalogue and its lock, the cache checks, byte progress, pinning
and the models step (ADR 0018, ADR 0022)."""

from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests import hf_cache_support as hf
from voiced import models, ratings
from voiced.config import Settings
from voiced.engines import registry
from voiced.engines.kokoro import KokoroEngine
from voiced.engines.moonshine import MoonshineEngine
from voiced.engines.nemotron import NemotronEngine
from voiced.engines.parakeet import ParakeetEngine
from voiced.engines.phonon import PhononEngine
from voiced.engines.qwen3_asr import Qwen3ASREngine
from voiced.engines.soprano import SopranoEngine
from voiced.engines.whisper import WhisperEngine
from voiced.launchd.progress import DOWNLOAD_PREFIX
from voiced.models import cache, commands, lockfile, pinning
from voiced.models.catalogue import Engine, Model

# The catalogue and the lock ---------------------------------------------------


def test_the_catalogue_and_the_lock_agree() -> None:
    """The one check that the hand-written catalogue and the generated lock
    name the same models, from the same repositories with the same patterns."""
    lock = lockfile.read_lock()
    catalogued = {m.id: (m.repo, m.patterns) for m in models.catalogued()}
    stale = pinning.stale(lock)
    dropped = sorted(set(lock) - set(catalogued))
    fix = f"run {lockfile.PIN_COMMAND} {' '.join(stale)}".rstrip()
    assert not stale, f"the lock lacks or has other patterns for {stale}: {fix}"
    assert not dropped, f"the lock has {dropped}, which the catalogue dropped: {fix}"
    assert {i: (e.repo, e.patterns) for i, e in lock.items()} == catalogued


def test_every_locked_file_has_a_size_and_a_sha256() -> None:
    for entry in models.lock().values():
        assert re.fullmatch(r"[0-9a-f]{40}", entry.revision), entry.repo
        assert entry.files and entry.bytes > 0, entry.repo
        for name, f in entry.files.items():
            assert f.bytes > 0 and re.fullmatch(r"[0-9a-f]{64}", f.sha256), (entry.repo, name)
        assert entry.revision not in entry.previous


def test_the_config_defaults_are_the_default_engines_first_models() -> None:
    defaults = Settings()
    assert (defaults.stt.engine, defaults.tts.engine) == ("parakeet", "kokoro")
    assert defaults.stt.model == models.default_model("parakeet").repo
    assert defaults.tts.model == models.default_model("kokoro").repo
    # The app's setup preselects these: one per service, none behind a token.
    recommended = [
        (engine["kind"], m["id"])
        for engine in lockfile.catalogue_view()["engines"]
        for m in engine["models"]
        if m["recommended"]
    ]
    assert recommended == [("stt", defaults.stt.model), ("tts", defaults.tts.model)]
    assert not any(models.find(m)[1].gated for _, m in recommended)  # type: ignore[index]


def test_the_manifest_is_printed_with_the_standard_library_alone() -> None:
    """The app's bundle script runs it from the unpacked wheel, without the
    dependencies installed; the lock is package data beside the module."""
    blocker = (
        "import sys\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        top = name.partition('.')[0]\n"
        "        if top not in sys.stdlib_module_names and top != 'voiced':\n"
        "            raise ImportError(f'not stdlib: {name}')\n"
        "sys.meta_path.insert(0, Block())\n"
        "from voiced.models.commands import main\n"
        "raise SystemExit(main([]))\n"
    )
    src = str(Path(models.__file__).parents[1])
    out = subprocess.run(
        [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {src!r})\n{blocker}"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(out.stdout) == lockfile.manifest()


@pytest.mark.parametrize(
    "cls",
    [
        ParakeetEngine,
        KokoroEngine,
        SopranoEngine,
        PhononEngine,
        Qwen3ASREngine,
        MoonshineEngine,
        NemotronEngine,
        WhisperEngine,
    ],
    ids=lambda c: c.name,
)
def test_each_catalogued_engine_takes_its_defaults_from_the_catalogue(cls: Any) -> None:
    default = models.default_model(cls.name)
    assert (cls.model_id, cls.license) == (default.id, default.license)
    assert models.engine(cls.name) is not None
    kind = models.engine(cls.name).kind  # type: ignore[union-attr]
    found = registry.list_stt_engines() if kind == "stt" else registry.list_tts_engines()
    assert found[cls.name] is cls  # the catalogue's kind is the registry group


def test_the_registry_sets_the_configured_models_licence() -> None:
    engine = registry.create_tts_engine("phonon", "gradium/phonon-40m-en")
    assert engine.license == "CC-BY-NC-4.0"
    # Outside the catalogue the engine's own licence stays.
    assert registry.create_tts_engine("kokoro", "someone/kokoro-fork").license == "Apache-2.0"


def test_models_that_share_a_repository_are_known_by_their_ids() -> None:
    """Several models may be folders of one repository: each has an id."""
    small = models.Model("org/assets", "MIT", ("small/*",), id="org/assets-small")
    assert (small.id, small.repo) == ("org/assets-small", "org/assets")
    assert models.Model("org/m", "MIT", ("*",)).id == "org/m"


def test_a_shared_repository_comes_with_every_model_of_its_engine() -> None:
    assert models.required("gradium/phonon-40m-en") == [
        "gradium/phonon-40m-en",
        "gradium/phonon-voices",
    ]
    assert models.required("someone/else") == ["someone/else"]
    assert models.find("gradium/phonon-voices") is None  # shared, not a model
    assert lockfile.pinned_revision("someone/else") is None


# Ratings (ADR 0024) -------------------------------------------------------------

BENCHMARKS = Path(__file__).parents[2] / "docs" / "benchmarks"


def _hardware(name: str) -> str:
    return " ".join(name.split()).casefold()


def rows_by_model(results: Iterable[dict[str, Any]]) -> dict[tuple[str, str, str], dict[str, Any]]:
    """The results of the reference Mac, by kind, engine and model."""
    rows: dict[tuple[str, str, str], dict[str, Any]] = {}
    for result in results:
        if _hardware(result["hardware"]) != _hardware(ratings.REFERENCE_HARDWARE):
            continue
        key = (result["kind"], result["engine"], result["model"])
        assert key not in rows, f"two results for {key} on the reference Mac"
        rows[key] = result
    return rows


@functools.cache
def reference_rows() -> dict[tuple[str, str, str], dict[str, Any]]:
    """docs/benchmarks/*.json read as JSON (the harness is a script, not a module)."""
    return rows_by_model(json.loads(p.read_text()) for p in sorted(BENCHMARKS.glob("*.json")))


def ratings_problem(
    engine: Engine, model: Model, rows: dict[tuple[str, str, str], dict[str, Any]]
) -> str | None:
    """Why ``model``'s ``quality`` and ``speed`` are not its reference row's,
    with the command that measures it; None when they are."""
    measure = (
        f"uv run python scripts/bench_engine.py {engine.kind} "
        f"--engine {engine.name} --model {model.id}"
    )
    row = rows.get((engine.kind, engine.name, model.id))
    if row is None:
        if (model.quality, model.speed) == (None, None):
            return None
        return (
            f"{model.id} has no result on the {ratings.REFERENCE_HARDWARE}: measure it "
            f"with `{measure}`, or set quality and speed to None"
        )
    rating = ratings.rate(ratings.rated(engine.kind, row["metrics"]))
    if (model.quality, model.speed) == (rating.quality, rating.speed):
        return None
    return (
        f"{model.id}: its result says quality={rating.quality}, speed={rating.speed}; "
        f"set those in the catalogue (measured by `{measure}`)"
    )


@pytest.mark.parametrize(
    ("engine", "model"),
    [(e, m) for e in models.CATALOGUE for m in e.models],
    ids=lambda v: v.id if isinstance(v, Model) else v.name,
)
def test_each_models_ratings_are_its_reference_rows(engine: Engine, model: Model) -> None:
    """``quality`` and ``speed`` are copied by hand from the model's result on
    the reference Mac; a model without one has neither."""
    problem = ratings_problem(engine, model, reference_rows())
    assert problem is None, problem


def test_a_wrong_rating_names_the_values_and_the_command() -> None:
    parakeet = models.engine("parakeet")
    assert parakeet is not None
    real = parakeet.models[0]
    row = reference_rows()[("stt", "parakeet", real.id)]
    wrong = models.Model(real.repo, real.license, real.patterns, quality=3, speed=5)
    problem = ratings_problem(parakeet, wrong, reference_rows())
    assert problem is not None
    assert f"quality={real.quality}, speed={real.speed}" in problem
    assert f"scripts/bench_engine.py stt --engine parakeet --model {real.id}" in problem
    # Another Mac's result, or one for another model, is not the reference row.
    other_mac = {**row, "hardware": "Mac Studio, Apple M1 Max, 32 GB"}
    other_model = {**row, "model": "someone/else"}
    rows = rows_by_model([other_mac, other_model])
    assert ratings_problem(parakeet, real, rows) == (
        f"{real.id} has no result on the {ratings.REFERENCE_HARDWARE}: measure it with "
        f"`uv run python scripts/bench_engine.py stt --engine parakeet --model {real.id}`, "
        "or set quality and speed to None"
    )
    unrated = models.Model(real.repo, real.license, real.patterns)
    assert ratings_problem(parakeet, unrated, rows) is None
    # The hardware is matched however it is spaced or cased.
    spaced = {**row, "hardware": "  macbook pro,  Apple M1 Max, 64 GB "}
    assert ratings_problem(parakeet, real, rows_by_model([spaced])) is None
    with pytest.raises(AssertionError, match="two results"):
        rows_by_model([row, spaced])


def test_notes_fit_one_line_and_shared_repositories_are_not_rated() -> None:
    for engine in models.CATALOGUE:
        for model in engine.models:
            assert len(model.note) <= 80 and "\n" not in model.note, model.id
        for shared in engine.shared:
            assert (shared.quality, shared.speed, shared.note) == (None, None, ""), shared.id


# The cache on disk -------------------------------------------------------------

KOKORO = "mlx-community/Kokoro-82M-bf16"


def _cache(hub: Path, repo: str, skip: Sequence[str] = ()) -> Path:
    snapshot = hf.add_locked(hub, models.lock()[repo])
    for name in skip:
        (snapshot / name).unlink()
    return snapshot


@pytest.fixture
def hub(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "hub"
    monkeypatch.setattr("voiced.models.cache.cache_dir", lambda: str(path))
    return path


def test_check_only_returns_a_complete_pinned_snapshot(hub: Path) -> None:
    snapshot = _cache(hub, KOKORO)
    assert models.snapshot(KOKORO, check_only=True) == snapshot
    assert models.not_downloaded(hub, KOKORO) == []


def test_check_only_finds_a_missing_file_without_the_hub(hub: Path) -> None:
    entry = models.lock()[KOKORO]
    _cache(hub, KOKORO, skip=["voices/af_heart.safetensors"])
    with pytest.raises(models.ModelNotDownloaded) as exc:
        models.snapshot(KOKORO, check_only=True)
    assert (exc.value.model_id, exc.value.revision) == (KOKORO, entry.revision)
    assert str(exc.value) == (
        f"{KOKORO} is not downloaded at revision {entry.revision}; "
        f"run voiced models download {KOKORO}"
    )
    assert models.missing_files(hub, entry) == ["voices/af_heart.safetensors"]


def test_a_file_of_another_size_is_missing(hub: Path) -> None:
    """A truncated download (huggingface_hub checks sizes, not hashes)."""
    snapshot = _cache(hub, KOKORO)
    os.truncate(snapshot / "config.json", 1)
    assert models.missing_files(hub, models.lock()[KOKORO]) == ["config.json"]


def test_a_missing_lock_is_an_error_not_an_empty_lock(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        lockfile.read_lock(tmp_path / "models.lock.json")


def test_a_link_whose_blob_is_gone_is_missing(hub: Path) -> None:
    snapshot = _cache(hub, KOKORO)
    (snapshot / "config.json").resolve().unlink()
    assert models.missing_files(hub, models.lock()[KOKORO]) == ["config.json"]


def test_a_model_needs_its_engines_shared_repository(hub: Path) -> None:
    _cache(hub, "gradium/phonon-31m-en")
    assert models.not_downloaded(hub, "gradium/phonon-31m-en") == ["gradium/phonon-voices"]
    _cache(hub, "gradium/phonon-voices")
    assert models.not_downloaded(hub, "gradium/phonon-31m-en") == []
    assert models.not_downloaded(hub, "someone/else") == []  # not checked


def test_check_only_of_an_unpinned_model_asks_the_cache_only(
    hub: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    huggingface_hub = pytest.importorskip("huggingface_hub")
    from huggingface_hub.errors import LocalEntryNotFoundError

    calls: list[dict[str, Any]] = []

    def offline(repo: str, **kwargs: Any) -> str:
        calls.append(kwargs)
        raise LocalEntryNotFoundError("not cached")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", offline)
    with pytest.raises(models.ModelNotDownloaded) as exc:
        models.snapshot("someone/else", check_only=True)
    assert calls == [{"local_files_only": True}]
    assert exc.value.revision is None


def test_a_local_model_directory_is_used_as_it_is(tmp_path: Path) -> None:
    assert models.snapshot(str(tmp_path), check_only=True) == tmp_path


def test_the_hubs_offline_and_not_found_errors_are_a_missing_model() -> None:
    pytest.importorskip("huggingface_hub")
    from huggingface_hub.errors import LocalEntryNotFoundError

    try:
        try:
            raise LocalEntryNotFoundError("offline")
        except LocalEntryNotFoundError as inner:
            raise RuntimeError("the engine's own wrapper") from inner
    except RuntimeError as exc:
        found = models.hub_missing_model(exc, KOKORO)
    assert found is not None and found.model_id == KOKORO
    assert found.revision == lockfile.pinned_revision(KOKORO)
    own = models.ModelNotDownloaded("gradium/phonon-voices", "abc")
    assert models.hub_missing_model(own, KOKORO) is own
    assert models.hub_missing_model(RuntimeError("Metal"), KOKORO) is None


# Byte progress -----------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _lines(written: list[str]) -> list[dict[str, Any]]:
    assert all(line.startswith(DOWNLOAD_PREFIX) for line in written)
    return [json.loads(line.removeprefix(DOWNLOAD_PREFIX)) for line in written]


def test_byte_reports_are_throttled_to_four_a_second() -> None:
    written: list[str] = []
    clock = Clock()
    report = cache.ByteReport("org/m", 100, 1000, written.append, clock)
    report.add(cache.BYTES_BAR, 0)  # the start, at what was cached
    for _ in range(10):
        clock.now += 0.0625
        report.add(cache.BYTES_BAR, 10)
    report.finish()
    assert _lines(written) == [
        {"kind": "bytes", "repo": "org/m", "bytes": 100, "total": 1000},
        {"kind": "bytes", "repo": "org/m", "bytes": 140, "total": 1000},  # 0.25 s later
        {"kind": "bytes", "repo": "org/m", "bytes": 180, "total": 1000},
        {"kind": "bytes", "repo": "org/m", "bytes": 1000, "total": 1000},
    ]


def test_the_two_byte_bars_count_and_the_further_one_wins() -> None:
    pytest.importorskip("huggingface_hub")
    report = cache.ByteReport("org/m", 0, 10, lambda line: None)
    counting = cache._counting_tqdm(report)
    written = counting(name=cache.BYTES_BAR, total=0, disable=True)
    transfer = counting(name=cache.TRANSFER_BAR, total=0, disable=True)
    files = counting(total=3, disable=True)  # the file count
    transfer.update(5)
    files.update(1)
    assert report.bytes == 5
    written.update(7)
    assert report.bytes == 7


def test_a_resumed_http_download_counts_its_cached_part_on_the_written_bar() -> None:
    """Plain HTTP: the resumed part arrives as the written bar's ``initial``
    (``_AggregatedTqdm`` updates only that bar with it), then each chunk
    feeds both bars."""
    pytest.importorskip("huggingface_hub")
    report = cache.ByteReport("org/m", 0, 100, lambda line: None)
    counting = cache._counting_tqdm(report)
    written = counting(name=cache.BYTES_BAR, total=0, disable=True)
    transfer = counting(name=cache.TRANSFER_BAR, total=0, disable=True)
    written.update(30)  # the initial
    written.update(10)
    transfer.update(10)
    assert report.bytes == 40


def test_a_repair_counts_on_from_the_snapshot() -> None:
    """``hf_hub_download``'s own bars count too, from where the snapshot
    ended: its network count alone moves the report, though the snapshot's
    written bar was ahead."""
    pytest.importorskip("huggingface_hub")
    report = cache.ByteReport("org/m", 0, 100, lambda line: None)
    counting = cache._counting_tqdm(report)
    counting(name=cache.BYTES_BAR, total=0, disable=True).update(60)
    counting(name=cache.TRANSFER_BAR, total=0, disable=True).update(40)
    report.rebase()
    counting(name="huggingface_hub.xet_get.transfer", total=40, disable=True).update(10)
    assert report.bytes == 70
    counting(name="huggingface_hub.xet_get", total=40, disable=True).update(40)
    assert report.bytes == 100


def test_bytes_arriving_under_hf_xet_are_reported_before_any_are_written() -> None:
    """As measured under hf_xet: the transfer bar moves for the whole
    download, the written bar only at its end, in large jumps; with
    deduplication the transfer ends below the file's size."""
    pytest.importorskip("huggingface_hub")
    written_lines: list[str] = []
    clock = Clock()
    report = cache.ByteReport("org/m", 100, 1100, written_lines.append, clock)
    counting = cache._counting_tqdm(report)
    written = counting(name=cache.BYTES_BAR, total=0, disable=True)
    transfer = counting(name=cache.TRANSFER_BAR, total=0, disable=True)
    report.add(cache.BYTES_BAR, 0)
    for _ in range(36):  # 9 s of network bytes, nothing written
        clock.now += 0.25
        transfer.update(25)
    reported = [line["bytes"] for line in _lines(written_lines)]
    assert reported == list(range(100, 1001, 25))
    for _ in range(4):  # then the file is written in jumps
        clock.now += 0.25
        written.update(250)
    report.finish()
    reported = [line["bytes"] for line in _lines(written_lines)]
    assert reported[-5:] == [1000, 1000, 1000, 1100, 1100]  # written passes 900 at last
    assert max(reported) == report.total == 1100


def test_bytes_never_run_past_the_total() -> None:
    written: list[str] = []
    clock = Clock()
    report = cache.ByteReport("org/m", 0, 100, written.append, clock)
    report.add(cache.TRANSFER_BAR, 150)  # network bytes beyond the files' size
    clock.now += 1
    report.add(cache.BYTES_BAR, 80)
    assert [line["bytes"] for line in _lines(written)] == [100, 100]


class FakeHub:
    """``huggingface_hub.snapshot_download``: a dry run lists two files, one
    cached; the download writes the other through the given tqdm class."""

    def __init__(self, offline: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.offline = offline

    def __call__(self, repo: str, **kwargs: Any) -> Any:
        from huggingface_hub.errors import DryRunError

        self.calls.append({"repo": repo, **kwargs})
        if kwargs.get("dry_run"):
            if self.offline:
                raise DryRunError("offline")
            return [
                SimpleNamespace(file_size=30, will_download=False),
                SimpleNamespace(file_size=70, will_download=True),
            ]
        tqdm_class = kwargs.get("tqdm_class")
        if tqdm_class is not None:
            bar = tqdm_class(name=cache.BYTES_BAR, total=70, disable=True)
            bar.update(70)
        return "/hf/hub/models--org--m/snapshots/abc"


@pytest.fixture
def fake_hub(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[..., FakeHub]:
    """Also ``hf_hub_download``, which writes the file at its locked size
    (the repair after a snapshot), recording what it was asked for."""
    huggingface_hub = pytest.importorskip("huggingface_hub")

    def make(offline: bool = False, repairs: bool = True) -> FakeHub:
        fake = FakeHub(offline)
        fake.repaired = []  # type: ignore[attr-defined]
        fake.repair_bars = []  # type: ignore[attr-defined]

        def hf_hub_download(
            repo: str, name: str, *, revision: str, force_download: bool, tqdm_class: Any
        ) -> str:
            fake.repaired.append((repo, name, force_download))  # type: ignore[attr-defined]
            fake.repair_bars.append(tqdm_class)  # type: ignore[attr-defined]
            path = cache.snapshot_dir(Path(cache.cache_dir()), repo, revision) / name
            if repairs:
                entry = next(e for e in models.lock().values() if e.repo == repo)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"")
                os.truncate(path, entry.files[name].bytes)
            return str(path)

        monkeypatch.setattr(huggingface_hub, "snapshot_download", fake)
        monkeypatch.setattr(huggingface_hub, "hf_hub_download", hf_hub_download)
        monkeypatch.setattr("voiced.models.cache.cache_dir", lambda: str(tmp_path / "hf" / "hub"))
        return fake

    return make


def test_a_pinned_model_downloads_its_locked_files_with_byte_progress(
    fake_hub: Callable[..., FakeHub],
) -> None:
    fake = fake_hub()
    entry = models.lock()[KOKORO]
    written: list[str] = []
    path = models.snapshot(KOKORO, write=written.append)
    assert path == Path("/hf/hub/models--org--m/snapshots/abc")
    # The lock says what is cached: no dry run.
    assert [(c["revision"], tuple(c["allow_patterns"])) for c in fake.calls] == [
        (entry.revision, entry.patterns)
    ]
    lines = _lines(written)
    assert lines[0] == {
        "kind": "model",
        "repo": KOKORO,
        "revision": entry.revision,
        "path": cache.cache_dir(),
    }
    # The snapshot left every file missing, so each is fetched again whole.
    assert sorted(n for _, n, _ in fake.repaired) == sorted(entry.files)  # type: ignore[attr-defined]
    assert all(force for _, _, force in fake.repaired)  # type: ignore[attr-defined]
    # With progress, the repair counts its bytes too.
    assert all(bar is not None for bar in fake.repair_bars)  # type: ignore[attr-defined]
    # Starts at what is cached (nothing here); the total is the lock's.
    assert lines[1] == {"kind": "bytes", "repo": KOKORO, "bytes": 0, "total": entry.bytes}
    assert lines[-1] == {
        "kind": "bytes",
        "repo": KOKORO,
        "bytes": entry.bytes,
        "total": entry.bytes,
    }


def test_a_resumed_download_starts_at_what_the_lock_says_is_cached(
    fake_hub: Callable[..., FakeHub], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_hub()
    hub = tmp_path / "hub"
    monkeypatch.setattr("voiced.models.cache.cache_dir", lambda: str(hub))
    snapshot = hf.add_locked(hub, models.lock()[KOKORO])
    (snapshot / "config.json").unlink()
    written: list[str] = []
    models.snapshot(KOKORO, write=written.append)
    entry = models.lock()[KOKORO]
    done = entry.bytes - entry.files["config.json"].bytes
    assert _lines(written)[1] == {
        "kind": "bytes",
        "repo": KOKORO,
        "bytes": done,
        "total": entry.bytes,
    }


def test_a_file_that_stays_truncated_fails_the_download(
    fake_hub: Callable[..., FakeHub],
) -> None:
    """huggingface_hub keeps a file that is there whatever its size: a file
    still short after the fetch again is an error, never done."""
    fake_hub(repairs=False)
    with pytest.raises(models.ModelsError) as exc:
        models.snapshot(KOKORO)
    assert exc.value.code == "failed"
    assert exc.value.message.endswith(
        f"of {KOKORO} did not download whole; delete the model "
        f"(voiced models delete {KOKORO}) and download it again"
    )


def test_an_unpinned_model_downloads_main_whole(fake_hub: Callable[..., FakeHub]) -> None:
    fake = fake_hub()
    written: list[str] = []
    models.snapshot("org/m", write=written.append)
    assert {c.get("revision") for c in fake.calls} == {None}
    assert {c.get("allow_patterns") for c in fake.calls} == {None}
    assert _lines(written)[1:] == [
        {"kind": "bytes", "repo": "org/m", "bytes": 30, "total": 100},
        {"kind": "bytes", "repo": "org/m", "bytes": 100, "total": 100},
    ]


def test_offline_the_download_goes_to_the_cache_without_bytes(
    fake_hub: Callable[..., FakeHub],
) -> None:
    fake = fake_hub(offline=True)
    written: list[str] = []
    models.snapshot("org/m", write=written.append)
    assert [c.get("dry_run", False) for c in fake.calls] == [True, False]
    assert [list(line) for line in _lines(written)] == [["kind", "repo", "revision", "path"]]


def test_a_gated_model_says_how_to_get_access(monkeypatch: pytest.MonkeyPatch) -> None:
    huggingface_hub = pytest.importorskip("huggingface_hub")
    import httpx
    from huggingface_hub.errors import GatedRepoError

    def gated(repo: str, **kwargs: Any) -> Any:
        request = httpx.Request("GET", f"https://huggingface.co/{repo}")
        raise GatedRepoError("401", response=httpx.Response(401, request=request))

    monkeypatch.setattr(huggingface_hub, "snapshot_download", gated)
    monkeypatch.setattr(huggingface_hub, "get_token", lambda: None)
    with pytest.raises(models.ModelsError) as exc:
        models.snapshot("gradium/phonon-40m-en")
    assert exc.value.code == "token_missing"
    assert "https://huggingface.co/gradium/phonon-40m-en" in exc.value.message
    assert "`hf auth login` with HF_HOME set" in exc.value.message
    assert isinstance(exc.value.__cause__, GatedRepoError)


# Pinning ---------------------------------------------------------------------

SHA = "a" * 40
NEWER = "b" * 40


def _files(commit: str) -> Callable[[str, str, Sequence[str]], pinning.HubFiles]:
    def files(repo: str, revision: str, patterns: Sequence[str]) -> pinning.HubFiles:
        files.asked.append(revision)  # type: ignore[attr-defined]
        return pinning.HubFiles(
            commit, {"config.json": (10, None), "w.safetensors": (90, "c" * 64)}
        )

    files.asked = []  # type: ignore[attr-defined]
    return files


def test_pin_resolves_a_revision_and_hashes_the_small_files() -> None:
    model = models.Model("org/m", "MIT", ("*.json", "*.safetensors"))
    hashed: list[str] = []
    files = _files(SHA)
    entry = pinning.pin_repo(
        model, None, None, files=files, sha256=lambda r, c, n: hashed.append(n) or "d" * 64
    )
    assert files.asked == ["main"]  # type: ignore[attr-defined]
    assert hashed == ["config.json"]  # the hub has the sha256 of the LFS file
    assert entry == models.Locked(
        "org/m",
        "org/m",
        SHA,
        model.patterns,
        {
            "config.json": lockfile.LockedFile(10, "d" * 64),
            "w.safetensors": lockfile.LockedFile(90, "c" * 64),
        },
    )
    assert entry.bytes == 100


def test_re_pinning_keeps_the_revision_unless_asked_and_accumulates_previous() -> None:
    model = models.Model("org/m", "MIT", ("*",))
    current = models.Locked("org/m", "org/m", SHA, ("*",), {}, ("0" * 40,))
    files = _files(SHA)
    same = pinning.pin_repo(model, current, None, files=files, sha256=lambda *a: "d" * 64)
    assert files.asked == [SHA] and same.previous == ("0" * 40,)  # type: ignore[attr-defined]
    moved = pinning.pin_repo(
        model, current, "main", files=_files(NEWER), sha256=lambda *a: "d" * 64
    )
    assert moved.revision == NEWER and moved.previous == ("0" * 40, SHA)


def test_pin_without_repositories_fixes_what_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = tmp_path / "models.lock.json"
    shipped = lockfile.read_lock()
    gone = models.Locked(
        "org/dropped", "org/dropped", SHA, ("*",), {"f": lockfile.LockedFile(1, "e" * 64)}
    )
    stale = models.Locked(KOKORO, KOKORO, SHA, ("other",), {"f": lockfile.LockedFile(1, "e" * 64)})
    lockfile.write_lock({**shipped, KOKORO: stale, "org/dropped": gone}, lock)
    asked: list[tuple[str, str | None]] = []

    def fake_pin(model: models.Model, current: models.Locked | None, revision: str | None) -> Any:
        asked.append((model.id, revision))
        return models.Locked(
            model.id, model.repo, NEWER, model.patterns, {"f": lockfile.LockedFile(1, "e" * 64)}
        )

    monkeypatch.setattr("voiced.models.pinning.pin_repo", fake_pin)
    assert pinning.pin([], path=lock) == [KOKORO, "org/dropped"]
    assert asked == [(KOKORO, None)]
    written = lockfile.read_lock(lock)
    assert set(written) == set(shipped) and written[KOKORO].revision == NEWER
    # The dropped model's revision stays known, so uninstall still deletes it.
    assert lockfile.read_retired(lock) == {("org/dropped", SHA)}
    with pytest.raises(SystemExit, match="not in the catalogue: org/new"):
        pinning.pin(["org/new"], path=lock)


# The models step -------------------------------------------------------------


def test_the_models_step_builds_each_engine_first_then_fetches_and_loads(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A catalogued engine's model comes with its shared repositories; a
    plugin engine (the fakes) downloads in its own load(); a model whose
    engine is not installed is fetched whole; a model the engine refuses
    fails before anything is fetched."""
    fetched: list[str] = []
    monkeypatch.setattr("voiced.models.commands.cache_dir", lambda: "/hf/hub")
    monkeypatch.setattr("voiced.models.cache.cache_dir", lambda: "/hf/hub")
    monkeypatch.setattr("voiced.models.cache.check_disk", lambda hub, needed: None)
    monkeypatch.setattr("voiced.models.cache.snapshot", lambda repo, write: fetched.append(repo))
    config = tmp_path / "config.toml"
    config.write_text(
        '[stt]\nengine = "fake"\nmodel = "org/stt"\n[tts]\nengine = "nope"\nmodel = "org/tts"\n'
    )
    commands.download(config, None)
    assert fetched == ["org/tts"]

    loaded: list[str] = []

    async def load(self: PhononEngine) -> None:
        loaded.append(self.model_id)

    monkeypatch.setattr(PhononEngine, "load", load)
    config.write_text(
        '[stt]\nengine = "fake"\n[tts]\nengine = "phonon"\nmodel = "gradium/phonon-40m-en"\n'
    )
    fetched.clear()
    commands.download(config, None)
    assert fetched == ["gradium/phonon-40m-en", "gradium/phonon-voices"]
    assert loaded == ["gradium/phonon-40m-en"]

    config.write_text(
        '[stt]\nengine = "fake"\n[tts]\nengine = "phonon"\nmodel = "gradium/phonon-40m-fr"\n'
    )
    fetched.clear()
    with pytest.raises(ValueError, match="not in voiced's model catalogue"):
        commands.download(config, None)
    assert fetched == []


def test_a_bad_set_is_one_line_and_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["download", "--config", str(tmp_path / "c.toml"), "--set", "stt.enabled=maybe"]
    assert commands.main(argv) == 2
    assert capsys.readouterr().err == '--set stt.enabled: must be true or false, got "maybe"\n'


@pytest.mark.parametrize("stt", ["true", "false"], ids=["tts off", "both off"])
def test_the_models_step_downloads_nothing_for_a_service_that_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stt: str
) -> None:
    """A service switched off loads no model (ADR 0033), so the install does
    not fetch one for it: the app's setup installs speech to text alone.
    With both off the step downloads nothing and says so for each."""
    fetched: list[str] = []
    monkeypatch.setattr("voiced.models.commands.cache_dir", lambda: "/hf/hub")
    monkeypatch.setattr("voiced.models.commands.fetch", lambda model, write: fetched.append(model))
    config = tmp_path / "config.toml"
    config.write_text(
        f'[stt]\nenabled = {stt}\nengine = "fake"\n[tts]\nenabled = false\nmodel = "org/tts"\n'
    )
    commands.download(config, None)
    assert fetched == []
    out = capsys.readouterr().out
    assert "tts: off; nothing to download" in out
    assert ("stt: off; nothing to download" in out) == (stt == "false")
    assert ("stt: engine=fake" in out) == (stt == "true")


# Against the hub (model tier) ----------------------------------------------------


@pytest.mark.model
def test_the_lock_is_what_the_hub_has_at_each_pin() -> None:
    """Network only: each locked file's size and, for LFS files, sha256."""
    for entry in models.lock().values():
        hub = pinning.hub_files(entry.repo, entry.revision, entry.patterns)
        assert hub.commit == entry.revision
        assert {n: s for n, (s, _) in hub.files.items()} == {
            n: f.bytes for n, f in entry.files.items()
        }, entry.repo
        for name, (_, digest) in hub.files.items():
            assert digest is None or digest == entry.files[name].sha256, (entry.repo, name)


@pytest.mark.model
def test_a_real_download_reports_its_bytes(tmp_path: Path) -> None:
    """Network only, into an empty cache: one small file of a pinned model,
    counted through huggingface_hub's own bars."""
    from huggingface_hub import snapshot_download

    entry = models.lock()[KOKORO]
    report = cache.ByteReport(KOKORO, 0, entry.bytes, lambda line: None)
    path = snapshot_download(
        KOKORO,
        revision=entry.revision,
        allow_patterns=["config.json"],
        cache_dir=tmp_path / "hub",
        tqdm_class=cache._counting_tqdm(report),
    )
    assert report.bytes == (Path(path) / "config.json").stat().st_size > 0
