"""The model catalogue and its lock, the cache checks, byte progress, pinning
and the models step (ADR 0018, ADR 0022)."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests import hf_cache_support as hf
from voiced import models
from voiced.config import Settings
from voiced.engines import registry
from voiced.engines.kokoro import KokoroEngine
from voiced.engines.parakeet import ParakeetEngine
from voiced.engines.phonon import PhononEngine
from voiced.engines.soprano import SopranoEngine
from voiced.launchd.progress import DOWNLOAD_PREFIX

# The catalogue and the lock ---------------------------------------------------


def test_the_catalogue_and_the_lock_agree() -> None:
    """The one check that the hand-written catalogue and the generated lock
    name the same models, from the same repositories with the same patterns."""
    lock = models.read_lock()
    catalogued = {m.id: (m.repo, m.patterns) for m in models.catalogued()}
    stale = models.stale(lock)
    dropped = sorted(set(lock) - set(catalogued))
    fix = f"run {models.PIN_COMMAND} {' '.join(stale)}".rstrip()
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
    assert models.manifest()["models"] == [
        {"repo": e.repo, "revision": e.revision, "bytes": e.bytes}
        for e in (models.lock()[defaults.stt.model], models.lock()[defaults.tts.model])
    ]


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
        "from voiced.models import main\n"
        "raise SystemExit(main([]))\n"
    )
    src = str(Path(models.__file__).parents[1])
    out = subprocess.run(
        [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {src!r})\n{blocker}"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(out.stdout) == models.manifest()


@pytest.mark.parametrize(
    "cls", [ParakeetEngine, KokoroEngine, SopranoEngine, PhononEngine], ids=lambda c: c.name
)
def test_each_catalogued_engine_takes_its_defaults_from_the_catalogue(cls: Any) -> None:
    default = models.default_model(cls.name)
    assert (cls.model_id, cls.license) == (default.repo, default.license)
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
    assert models.pinned_revision("someone/else") is None


# The cache on disk -------------------------------------------------------------

KOKORO = "mlx-community/Kokoro-82M-bf16"


def _cache(hub: Path, repo: str, skip: Sequence[str] = ()) -> Path:
    entry = models.lock()[repo]
    files = {name: name.encode() for name in entry.files if name not in skip}
    return hf.add_revision(hub, repo, entry.revision, files)


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
    assert (exc.value.repo, exc.value.revision) == (KOKORO, entry.revision)
    assert str(exc.value) == (
        f"{KOKORO} is not downloaded at revision {entry.revision}; "
        f"run voiced models download {KOKORO}"
    )
    assert models.missing_files(hub, entry) == ["voices/af_heart.safetensors"]


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
    assert found is not None and found.repo == KOKORO
    assert found.revision == models.pinned_revision(KOKORO)
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
    report = models.ByteReport("org/m", 100, 1000, 1000, written.append, clock)
    report.add(0)  # the start, at what was cached
    for _ in range(10):
        clock.now += 0.0625
        report.add(10)
    report.finish()
    assert _lines(written) == [
        {"kind": "bytes", "repo": "org/m", "bytes": 100, "total": 1000},
        {"kind": "bytes", "repo": "org/m", "bytes": 140, "total": 1000},  # 0.25 s later
        {"kind": "bytes", "repo": "org/m", "bytes": 180, "total": 1000},
        {"kind": "bytes", "repo": "org/m", "bytes": 1000, "total": 1000},
    ]


def test_only_the_bar_of_bytes_written_counts() -> None:
    pytest.importorskip("huggingface_hub")
    report = models.ByteReport("org/m", 0, 10, 10, lambda line: None)
    counting = models._counting_tqdm(report)
    written = counting(name=models.BYTES_BAR, total=0, disable=True)
    transfer = counting(name="huggingface_hub.snapshot_download.transfer", disable=True)
    files = counting(total=3, disable=True)  # the file count
    written.update(7)
    transfer.update(5)
    files.update(1)
    assert report.bytes == 7


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
            bar = tqdm_class(name=models.BYTES_BAR, total=70, disable=True)
            bar.update(70)
        return "/hf/hub/models--org--m/snapshots/abc"


@pytest.fixture
def fake_hub(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FakeHub]:
    huggingface_hub = pytest.importorskip("huggingface_hub")

    def make(offline: bool = False) -> FakeHub:
        fake = FakeHub(offline)
        monkeypatch.setattr(huggingface_hub, "snapshot_download", fake)
        monkeypatch.setattr("voiced.models.cache.cache_dir", lambda: "/hf/hub")
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
    assert {c["revision"] for c in fake.calls} == {entry.revision}
    assert {tuple(c["allow_patterns"]) for c in fake.calls} == {entry.patterns}
    lines = _lines(written)
    assert lines[0] == {
        "kind": "model",
        "repo": KOKORO,
        "revision": entry.revision,
        "path": "/hf/hub",
    }
    # Starts at what was cached; the total is the lock's; ends at what is there.
    assert lines[1] == {"kind": "bytes", "repo": KOKORO, "bytes": 30, "total": entry.bytes}
    assert lines[-1] == {"kind": "bytes", "repo": KOKORO, "bytes": 100, "total": entry.bytes}


def test_an_unpinned_model_downloads_main_whole(fake_hub: Callable[..., FakeHub]) -> None:
    fake = fake_hub()
    written: list[str] = []
    models.snapshot("org/m", write=written.append)
    assert {c["revision"] for c in fake.calls} == {None}
    assert {c["allow_patterns"] for c in fake.calls} == {None}
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
    with pytest.raises(RuntimeError) as exc:
        models.snapshot("gradium/phonon-40m-en")
    message = str(exc.value)
    assert "https://huggingface.co/gradium/phonon-40m-en" in message
    assert "`hf auth login` with HF_HOME set" in message
    assert isinstance(exc.value.__cause__, GatedRepoError)


# Pinning ---------------------------------------------------------------------

SHA = "a" * 40
NEWER = "b" * 40


def _files(commit: str) -> Callable[[str, str, Sequence[str]], models.HubFiles]:
    def files(repo: str, revision: str, patterns: Sequence[str]) -> models.HubFiles:
        files.asked.append(revision)  # type: ignore[attr-defined]
        return models.HubFiles(commit, {"config.json": (10, None), "w.safetensors": (90, "c" * 64)})

    files.asked = []  # type: ignore[attr-defined]
    return files


def test_pin_resolves_a_revision_and_hashes_the_small_files() -> None:
    model = models.Model("org/m", "MIT", ("*.json", "*.safetensors"))
    hashed: list[str] = []
    files = _files(SHA)
    entry = models.pin_repo(
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
            "config.json": models.LockedFile(10, "d" * 64),
            "w.safetensors": models.LockedFile(90, "c" * 64),
        },
    )
    assert entry.bytes == 100


def test_re_pinning_keeps_the_revision_unless_asked_and_accumulates_previous() -> None:
    model = models.Model("org/m", "MIT", ("*",))
    current = models.Locked("org/m", "org/m", SHA, ("*",), {}, ("0" * 40,))
    files = _files(SHA)
    same = models.pin_repo(model, current, None, files=files, sha256=lambda *a: "d" * 64)
    assert files.asked == [SHA] and same.previous == ("0" * 40,)  # type: ignore[attr-defined]
    moved = models.pin_repo(model, current, "main", files=_files(NEWER), sha256=lambda *a: "d" * 64)
    assert moved.revision == NEWER and moved.previous == ("0" * 40, SHA)


def test_pin_without_repositories_fixes_what_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = tmp_path / "models.lock.json"
    shipped = models.read_lock()
    gone = models.Locked(
        "org/dropped", "org/dropped", SHA, ("*",), {"f": models.LockedFile(1, "e" * 64)}
    )
    stale = models.Locked(KOKORO, KOKORO, SHA, ("other",), {"f": models.LockedFile(1, "e" * 64)})
    models.write_lock({**shipped, KOKORO: stale, "org/dropped": gone}, lock)
    asked: list[tuple[str, str | None]] = []

    def fake_pin(model: models.Model, current: models.Locked | None, revision: str | None) -> Any:
        asked.append((model.id, revision))
        return models.Locked(
            model.id, model.repo, NEWER, model.patterns, {"f": models.LockedFile(1, "e" * 64)}
        )

    monkeypatch.setattr("voiced.models.pinning.pin_repo", fake_pin)
    assert models.pin([], path=lock) == [KOKORO, "org/dropped"]
    assert asked == [(KOKORO, None)]
    written = models.read_lock(lock)
    assert set(written) == set(shipped) and written[KOKORO].revision == NEWER
    with pytest.raises(SystemExit, match="not in the catalogue: org/new"):
        models.pin(["org/new"], path=lock)


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
    monkeypatch.setattr("voiced.models.commands.snapshot", lambda repo, write: fetched.append(repo))
    config = tmp_path / "config.toml"
    config.write_text(
        '[stt]\nengine = "fake"\nmodel = "org/stt"\n[tts]\nengine = "nope"\nmodel = "org/tts"\n'
    )
    models.download(config, None)
    assert fetched == ["org/tts"]

    loaded: list[str] = []

    async def load(self: PhononEngine) -> None:
        loaded.append(self.model_id)

    monkeypatch.setattr(PhononEngine, "load", load)
    config.write_text(
        '[stt]\nengine = "fake"\n[tts]\nengine = "phonon"\nmodel = "gradium/phonon-40m-en"\n'
    )
    fetched.clear()
    models.download(config, None)
    assert fetched == ["gradium/phonon-40m-en", "gradium/phonon-voices"]
    assert loaded == ["gradium/phonon-40m-en"]

    config.write_text(
        '[stt]\nengine = "fake"\n[tts]\nengine = "phonon"\nmodel = "gradium/phonon-40m-fr"\n'
    )
    fetched.clear()
    with pytest.raises(ValueError, match="not in voiced's model catalogue"):
        models.download(config, None)
    assert fetched == []


# Against the hub (model tier) ----------------------------------------------------


@pytest.mark.model
def test_the_lock_is_what_the_hub_has_at_each_pin() -> None:
    """Network only: each locked file's size and, for LFS files, sha256."""
    for entry in models.lock().values():
        hub = models.hub_files(entry.repo, entry.revision, entry.patterns)
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
    report = models.ByteReport(KOKORO, 0, entry.bytes, entry.bytes, lambda line: None)
    path = snapshot_download(
        KOKORO,
        revision=entry.revision,
        allow_patterns=["config.json"],
        cache_dir=tmp_path / "hub",
        tqdm_class=models._counting_tqdm(report),
    )
    assert report.bytes == (Path(path) / "config.json").stat().st_size > 0
