"""The default models, pinned per release, and their download (ADR 0018).

:data:`PINS` is the one place a default model's revision is set: the engines
load that revision, ``voiced install`` downloads it, and the app's
``install-manifest.json`` is written from it::

    python -m voiced.models            # {"models": [{"repo", "revision", "bytes"}, ...]}

``bytes`` is the size of the files the engine's ``download_patterns()``
names at that revision, so a model repository changing upstream changes
nothing installed. A model that is not pinned (one the config names) loads
its ``main`` revision, as before.

An engine may declare a ``download_patterns()`` classmethod returning the
Hugging Face file patterns it loads. Like ``catalogue_voices`` it is not
part of the frozen interface in ``voiced.engines.base``: an engine without
it downloads in its own ``load()``.

``python -m voiced.models download --config FILE [--report]`` is the
models step of ``voiced install``, run in the new environment: it fetches
each configured model and loads each engine once. With ``--report`` it also
writes :data:`voiced.launchd.progress.DOWNLOAD_PREFIX` lines that the
installer turns into ``download`` events.

Standard library only at import (the app's bundle script runs this from the
unpacked wheel); the download imports the engines and ``huggingface_hub``.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from voiced.launchd.progress import DOWNLOAD_PREFIX


@dataclass(frozen=True)
class Pin:
    repo: str
    revision: str  # a commit SHA
    bytes: int  # the files download_patterns() names at that revision
    # The revisions earlier releases pinned, newest first: re-pinning moves
    # the old revision here, so uninstall still knows it is voiced's.
    previous: tuple[str, ...] = ()


PINS: tuple[Pin, ...] = (
    # config.json and model.safetensors
    Pin(
        "mlx-community/parakeet-tdt-0.6b-v3", "ed2b7e8c15f9aaa0b5772e2efb986255eaef7e15", 2508532829
    ),
    # config.json, kokoro-v1_0.safetensors and the 54 voices/*.safetensors
    Pin("mlx-community/Kokoro-82M-bf16", "a71e4d38b236d968966a2002c4c895dbd12b1c3c", 355322783),
)

REPORT_INTERVAL_S = 0.25
"""At most four byte reports a second."""

BYTES_BAR = "huggingface_hub.snapshot_download"
"""The name ``snapshot_download`` gives its bar of bytes written to disk
(its other byte bar counts network bytes, which deduplication makes fewer)."""


def pin(repo: str) -> Pin | None:
    return next((p for p in PINS if p.repo == repo), None)


def pinned_revision(repo: str) -> str | None:
    """``repo``'s pinned commit, or None (its ``main``)."""
    pinned = pin(repo)
    return pinned.revision if pinned is not None else None


def pinned_revisions() -> set[tuple[str, str]]:
    """Every ``(repo, revision)`` voiced pins or pinned, which is what
    ``voiced uninstall --remove-models`` may delete."""
    return {(p.repo, rev) for p in PINS for rev in (p.revision, *p.previous)}


def manifest() -> dict[str, Any]:
    return {"models": [{"repo": p.repo, "revision": p.revision, "bytes": p.bytes} for p in PINS]}


def download_patterns(engine: type) -> list[str] | None:
    """The files ``engine`` loads, or None when it does not say."""
    declared = getattr(engine, "download_patterns", None)
    return list(declared()) if callable(declared) else None


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


def snapshot(repo: str, patterns: list[str] | None, *, write: Write | None = None) -> Path:
    """The local directory of ``repo`` at its pinned revision (``main`` if
    not pinned), downloading what ``patterns`` name that is not cached. An
    existing directory is a local model and is returned as it is. With
    ``write``, the download's progress goes there (:class:`ByteReport`)."""
    local = Path(repo).expanduser()
    if local.is_dir():
        return local
    from huggingface_hub import snapshot_download
    from huggingface_hub.errors import DryRunError

    pinned = pin(repo)
    revision = pinned.revision if pinned is not None else None
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
            total = pinned.bytes if pinned is not None else size
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


@dataclass(frozen=True, order=True)
class CachedRevision:
    repo: str
    revision: str
    bytes: int  # on disk, following the links into the shared blob store


def cached_revisions(hub: Path, repos: set[str]) -> list[CachedRevision]:
    """The revisions of ``repos`` (model repositories) that the cache at
    ``hub`` holds; nothing when there is no cache there."""
    from huggingface_hub import scan_cache_dir
    from huggingface_hub.errors import CacheNotFound

    try:
        info = scan_cache_dir(hub)
    except CacheNotFound:
        return []
    return sorted(
        CachedRevision(repo.repo_id, rev.commit_hash, rev.size_on_disk)
        for repo in info.repos
        if repo.repo_type == "model" and repo.repo_id in repos
        for rev in repo.revisions
    )


def delete_revisions(hub: Path, revisions: Sequence[CachedRevision]) -> int:
    """Delete ``revisions`` from the cache at ``hub`` and return the bytes
    freed. Through ``huggingface_hub``: a repository folder holds links into
    the cache-wide blob store, so removing the folder would leave the bytes
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


def download(config: Path, write: Write | None) -> None:
    """The models step: each configured model, then each engine loaded once.
    An engine without ``download_patterns()`` downloads in its ``load()``;
    a model whose engine is not installed is fetched whole."""
    import asyncio

    from voiced.config import load_settings
    from voiced.engines.registry import UnknownEngine, get_stt_engine, get_tts_engine

    settings = load_settings(config)
    print(f"Hugging Face cache: {cache_dir()}", flush=True)
    jobs: tuple[tuple[str, str, str, Callable[[str], Any]], ...] = (
        ("stt", settings.stt.engine, settings.stt.model, get_stt_engine),
        ("tts", settings.tts.engine, settings.tts.model, get_tts_engine),
    )
    for service, engine_name, model, getter in jobs:
        revision = pinned_revision(model)
        at = f" revision={revision}" if revision is not None else ""
        print(f"{service}: engine={engine_name} model={model}{at}", flush=True)
        try:
            cls = getter(engine_name)
        except UnknownEngine as exc:
            print(f"  {exc}; downloading weights only", flush=True)
            snapshot(model, None, write=write)
            continue
        patterns = download_patterns(cls)
        if patterns is not None:
            snapshot(model, patterns, write=write)
        engine = cls(model)
        asyncio.run(engine.load())
        print(f"  loaded {engine.name} ({engine.model_id})", flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m voiced.models",
        description="Print the pinned default models as JSON, or download the configured ones.",
    )
    sub = parser.add_subparsers(dest="command")
    fetch = sub.add_parser("download", help="download the configured models and load each once")
    fetch.add_argument("--config", type=Path, required=True)
    fetch.add_argument("--report", action="store_true", help="write byte progress lines")
    args = parser.parse_args(argv)
    if args.command == "download":
        download(args.config, (lambda line: print(line, flush=True)) if args.report else None)
        return 0
    json.dump(manifest(), sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
