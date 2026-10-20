"""The default models, pinned per release, and their download (ADR 0018).

:data:`PINS` is the one place a default model's revision is set: the engines
load that revision, ``voiced install`` downloads it, and the app's
``install-manifest.json`` is written from it::

    python -m voiced.models            # {"models": [{"repo", "revision", "bytes"}, ...]}

``bytes`` is the size of the files the engine's ``download_patterns()``
names at that revision, so a model repository changing upstream changes
nothing installed. A model that is not pinned (one the config names) loads
its ``main`` revision, as before.

:data:`OPTIONAL_PINS` fixes the revision of models an engine loads that
are not installed by default (Phonon's, ADR 0021): the engine loads that
revision and ``download`` fetches it when the config names it, but the
manifest leaves it out.

An engine may declare a ``download_patterns()`` classmethod returning the
Hugging Face file patterns it loads, and an ``extra_downloads()`` classmethod
naming other repositories it loads from, each with its patterns. Like
``catalogue_voices`` they are not part of the frozen interface in
``voiced.engines.base``: an engine without them downloads in its own
``load()``.

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
import shlex
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
    gated: bool = False  # needs its licence accepted and a Hugging Face token


PINS: tuple[Pin, ...] = (
    # config.json and model.safetensors
    Pin(
        "mlx-community/parakeet-tdt-0.6b-v3", "ed2b7e8c15f9aaa0b5772e2efb986255eaef7e15", 2508532829
    ),
    # config.json, kokoro-v1_0.safetensors and the 54 voices/*.safetensors
    Pin("mlx-community/Kokoro-82M-bf16", "a71e4d38b236d968966a2002c4c895dbd12b1c3c", 355322783),
)

OPTIONAL_PINS: tuple[Pin, ...] = (
    # config.json, tokenizer.json, model.q8.gguf and default-voice.safetensors,
    # at the revisions ptts 0.4.0 names as its defaults
    Pin("gradium/phonon-31m-en", "8c64b55ec301728e2d1695bfea87a08a267d0c2d", 65049306),
    # voices.jsonl and the 37 English voices/*.safetensors
    Pin("gradium/phonon-voices", "da6c9ec0d47be8df5c87c730a3aa97187f3229c3", 9510439),
    # The 40M checkpoints: CC BY-NC 4.0, gated
    Pin("gradium/phonon-40m-en", "9b95393286eaeeebf1330b2ae90a14a2074c415d", 75090522, gated=True),
    Pin(
        "gradium/phonon-40m-multilingual",
        "1267486cbcb55d2eaa61ba521c2ed8e4d22923c2",
        75090344,
        gated=True,
    ),
)

REPORT_INTERVAL_S = 0.25
"""At most four byte reports a second."""

BYTES_BAR = "huggingface_hub.snapshot_download"
"""The name ``snapshot_download`` gives its bar of bytes written to disk
(its other byte bar counts network bytes, which deduplication makes fewer)."""


def pin(repo: str) -> Pin | None:
    return next((p for p in (*PINS, *OPTIONAL_PINS) if p.repo == repo), None)


def pinned_revision(repo: str) -> str | None:
    """``repo``'s pinned commit, or None (its ``main``)."""
    pinned = pin(repo)
    return pinned.revision if pinned is not None else None


def pinned_revisions() -> set[tuple[str, str]]:
    """Every ``(repo, revision)`` voiced pins or pinned, which is what
    ``voiced uninstall --remove-models`` may delete."""
    return {(p.repo, rev) for p in (*PINS, *OPTIONAL_PINS) for rev in (p.revision, *p.previous)}


def manifest() -> dict[str, Any]:
    return {"models": [{"repo": p.repo, "revision": p.revision, "bytes": p.bytes} for p in PINS]}


def download_patterns(engine: type) -> list[str] | None:
    """The files ``engine`` loads, or None when it does not say."""
    declared = getattr(engine, "download_patterns", None)
    return list(declared()) if callable(declared) else None


def extra_downloads(engine: type) -> dict[str, list[str]]:
    """Other repositories ``engine`` loads from, with their file patterns."""
    declared = getattr(engine, "extra_downloads", None)
    return dict(declared()) if callable(declared) else {}


def login_hint(repo: str, hf_home: Path) -> str:
    """What to do when ``repo`` is gated and the services cannot read it.
    The command is huggingface_hub's, from voiced's environment when this
    runs in it."""
    found = Path(sys.executable).parent / "hf"
    hf = shlex.quote(str(found)) if found.is_file() else "hf"
    return (
        f"{repo} is gated: accept its licence at https://huggingface.co/{repo}, then, as "
        f"the user the services run as, run `HF_HOME={shlex.quote(str(hf_home))} {hf} auth "
        f"login`, which saves the token to {hf_home / 'token'}"
    )


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
    from huggingface_hub.errors import GatedRepoError

    try:
        return _snapshot(repo, patterns, write)
    except GatedRepoError as exc:
        from huggingface_hub import constants

        raise RuntimeError(login_hint(repo, Path(constants.HF_HOME))) from exc


def _snapshot(repo: str, patterns: list[str] | None, write: Write | None) -> Path:
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
    broken = sorted(
        repo for repo in repos - read if (hub / f"models--{repo.replace('/', '--')}").is_dir()
    )
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
        for repo, extra in extra_downloads(cls).items():
            snapshot(repo, extra, write=write)
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
