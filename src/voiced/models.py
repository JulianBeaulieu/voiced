"""The models voiced knows, pinned per release, and their download (ADR 0022).

Two parts, in the relation of ``pyproject.toml`` to ``uv.lock``:

* :data:`CATALOGUE`, written by hand: each engine voiced ships, its models
  (the first is the default) and the repositories every model of it needs
  (``shared``), each with its licence, whether it is gated, and the files
  the engine loads (``patterns``).
* ``models.lock.json`` beside this module, written by ``python -m
  voiced.models pin [REPO ...]`` (a maintainer's command, which needs the
  network): per repository the commit, the patterns it was resolved with,
  each file's size and sha256, and ``previous``, the revisions earlier
  releases pinned, so uninstall still knows them. A test fails, naming the
  command, when the two disagree.

Every catalogued engine loads its model from :func:`snapshot` with
``check_only``: the services never download (they run with
``HF_HUB_OFFLINE``), and a missing file is :class:`ModelNotDownloaded`. A
model the catalogue does not know loads its ``main`` revision, as before.

``python -m voiced.models`` prints the default models as JSON (the app's
``install-manifest.json`` is written from it); ``python -m voiced.models
download --config FILE [--report]`` is the models step of ``voiced
install``, run in the new environment: it fetches each configured model and
loads each engine once. With ``--report`` it also writes
:data:`voiced.launchd.progress.DOWNLOAD_PREFIX` lines that the installer
turns into ``download`` events.

Standard library only at import (the app's bundle script runs this from the
unpacked wheel, and the watchdog checks settings with it); the download and
the cache scans import ``huggingface_hub``.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import importlib
import json
import os
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from voiced.launchd.progress import DOWNLOAD_PREFIX

# The catalogue -----------------------------------------------------------


@dataclass(frozen=True)
class Model:
    """A Hugging Face model repository and the files voiced loads from it."""

    repo: str
    license: str
    patterns: tuple[str, ...]
    gated: bool = False  # needs a token and acceptance of its terms

    @property
    def url(self) -> str:
        return f"https://huggingface.co/{self.repo}"


@dataclass(frozen=True)
class Engine:
    """``name`` is the entry point name ``engine =`` uses in the config;
    ``kind`` its registry group. ``shared`` repositories are downloaded with
    any of its models and deleted with the last."""

    name: str
    kind: Literal["stt", "tts"]
    models: tuple[Model, ...]
    shared: tuple[Model, ...] = ()


PHONON = ("config.json", "tokenizer.json", "model.q8.gguf", "default-voice.safetensors")
"""Phonon's config, tokenizer, q8 weights and default voice; not the other weight formats."""

PHONON_VOICES = (
    "NbpkqMVS3CJeq2j8",
    "NIWIOv5vTi5eRMj8",
    "vjFyp2BcCyTlUmYb",
    "4SZHfMpw-p46Ywgs",
    "bXwrkHhURL1VFJdG",
    "nGzMHMPAWGaJFHOo",
    "LMDIYE0Oa377Qh6Z",
    "40QrMVgAxFiUnP9f",
    "0zLApsZfVLLNaEGl",
    "_6Aslh2DxfmnRLmP",
    "qIxlZ7Ei8KvWlyQH",
    "d9XCyhjo4JhkHF1d",
    "6PWnV0Nq4wu7RVBT",
    "GgfEkEJtxZR7gnpy",
    "uem82D50GRv2Dwma",
    "y9bQqIDnyxwqK01t",
    "gDT1nz7Ie36ZhL-C",
    "m9MxpY6IUGXtWp76",
    "X6NceqF285Gr04gb",
    "YyKcxPG7iIgS3jsK",
    "zUCxzrOrugTSis03",
    "JOW0vchyswkQUMDS",
    "dME3IWyZBvmh1n1q",
    "Zn7g6NxLqXlJb7Qi",
    "gfP44RlHwr2kEwk7",
    "xPDpsV3F592srkjA",
    "nOykTmcH9ydsGGIc",
    "lX3LavMNdlIbgEHH",
    "dGyGDKo2osj7h3s7",
    "cXoIXJ2llS8S0WOW",
    "GXaSqMEmOg12OIYb",
    "xO8xQo3BrB8FuI6J",
    "cYtsRSxV5q9IMehn",
    "gSgG5Tor0GPEnqzC",
    "NIWoEurJHMJjyUTe",
    "y2eiKxtQo2B67deU",
    "QXNr0SvkukLB4wPq",
)
"""The English presets of ``gradium/phonon-voices`` that Phonon loads, by file
id (``voiced.engines.phonon`` names them; a test keeps the two in step)."""

CATALOGUE: tuple[Engine, ...] = (
    Engine(
        "parakeet",
        "stt",
        # What parakeet-mlx's from_pretrained reads.
        models=(
            Model(
                "mlx-community/parakeet-tdt-0.6b-v3",
                "CC-BY-4.0",
                ("config.json", "model.safetensors"),
            ),
        ),
    ),
    Engine(
        "kokoro",
        "tts",
        # The config and the weights, with the voice packs in voices/; not
        # the sample WAVs or the .pt copies.
        models=(
            Model("mlx-community/Kokoro-82M-bf16", "Apache-2.0", ("config.json", "*.safetensors")),
        ),
    ),
    Engine(
        "soprano",
        "tts",
        # The configs, the tokenizer and the weights; not the README.
        models=(
            Model("mlx-community/Soprano-80M-bf16", "Apache-2.0", ("*.json", "*.safetensors")),
        ),
    ),
    Engine(
        "phonon",
        "tts",
        models=(
            Model("gradium/phonon-31m-en", "Apache-2.0", PHONON),
            # Non-commercial, gated. Of the 40M checkpoints, the English one.
            Model("gradium/phonon-40m-en", "CC-BY-NC-4.0", PHONON, gated=True),
        ),
        # The voice catalogue and its presets (ADR 0021).
        shared=(
            Model(
                "gradium/phonon-voices",
                "Apache-2.0",
                ("voices.jsonl", *(f"voices/{v}.safetensors" for v in PHONON_VOICES)),
            ),
        ),
    ),
)

DEFAULT_ENGINES: dict[str, str] = {"stt": "parakeet", "tts": "kokoro"}
"""The engines a new config runs (``voiced.config``'s defaults)."""


def engine(name: str) -> Engine | None:
    return next((e for e in CATALOGUE if e.name == name), None)


def default_model(engine_name: str) -> Model:
    """The first model of a catalogued engine (KeyError if there is none)."""
    found = engine(engine_name)
    if found is None:
        raise KeyError(engine_name)
    return found.models[0]


def find(repo: str) -> tuple[Engine, Model] | None:
    """The catalogued model ``repo`` and its engine; None for a shared
    repository or one the catalogue does not know."""
    for e in CATALOGUE:
        for m in e.models:
            if m.repo == repo:
                return e, m
    return None


def repositories() -> list[Model]:
    """Every catalogued repository, models and shared ones, once each."""
    seen: dict[str, Model] = {}
    for e in CATALOGUE:
        for m in (*e.models, *e.shared):
            seen.setdefault(m.repo, m)
    return list(seen.values())


def required(repo: str) -> list[str]:
    """What a load of ``repo`` needs: the model, then its engine's shared repositories."""
    found = find(repo)
    return [repo, *(s.repo for s in found[0].shared)] if found is not None else [repo]


def license_of(repo: str) -> str | None:
    found = find(repo)
    return found[1].license if found is not None else None


# The lock ----------------------------------------------------------------

LOCK_FILE = Path(__file__).with_name("models.lock.json")
PIN_COMMAND = "python -m voiced.models pin"


@dataclass(frozen=True)
class LockedFile:
    bytes: int
    sha256: str


@dataclass(frozen=True)
class Locked:
    """One repository in ``models.lock.json``."""

    repo: str
    revision: str  # a commit SHA
    patterns: tuple[str, ...]
    files: dict[str, LockedFile]
    previous: tuple[str, ...] = ()  # earlier pins, oldest first

    @property
    def bytes(self) -> int:
        return sum(f.bytes for f in self.files.values())

    def to_json(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "patterns": list(self.patterns),
            "files": {
                name: {"bytes": f.bytes, "sha256": f.sha256}
                for name, f in sorted(self.files.items())
            },
            "previous": list(self.previous),
        }


def read_lock(path: Path = LOCK_FILE) -> dict[str, Locked]:
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return {
        repo: Locked(
            repo,
            entry["revision"],
            tuple(entry["patterns"]),
            {name: LockedFile(f["bytes"], f["sha256"]) for name, f in entry["files"].items()},
            tuple(entry.get("previous", ())),
        )
        for repo, entry in data.get("repos", {}).items()
    }


def write_lock(lock: dict[str, Locked], path: Path = LOCK_FILE) -> None:
    data = {
        "comment": f"Written by `{PIN_COMMAND}`; see voiced/models.py.",
        "repos": {repo: lock[repo].to_json() for repo in sorted(lock)},
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


@functools.cache
def lock() -> dict[str, Locked]:
    """The shipped lock, read once."""
    return read_lock()


def locked(repo: str) -> Locked | None:
    return lock().get(repo)


def pinned_revision(repo: str) -> str | None:
    """``repo``'s pinned commit, or None (its ``main``)."""
    entry = locked(repo)
    return entry.revision if entry is not None else None


def pinned_revisions() -> set[tuple[str, str]]:
    """Every ``(repo, revision)`` voiced pins or pinned, which is what
    voiced may delete from the cache."""
    return {(e.repo, rev) for e in lock().values() for rev in (e.revision, *e.previous)}


def manifest() -> dict[str, Any]:
    """The default engines' models, which ``voiced install`` downloads."""
    repos = [r for name in DEFAULT_ENGINES.values() for r in required(default_model(name).repo)]
    return {
        "models": [
            {"repo": e.repo, "revision": e.revision, "bytes": e.bytes}
            for e in (lock()[r] for r in repos)
        ]
    }


# The cache on disk (standard library) ------------------------------------


def hub_dir(hf_home: Path | None) -> Path:
    """The Hugging Face cache under ``[install] hf_home``, or the default one."""
    return (hf_home or Path.home() / ".cache" / "huggingface") / "hub"


def repo_folder(repo: str) -> str:
    """A model repository's folder in the Hugging Face cache."""
    return f"models--{repo.replace('/', '--')}"


def snapshot_dir(hub: Path, repo: str, revision: str) -> Path:
    return hub / repo_folder(repo) / "snapshots" / revision


def missing_files(hub: Path, entry: Locked, revision: str | None = None) -> list[str]:
    """The locked files of ``entry`` (at ``revision``, its pin by default)
    that the cache at ``hub`` lacks: each is a link into the blob store,
    checked by ``stat``, so a broken link is missing too."""
    root = snapshot_dir(hub, entry.repo, revision or entry.revision)
    missing = []
    for name in entry.files:
        try:
            os.stat(root / name)
        except OSError:
            missing.append(name)
    return missing


def not_downloaded(hub: Path, repo: str) -> list[str]:
    """The repositories a load of the catalogued model ``repo`` needs that
    are not complete at their pins; empty for a model outside the catalogue."""
    if find(repo) is None:
        return []
    return [r for r in required(repo) if (e := locked(r)) is None or missing_files(hub, e)]


class ModelNotDownloaded(LookupError):
    """A model's files are not in the cache. ``revision`` is None for a
    model outside the catalogue, which loads ``main``."""

    def __init__(self, repo: str, revision: str | None = None) -> None:
        self.repo = repo
        self.revision = revision
        self.command = f"voiced models download {repo}"
        at = f" at revision {revision}" if revision else ""
        super().__init__(f"{repo} is not downloaded{at}; run {self.command}")


def hub_missing_model(exc: BaseException, repo: str) -> ModelNotDownloaded | None:
    """``exc``, or what caused it, as :class:`ModelNotDownloaded`: that
    itself, or the hub's offline and not-found errors (``repo`` is the
    model the engine was loading). None for any other failure."""
    try:
        from huggingface_hub.errors import (
            EntryNotFoundError,
            LocalEntryNotFoundError,
            RepositoryNotFoundError,
            RevisionNotFoundError,
        )

        hub_errors: tuple[type[BaseException], ...] = (
            LocalEntryNotFoundError,
            RepositoryNotFoundError,
            RevisionNotFoundError,
            EntryNotFoundError,
        )
    except ImportError:
        hub_errors = ()
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ModelNotDownloaded):
            return current
        if isinstance(current, hub_errors):
            return ModelNotDownloaded(repo, pinned_revision(repo))
        current = current.__cause__ or current.__context__
    return None


# Downloading ---------------------------------------------------------------

REPORT_INTERVAL_S = 0.25
"""At most four byte reports a second."""

BYTES_BAR = "huggingface_hub.snapshot_download"
"""The name ``snapshot_download`` gives its bar of bytes written to disk
(its other byte bar counts network bytes, which deduplication makes fewer)."""

Write = Callable[[str], None]


class ByteReport:
    """Writes ``{"kind": "bytes", "repo", "bytes", "total"}`` lines after
    :data:`DOWNLOAD_PREFIX`, at most every :data:`REPORT_INTERVAL_S`.
    ``done`` is what was cached before the download and ``size`` what it
    adds up to; the bar's updates come from several download threads."""

    def __init__(
        self,
        repo: str,
        done: int,
        size: int,
        total: int,
        write: Write,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.repo, self.bytes, self.size, self.total = repo, done, size, total
        self._write, self._clock = write, clock
        self._lock = threading.Lock()
        self._last: float | None = None

    def add(self, n: int) -> None:
        with self._lock:
            self.bytes += n
            now = self._clock()
            if self._last is None or now - self._last >= REPORT_INTERVAL_S:
                self._last = now
                self._emit()

    def finish(self) -> None:
        with self._lock:
            self.bytes = self.size
            self._emit()

    def _emit(self) -> None:
        fields = {"kind": "bytes", "repo": self.repo, "bytes": self.bytes, "total": self.total}
        line = json.dumps(fields)
        self._write(DOWNLOAD_PREFIX + line)


def _counting_tqdm(report: ByteReport) -> type:
    """``snapshot_download``'s ``tqdm_class``: its bars as usual, and the
    bytes written to disk to ``report``."""
    # huggingface_hub's own tqdm, so its bars behave as without a tqdm_class.
    base: Any = importlib.import_module("huggingface_hub.utils.tqdm").tqdm

    class Counting(base):  # type: ignore[misc]
        def __init__(self, *args: Any, name: str | None = None, **kwargs: Any) -> None:
            super().__init__(*args, name=name, **kwargs)
            self._counts = name == BYTES_BAR

        def update(self, n: float | None = 1) -> bool | None:
            if self._counts and n:
                report.add(int(n))
            result: bool | None = super().update(n)
            return result

    return Counting


def snapshot(repo: str, *, check_only: bool = False, write: Write | None = None) -> Path:
    """The local directory of ``repo`` at its pinned revision (``main`` if
    not pinned). An existing directory is a local model and is returned as
    it is.

    ``check_only`` (how the engines load): nothing is fetched, and a locked
    file missing from the cache, or a model outside the catalogue not cached
    at all, raises :class:`ModelNotDownloaded`. Otherwise what the lock's
    patterns name (everything, unpinned) that is not cached is downloaded;
    with ``write``, the download's progress goes there (:class:`ByteReport`)."""
    local = Path(repo).expanduser()
    if local.is_dir():
        return local
    if check_only:
        return _cached(repo)
    from huggingface_hub.errors import GatedRepoError

    try:
        return _download(repo, write)
    except GatedRepoError as exc:
        raise RuntimeError(
            f"{repo} is gated: accept its licence at https://huggingface.co/{repo}, then "
            "run `hf auth login` with HF_HOME set to the Hugging Face home the services "
            "use, as the user they run as"
        ) from exc


def _cached(repo: str) -> Path:
    entry = locked(repo)
    if entry is None:
        from huggingface_hub import snapshot_download
        from huggingface_hub.errors import LocalEntryNotFoundError

        try:
            return Path(snapshot_download(repo, local_files_only=True))
        except LocalEntryNotFoundError as exc:
            raise ModelNotDownloaded(repo) from exc
    hub = Path(cache_dir())
    if missing_files(hub, entry):
        raise ModelNotDownloaded(repo, entry.revision)
    return snapshot_dir(hub, repo, entry.revision)


def _download(repo: str, write: Write | None) -> Path:
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import DryRunError

    entry = locked(repo)
    revision = entry.revision if entry is not None else None
    patterns = list(entry.patterns) if entry is not None else None
    report = None
    if write is not None:
        start = {"kind": "model", "repo": repo, "revision": revision, "path": cache_dir()}
        write(DOWNLOAD_PREFIX + json.dumps(start))
        # What is cached already, so a resumed download starts at those bytes.
        try:
            files = snapshot_download(
                repo, revision=revision, allow_patterns=patterns, dry_run=True
            )
        except DryRunError:  # offline: the download finds the cache or says why it cannot
            files = None
        if files is not None:
            size = sum(f.file_size or 0 for f in files)
            cached = sum(f.file_size or 0 for f in files if not f.will_download)
            total = entry.bytes if entry is not None else size
            report = ByteReport(repo, cached, size, total, write)
            report.add(0)
    path = snapshot_download(
        repo,
        revision=revision,
        allow_patterns=patterns,
        tqdm_class=_counting_tqdm(report) if report is not None else None,
    )
    if report is not None:
        report.finish()
    return Path(path)


# The cache through huggingface_hub ---------------------------------------


@dataclass(frozen=True, order=True)
class CachedRevision:
    """One revision of a model repository in the Hugging Face cache."""

    repo: str
    revision: str
    bytes: int  # on disk, following the links into the shared blob store
    refs: tuple[str, ...] = ()  # the branches and tags that point at it, such as "main"


@dataclass(frozen=True)
class CacheScan:
    """``revisions``: what the cache holds of the repositories asked for.
    ``broken``: those of them whose folder is there but which the scan
    could not read (a download interrupted before any snapshot, say), so
    nothing of them can be deleted through the library."""

    revisions: list[CachedRevision]
    broken: list[str]


def cached_revisions(hub: Path, repos: set[str]) -> CacheScan:
    """The revisions of ``repos`` (model repositories) that the cache at
    ``hub`` holds, and those of ``repos`` it could not read; nothing when
    there is no cache there."""
    from huggingface_hub import scan_cache_dir
    from huggingface_hub.errors import CacheNotFound

    try:
        info = scan_cache_dir(hub)
    except CacheNotFound:
        return CacheScan([], [])
    revisions = sorted(
        CachedRevision(repo.repo_id, rev.commit_hash, rev.size_on_disk, tuple(sorted(rev.refs)))
        for repo in info.repos
        if repo.repo_type == "model" and repo.repo_id in repos
        for rev in repo.revisions
    )
    # The scan leaves a repository it cannot read out of info.repos.
    read = {repo.repo_id for repo in info.repos if repo.repo_type == "model"}
    broken = sorted(repo for repo in repos - read if (hub / repo_folder(repo)).is_dir())
    return CacheScan(revisions, broken)


def delete_revisions(
    hub: Path, revisions: Sequence[CachedRevision], *, dry_run: bool = False
) -> int:
    """Delete ``revisions`` from the cache at ``hub`` and return the bytes
    freed (with ``dry_run``, that would be freed, deleting nothing).
    Through ``huggingface_hub``: a repository folder holds links into the
    cache-wide blob store, so removing the folder would leave the bytes
    behind. The library removes each revision's snapshot and refs and the
    blobs no other revision or repository uses; other revisions of the same
    repository stay. Only the named repositories are considered, so a
    revision hash another repository shares is not touched there."""
    import dataclasses

    from huggingface_hub import scan_cache_dir

    info = scan_cache_dir(hub)
    repos = {r.repo for r in revisions}
    mine = dataclasses.replace(
        info,
        repos=frozenset(r for r in info.repos if r.repo_type == "model" and r.repo_id in repos),
    )
    strategy = mine.delete_revisions(*(r.revision for r in revisions))
    if not dry_run:
        strategy.execute()
    return int(strategy.expected_freed_size)


def size_text(n: int) -> str:
    """``n`` bytes in decimal units, as the Hugging Face tools show them."""
    for unit, scale in (("GB", 10**9), ("MB", 10**6), ("KB", 10**3)):
        if n >= scale:
            return f"{n / scale:.1f} {unit}"
    return f"{n} bytes"


def cache_dir() -> str:
    """The Hugging Face cache the models go to (``HF_HOME`` as the config sets it)."""
    from huggingface_hub import constants

    return str(constants.HF_HUB_CACHE)


# The models step -------------------------------------------------------------


def download(config: Path, write: Write | None) -> None:
    """The models step: each configured model, then each engine loaded once.
    The engine is built first, so a model it refuses fails before anything
    is fetched. A catalogued engine's model is fetched at its pin with its
    shared repositories, any other model of a catalogued engine whole at
    ``main``; a plugin engine downloads in its own ``load()``, and a model
    whose engine is not installed is fetched whole."""
    import asyncio

    from voiced.config import load_settings
    from voiced.engines.registry import UnknownEngine, create_engine

    settings = load_settings(config)
    print(f"Hugging Face cache: {cache_dir()}", flush=True)
    jobs: tuple[tuple[Literal["stt", "tts"], str, str], ...] = (
        ("stt", settings.stt.engine, settings.stt.model),
        ("tts", settings.tts.engine, settings.tts.model),
    )
    for kind, engine_name, model in jobs:
        revision = pinned_revision(model)
        at = f" revision={revision}" if revision is not None else ""
        print(f"{kind}: engine={engine_name} model={model}{at}", flush=True)
        try:
            built = create_engine(kind, engine_name, model)
        except UnknownEngine as exc:
            print(f"  {exc}; downloading weights only", flush=True)
            snapshot(model, write=write)
            continue
        if engine(engine_name) is not None:
            for repo in required(model):
                snapshot(repo, write=write)
        asyncio.run(built.load())
        print(f"  loaded {built.name} ({built.model_id})", flush=True)


# Pinning (a maintainer's command) -----------------------------------------


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass
class HubFiles:
    """What the hub says of a repository at a revision: its commit and the
    files the patterns name, with their sizes and, for LFS and Xet files,
    their sha256."""

    commit: str
    files: dict[str, tuple[int, str | None]] = field(default_factory=dict)


def hub_files(repo: str, revision: str, patterns: Sequence[str]) -> HubFiles:
    from huggingface_hub import HfApi
    from huggingface_hub.utils import filter_repo_objects  # type: ignore[attr-defined]

    info = HfApi().model_info(repo, revision=revision, files_metadata=True)
    siblings = {f.rfilename: f for f in info.siblings or []}
    wanted = filter_repo_objects(sorted(siblings), allow_patterns=list(patterns))
    found = HubFiles(str(info.sha))
    for name in wanted:
        f = siblings[name]
        found.files[name] = (int(f.size or 0), f.lfs.sha256 if f.lfs else None)
    return found


def fetch_sha256(repo: str, revision: str, name: str) -> str:
    """Hash a small file the hub carries only a git sha1 for, downloaded
    into a scratch cache (not the user's)."""
    from huggingface_hub import hf_hub_download

    with tempfile.TemporaryDirectory() as scratch:
        return _sha256(Path(hf_hub_download(repo, name, revision=revision, cache_dir=scratch)))


def pin_repo(
    model: Model,
    current: Locked | None,
    revision: str | None,
    *,
    files: Callable[[str, str, Sequence[str]], HubFiles] = hub_files,
    sha256: Callable[[str, str, str], str] = fetch_sha256,
) -> Locked:
    """``model`` locked at ``revision`` (default: its current pin, else
    ``main``), resolved to a commit with the catalogue's patterns. A
    replaced revision is added to ``previous``."""
    wanted = revision or (current.revision if current is not None else "main")
    hub = files(model.repo, wanted, model.patterns)
    if not hub.files:
        raise SystemExit(f"{model.repo}@{wanted}: the patterns {model.patterns} name no file")
    locked_files = {
        name: LockedFile(size, digest or sha256(model.repo, hub.commit, name))
        for name, (size, digest) in hub.files.items()
    }
    previous = list(current.previous) if current is not None else []
    if current is not None and current.revision != hub.commit:
        previous.append(current.revision)
    return Locked(model.repo, hub.commit, model.patterns, locked_files, tuple(previous))


def stale(lock: dict[str, Locked]) -> list[str]:
    """Catalogued repositories the lock lacks or locked with other patterns."""
    return [
        m.repo
        for m in repositories()
        if (e := lock.get(m.repo)) is None or e.patterns != m.patterns
    ]


def pin(repos: Iterable[str], revision: str | None = None, path: Path = LOCK_FILE) -> list[str]:
    """Update the lock at ``path`` for ``repos`` (none: every stale one,
    and repositories the catalogue dropped are removed). Returns what changed."""
    named = list(repos)
    current = read_lock(path)
    catalogued = {m.repo: m for m in repositories()}
    targets = named or stale(current)
    unknown = [r for r in targets if r not in catalogued]
    if unknown:
        raise SystemExit(f"not in the catalogue: {', '.join(unknown)}; add it to CATALOGUE first")
    changed = []
    for repo in targets:
        current[repo] = pin_repo(catalogued[repo], current.get(repo), revision)
        changed.append(repo)
    if not named:
        for repo in sorted(set(current) - set(catalogued)):
            del current[repo]
            changed.append(repo)
    write_lock(current, path)
    return changed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m voiced.models",
        description="Print the default models as JSON, download the configured ones, or pin.",
    )
    sub = parser.add_subparsers(dest="command")
    fetch = sub.add_parser("download", help="download the configured models and load each once")
    fetch.add_argument("--config", type=Path, required=True)
    fetch.add_argument("--report", action="store_true", help="write byte progress lines")
    pins = sub.add_parser("pin", help="lock catalogued repositories (needs the network)")
    pins.add_argument("repos", nargs="*", help="default: every one the lock lacks or has stale")
    pins.add_argument("--revision", help="branch, tag or commit (default: the pin, else main)")
    args = parser.parse_args(argv)
    if args.command == "download":
        download(args.config, (lambda line: print(line, flush=True)) if args.report else None)
        return 0
    if args.command == "pin":
        for repo in pin(args.repos, args.revision):
            entry = read_lock().get(repo)
            print(f"{repo} {entry.revision if entry else 'removed'}")
        return 0
    json.dump(manifest(), sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
