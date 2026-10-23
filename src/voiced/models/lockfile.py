"""``models.lock.json``: each model's pinned revision, files, sizes and hashes.

The hashes are not checked when loading (``huggingface_hub`` checks sizes,
and so does :func:`voiced.models.missing_files`); they are there for a
mirror that serves the files outside the Hub (ADR 0022, "Later").
``retired`` keeps the revisions of models the catalogue dropped, so
uninstall still deletes what an earlier release downloaded.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from voiced.models.catalogue import CATALOGUE, Model, engine_label
from voiced.models.lookup import default_models

LOCK_FILE = Path(__file__).with_name("models.lock.json")
PIN_COMMAND = "python -m voiced.models pin"


@dataclass(frozen=True)
class LockedFile:
    bytes: int
    sha256: str


@dataclass(frozen=True)
class Locked:
    """One model in ``models.lock.json``."""

    id: str
    repo: str
    revision: str  # a commit SHA
    patterns: tuple[str, ...]
    files: dict[str, LockedFile]
    previous: tuple[str, ...] = ()  # earlier pins, oldest first

    @property
    def bytes(self) -> int:
        return sum(f.bytes for f in self.files.values())

    @property
    def revisions(self) -> tuple[str, ...]:
        """The pin, then the earlier ones: every revision voiced may delete."""
        return (self.revision, *self.previous)

    def to_json(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "revision": self.revision,
            "patterns": list(self.patterns),
            "files": {
                name: {"bytes": f.bytes, "sha256": f.sha256}
                for name, f in sorted(self.files.items())
            },
            "previous": list(self.previous),
        }


Retired = set[tuple[str, str]]
"""``(repo, revision)`` of models no longer in the catalogue."""


def _read(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def read_lock(path: Path = LOCK_FILE) -> dict[str, Locked]:
    """The lock at ``path``; a missing file is an error (``FileNotFoundError``),
    never an empty lock that would load every model unpinned."""
    return {
        model_id: Locked(
            model_id,
            entry["repo"],
            entry["revision"],
            tuple(entry["patterns"]),
            {name: LockedFile(f["bytes"], f["sha256"]) for name, f in entry["files"].items()},
            tuple(entry.get("previous", ())),
        )
        for model_id, entry in _read(path)["models"].items()
    }


def read_retired(path: Path = LOCK_FILE) -> Retired:
    return {(r["repo"], r["revision"]) for r in _read(path).get("retired", [])}


def write_lock(
    lock: dict[str, Locked], path: Path = LOCK_FILE, retired: Iterable[tuple[str, str]] = ()
) -> None:
    data: dict[str, Any] = {
        "comment": f"Written by `{PIN_COMMAND}`; see voiced/models/catalogue.py.",
        "models": {model_id: lock[model_id].to_json() for model_id in sorted(lock)},
    }
    if retired:
        data["retired"] = [{"repo": r, "revision": rev} for r, rev in sorted(set(retired))]
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


@functools.cache
def lock() -> dict[str, Locked]:
    """The shipped lock, read once."""
    return read_lock()


def locked(model_id: str) -> Locked | None:
    return lock().get(model_id)


def pinned_revision(model_id: str) -> str | None:
    """``model_id``'s pinned commit, or None (its ``main``)."""
    entry = locked(model_id)
    return entry.revision if entry is not None else None


def pinned_revisions() -> set[tuple[str, str]]:
    """Every ``(repo, revision)`` voiced pins or pinned, retired models'
    included, which is what voiced may delete from the cache."""
    pins = {(e.repo, rev) for e in lock().values() for rev in e.revisions}
    return pins | read_retired()


@dataclass(frozen=True)
class Source:
    """Where a model's files come from: its repository and, when pinned
    (``entry``), the commit and the patterns; a model outside the catalogue
    is a repository fetched whole at ``main``."""

    repo: str
    entry: Locked | None

    @property
    def revision(self) -> str | None:
        return self.entry.revision if self.entry is not None else None

    @property
    def patterns(self) -> list[str] | None:
        return list(self.entry.patterns) if self.entry is not None else None


def resolve(model_id: str) -> Source:
    entry = locked(model_id)
    return Source(entry.repo if entry is not None else model_id, entry)


def model_view(model: Model, *, recommended: bool) -> dict[str, Any]:
    """What ``voiced models list --json`` says of a catalogued model that
    the cache does not change: its catalogue entry and its pin."""
    entry = lock()[model.id]
    return {
        "id": model.id,
        "repo": model.repo,
        "license": model.license,
        "url": model.url,
        "gated": model.gated,
        "revision": entry.revision,
        "download_bytes": entry.bytes,
        "quality": model.quality,
        "speed": model.speed,
        "note": model.note,
        "recommended": recommended,
    }


def catalogue_view() -> dict[str, Any]:
    """Every catalogued engine and model as ``voiced models list --json``
    shows them, without what only an installed voiced knows (the cache, the
    installed engines): the menu bar app's first-run setup offers these
    before anything is installed (ADR 0018, "Amendment (2026-10-23)")."""
    defaults = {m.id for m in default_models()}
    return {
        "engines": [
            {
                "name": engine.name,
                "label": engine_label(engine.name),
                "kind": engine.kind,
                "models": [model_view(m, recommended=m.id in defaults) for m in engine.models],
                "shared": [model_view(m, recommended=False) for m in engine.shared],
            }
            for engine in CATALOGUE
        ]
    }


def manifest() -> dict[str, Any]:
    """What ``python -m voiced.models`` prints for the app's
    install-manifest.json: the catalogue, the recommended models included."""
    return {"catalogue": catalogue_view()}
