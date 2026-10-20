"""The Hugging Face cache: what is there, downloading into it, deleting from it."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar

from voiced.launchd.progress import DOWNLOAD_PREFIX
from voiced.models.errors import ModelNotDownloaded, ModelsError, classify
from voiced.models.lockfile import Locked, Source, locked, resolve
from voiced.models.lookup import find, is_shared, required

T = TypeVar("T")

DEFAULT_HF_HOME = Path.home() / ".cache" / "huggingface"
"""Where Hugging Face keeps its cache without ``HF_HOME``, ignoring
``XDG_CACHE_HOME`` so voiced and its services always agree."""

# The cache on disk (standard library) ------------------------------------

DISK_HEADROOM = 10**9
"""Free space a download leaves: the bytes still to fetch plus 1 GB."""


def hub_dir(hf_home: Path | None) -> Path:
    """The Hugging Face cache under ``[install] hf_home``, or the default one
    (``voiced.config.apply_engine_environment`` sets ``HF_HOME`` to the same,
    so ``huggingface_hub`` downloads where this looks)."""
    return (hf_home or DEFAULT_HF_HOME) / "hub"


def repo_folder(repo: str) -> str:
    """A model repository's folder in the Hugging Face cache."""
    return f"models--{repo.replace('/', '--')}"


def snapshot_dir(hub: Path, repo: str, revision: str) -> Path:
    return hub / repo_folder(repo) / "snapshots" / revision


def missing_files(hub: Path, entry: Locked, revision: str | None = None) -> list[str]:
    """The locked files of ``entry`` (at ``revision``, its pin by default)
    that the cache at ``hub`` lacks or holds at another size: each is a link
    into the blob store, checked by ``stat``, so a broken link or a
    truncated file is missing too."""
    root = snapshot_dir(hub, entry.repo, revision or entry.revision)
    missing = []
    for name, locked_file in entry.files.items():
        try:
            size = os.stat(root / name).st_size
        except OSError:
            size = -1
        if size != locked_file.bytes:
            missing.append(name)
    return missing


def not_downloaded(hub: Path, model_id: str) -> list[str]:
    """The models a load of the catalogued ``model_id`` needs that are not
    complete at their pins; empty for a model outside the catalogue."""
    if find(model_id) is None:
        return []
    return [m for m in required(model_id) if (e := locked(m)) is None or missing_files(hub, e)]


def remaining_bytes(hub: Path, model_ids: list[str]) -> int:
    """What is still to fetch of ``model_ids`` (0 for an unpinned one, whose size is unknown)."""
    total = 0
    for model_id in model_ids:
        entry = locked(model_id)
        if entry is not None:
            total += sum(entry.files[name].bytes for name in missing_files(hub, entry))
    return total


def check_disk(hub: Path, needed: int) -> None:
    existing = next(p for p in (hub, *hub.parents) if p.exists())
    free = shutil.disk_usage(existing).free
    if free < needed + DISK_HEADROOM:
        raise ModelsError(
            "disk",
            f"not enough disk space in {hub}: the download needs {size_text(needed)} "
            f"and 1 GB to spare, {size_text(free)} is free",
        )


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
    ``done`` is what was cached before the download (``repo`` names the
    model by its id); the bar's updates come from several download threads."""

    def __init__(
        self,
        repo: str,
        done: int,
        total: int,
        write: Write,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.repo, self.bytes, self.total = repo, done, total
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
            self.bytes = self.total
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


def fetch(model_id: str, write: Write | None = None) -> list[Path]:
    """Download ``model_id`` and the shared models it needs, after checking
    the disk has room for what is missing and 1 GB more: ``voiced models
    download`` and the install's models step. A shared model alone is
    refused; a model outside the catalogue is fetched whole at ``main``.
    Raises :class:`ModelsError`."""
    if is_shared(model_id):
        raise ModelsError(
            "failed", f"{model_id} comes with its engine's models; download one of them"
        )
    needed = required(model_id)
    hub = Path(cache_dir())
    check_disk(hub, remaining_bytes(hub, needed))
    return [snapshot(m, write=write) for m in needed]


def snapshot(model_id: str, *, check_only: bool = False, write: Write | None = None) -> Path:
    """The local snapshot of ``model_id``'s repository at its pinned revision
    (a model outside the catalogue: the repository ``model_id`` at ``main``).
    An existing directory is a local model and is returned as it is.

    ``check_only`` (how the engines load): nothing is fetched, and a locked
    file missing from the cache, or a model outside the catalogue not cached
    at all, raises :class:`ModelNotDownloaded`. Otherwise what the lock's
    patterns name (everything, unpinned) that is not cached is downloaded;
    with ``write``, the download's progress goes there (:class:`ByteReport`).
    A failed download raises :class:`ModelsError` with its code."""
    local = Path(model_id).expanduser()
    if local.is_dir():
        return local
    if check_only:
        return _cached(model_id)
    source = resolve(model_id)
    try:
        return _download(model_id, source, write)
    except ModelsError:
        raise
    except Exception as exc:
        code, message = classify(exc, source.repo)
        raise ModelsError(code, message) from exc


def _cached(model_id: str) -> Path:
    source = resolve(model_id)
    if source.entry is None:
        from huggingface_hub import snapshot_download
        from huggingface_hub.errors import LocalEntryNotFoundError

        try:
            return Path(snapshot_download(source.repo, local_files_only=True))
        except LocalEntryNotFoundError as exc:
            raise ModelNotDownloaded(model_id) from exc
    hub = Path(cache_dir())
    if missing_files(hub, source.entry):
        raise ModelNotDownloaded(model_id, source.revision)
    return snapshot_dir(hub, source.repo, source.entry.revision)


def _download(model_id: str, source: Source, write: Write | None) -> Path:
    """Progress lines name the model by its id. A pinned model's progress
    starts at what the lock says is cached; only an unpinned one asks the
    hub (a dry run) what it holds."""
    from huggingface_hub import snapshot_download

    report = None
    if write is not None:
        start = {
            "kind": "model",
            "repo": model_id,
            "revision": source.revision,
            "path": cache_dir(),
        }
        write(DOWNLOAD_PREFIX + json.dumps(start))
        report = _report(model_id, source, write)
    path = snapshot_download(
        source.repo,
        revision=source.revision,
        allow_patterns=source.patterns,
        tqdm_class=_counting_tqdm(report) if report is not None else None,
    )
    if report is not None:
        report.finish()
    return Path(path)


def _report(model_id: str, source: Source, write: Write) -> ByteReport | None:
    if source.entry is not None:
        total = source.entry.bytes
        done = total - remaining_bytes(Path(cache_dir()), [model_id])
    else:
        from huggingface_hub import snapshot_download
        from huggingface_hub.errors import DryRunError

        try:
            files = snapshot_download(source.repo, dry_run=True)
        except DryRunError:  # offline: the download finds the cache or says why it cannot
            return None
        total = sum(f.file_size or 0 for f in files)
        done = sum(f.file_size or 0 for f in files if not f.will_download)
    report = ByteReport(model_id, done, total, write)
    report.add(0)
    return report


def load_pinned(loader: Callable[..., T], model_id: str) -> T:
    """``loader(repo, revision=..., allow_patterns=...)`` for a model at its
    pin, after checking it is downloaded (:class:`ModelNotDownloaded`): for
    engines whose library takes a repository id rather than a folder
    (mlx-audio's ``load_model``, which tells a model by its name). Under
    ``HF_HUB_OFFLINE`` its own resolve is a cache hit."""
    snapshot(model_id, check_only=True)
    source = resolve(model_id)
    return loader(source.repo, revision=source.revision, allow_patterns=source.patterns)


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
