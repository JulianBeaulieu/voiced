"""The Hugging Face cache: what is there, downloading into it, deleting from it."""

from __future__ import annotations

import importlib
import json
import os
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from voiced.launchd.progress import DOWNLOAD_PREFIX
from voiced.models.lockfile import Locked, locked, pinned_revision
from voiced.models.lookup import find, required

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


def not_downloaded(hub: Path, model_id: str) -> list[str]:
    """The models a load of the catalogued ``model_id`` needs that are not
    complete at their pins; empty for a model outside the catalogue."""
    if find(model_id) is None:
        return []
    return [m for m in required(model_id) if (e := locked(m)) is None or missing_files(hub, e)]


class ModelNotDownloaded(LookupError):
    """A model's files are not in the cache. ``repo`` is the model's id
    (its repository for every model so far); ``revision`` is None for a
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


def snapshot(model_id: str, *, check_only: bool = False, write: Write | None = None) -> Path:
    """The local snapshot of ``model_id``'s repository at its pinned revision
    (a model outside the catalogue: the repository ``model_id`` at ``main``).
    An existing directory is a local model and is returned as it is.

    ``check_only`` (how the engines load): nothing is fetched, and a locked
    file missing from the cache, or a model outside the catalogue not cached
    at all, raises :class:`ModelNotDownloaded`. Otherwise what the lock's
    patterns name (everything, unpinned) that is not cached is downloaded;
    with ``write``, the download's progress goes there (:class:`ByteReport`)."""
    local = Path(model_id).expanduser()
    if local.is_dir():
        return local
    if check_only:
        return _cached(model_id)
    from huggingface_hub.errors import GatedRepoError

    entry = locked(model_id)
    repo = entry.repo if entry is not None else model_id
    try:
        return _download(model_id, write)
    except GatedRepoError as exc:
        raise RuntimeError(
            f"{repo} is gated: accept its licence at https://huggingface.co/{repo}, then "
            "run `hf auth login` with HF_HOME set to the Hugging Face home the services "
            "use, as the user they run as"
        ) from exc


def _cached(model_id: str) -> Path:
    entry = locked(model_id)
    if entry is None:
        from huggingface_hub import snapshot_download
        from huggingface_hub.errors import LocalEntryNotFoundError

        try:
            return Path(snapshot_download(model_id, local_files_only=True))
        except LocalEntryNotFoundError as exc:
            raise ModelNotDownloaded(model_id) from exc
    hub = Path(cache_dir())
    if missing_files(hub, entry):
        raise ModelNotDownloaded(model_id, entry.revision)
    return snapshot_dir(hub, entry.repo, entry.revision)


def _download(model_id: str, write: Write | None) -> Path:
    """Progress lines name the model by its id."""
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import DryRunError

    entry = locked(model_id)
    repo = entry.repo if entry is not None else model_id
    revision = entry.revision if entry is not None else None
    patterns = list(entry.patterns) if entry is not None else None
    report = None
    if write is not None:
        start = {"kind": "model", "repo": model_id, "revision": revision, "path": cache_dir()}
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
            report = ByteReport(model_id, cached, size, total, write)
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
