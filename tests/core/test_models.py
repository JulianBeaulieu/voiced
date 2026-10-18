"""The pinned default models, their byte progress and the models step (ADR 0018)."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from voiced import models
from voiced.config import Settings
from voiced.engines.kokoro import KokoroEngine
from voiced.engines.parakeet import ParakeetEngine
from voiced.launchd.progress import DOWNLOAD_PREFIX


def test_the_default_models_are_pinned_to_commits() -> None:
    defaults = Settings()
    assert [p.repo for p in models.PINS] == [defaults.stt.model, defaults.tts.model]
    for pin in models.PINS:
        assert len(pin.revision) == 40 and int(pin.revision, 16) >= 0
        assert pin.bytes > 0
    assert models.pinned_revision(defaults.stt.model) == models.PINS[0].revision
    assert models.pinned_revision("someone/else") is None


def test_the_manifest_is_printed_with_the_standard_library_alone() -> None:
    """The app's bundle script runs it from the unpacked wheel, without the
    dependencies installed."""
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
    assert json.loads(out.stdout) == {
        "models": [{"repo": p.repo, "revision": p.revision, "bytes": p.bytes} for p in models.PINS]
    }


def test_the_engines_name_what_they_load() -> None:
    assert models.download_patterns(ParakeetEngine) == ["config.json", "model.safetensors"]
    # The config, the weights and voices/*.safetensors; not the sample WAVs or the .pt copies.
    assert models.download_patterns(KokoroEngine) == ["config.json", "*.safetensors"]
    assert models.download_patterns(object) is None


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
    report = models.ByteReport("org/m", 100, 1000, written.append, clock)
    report.add(0)  # the start, at what was cached
    for _ in range(10):
        clock.now += 0.0625
        report.add(10)
    report.finish(1000)
    assert _lines(written) == [
        {"repo": "org/m", "bytes": 100, "total": 1000},
        {"repo": "org/m", "bytes": 140, "total": 1000},  # 0.25 s later
        {"repo": "org/m", "bytes": 180, "total": 1000},
        {"repo": "org/m", "bytes": 1000, "total": 1000},
    ]


def test_only_the_bar_of_bytes_written_counts() -> None:
    pytest.importorskip("huggingface_hub")
    report = models.ByteReport("org/m", 0, 10, lambda line: None)
    counting = models._counting_tqdm(report)
    written = counting(name=models.BYTES_BAR, total=0, disable=True)
    transfer = counting(name="huggingface_hub.snapshot_download.transfer", disable=True)
    files = counting(total=3, disable=True)  # the file count
    written.update(7)
    transfer.update(5)
    files.update(1)
    assert report.bytes == 7


def test_a_local_model_directory_is_used_as_it_is(tmp_path: Path) -> None:
    assert models.snapshot(str(tmp_path), ["*.json"]) == tmp_path


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
def hub(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FakeHub]:
    huggingface_hub = pytest.importorskip("huggingface_hub")

    def make(offline: bool = False) -> FakeHub:
        fake = FakeHub(offline)
        monkeypatch.setattr(huggingface_hub, "snapshot_download", fake)
        monkeypatch.setattr(models, "cache_dir", lambda: "/hf/hub")
        return fake

    return make


def test_a_pinned_model_downloads_its_revision_with_byte_progress(
    hub: Callable[..., FakeHub],
) -> None:
    fake = hub()
    pin = models.PINS[1]
    written: list[str] = []
    path = models.snapshot(pin.repo, ["config.json"], write=written.append)
    assert path == Path("/hf/hub/models--org--m/snapshots/abc")
    assert {c["revision"] for c in fake.calls} == {pin.revision}
    assert {tuple(c["allow_patterns"]) for c in fake.calls} == {("config.json",)}
    lines = _lines(written)
    assert lines[0] == {"repo": pin.repo, "revision": pin.revision, "path": "/hf/hub"}
    # Starts at what was cached; the total is the pin's; ends at what is there.
    assert lines[1] == {"repo": pin.repo, "bytes": 30, "total": pin.bytes}
    assert lines[-1] == {"repo": pin.repo, "bytes": 100, "total": pin.bytes}


def test_an_unpinned_model_loads_main_and_its_size_is_the_total(
    hub: Callable[..., FakeHub],
) -> None:
    fake = hub()
    written: list[str] = []
    models.snapshot("org/m", None, write=written.append)
    assert {c["revision"] for c in fake.calls} == {None}
    assert _lines(written)[1:] == [
        {"repo": "org/m", "bytes": 30, "total": 100},
        {"repo": "org/m", "bytes": 100, "total": 100},
    ]


def test_without_write_there_is_no_dry_run_and_no_report(hub: Callable[..., FakeHub]) -> None:
    """How the engines load: from the cache, at the pinned revision."""
    fake = hub()
    models.snapshot(models.PINS[0].repo, ["config.json"])
    assert len(fake.calls) == 1 and "dry_run" not in fake.calls[0]
    assert fake.calls[0]["revision"] == models.PINS[0].revision


def test_offline_the_download_goes_to_the_cache_without_bytes(
    hub: Callable[..., FakeHub],
) -> None:
    fake = hub(offline=True)
    written: list[str] = []
    models.snapshot("org/m", None, write=written.append)
    assert [c.get("dry_run", False) for c in fake.calls] == [True, False]
    assert [list(line) for line in _lines(written)] == [["repo", "revision", "path"]]


def test_the_models_step_fetches_what_each_engine_names_then_loads_it(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An engine with download_patterns() is fetched first; one without
    (the fakes, a plugin) downloads in its load(); one that is not
    installed is fetched whole."""
    fetched: list[tuple[str, list[str] | None]] = []
    monkeypatch.setattr(models, "cache_dir", lambda: "/hf/hub")
    monkeypatch.setattr(
        models, "snapshot", lambda repo, patterns, write: fetched.append((repo, patterns))
    )
    config = tmp_path / "config.toml"
    config.write_text(
        '[stt]\nengine = "fake"\nmodel = "org/stt"\n[tts]\nengine = "nope"\nmodel = "org/tts"\n'
    )
    models.download(config, None)
    assert fetched == [("org/tts", None)]

    loaded: list[str] = []

    class Patterned:
        name = "patterned"

        def __init__(self, model: str) -> None:
            self.model_id = model

        @classmethod
        def download_patterns(cls) -> list[str]:
            return ["*.json"]

        async def load(self) -> None:
            loaded.append(self.model_id)

    from voiced.engines import registry

    monkeypatch.setattr(registry, "get_stt_engine", lambda name: Patterned)
    config.write_text('[stt]\nmodel = "org/stt"\n[tts]\nengine = "fake"\n')
    fetched.clear()
    models.download(config, None)
    assert fetched == [("org/stt", ["*.json"])]
    assert loaded == ["org/stt"]


@pytest.mark.model
def test_the_pins_are_the_sizes_of_what_the_engines_load() -> None:
    """Network only: the hub's file sizes at each pinned revision."""
    from huggingface_hub import HfApi
    from huggingface_hub.utils import filter_repo_objects

    engines = {models.PINS[0].repo: ParakeetEngine, models.PINS[1].repo: KokoroEngine}
    for pin in models.PINS:
        info = HfApi().model_info(pin.repo, revision=pin.revision, files_metadata=True)
        sizes = {f.rfilename: f.size or 0 for f in info.siblings or []}
        patterns = models.download_patterns(engines[pin.repo])
        wanted = filter_repo_objects(list(sizes), allow_patterns=patterns)
        assert sum(sizes[name] for name in wanted) == pin.bytes, pin.repo


@pytest.mark.model
def test_a_real_download_reports_its_bytes(tmp_path: Path) -> None:
    """Network only, into an empty cache: one small file of a pinned model,
    counted through huggingface_hub's own bars."""
    from huggingface_hub import snapshot_download

    pin = models.PINS[1]
    report = models.ByteReport(pin.repo, 0, pin.bytes, lambda line: None)
    path = snapshot_download(
        pin.repo,
        revision=pin.revision,
        allow_patterns=["config.json"],
        cache_dir=tmp_path / "hub",
        tqdm_class=models._counting_tqdm(report),
    )
    assert report.bytes == (Path(path) / "config.json").stat().st_size > 0
