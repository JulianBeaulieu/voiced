"""``models.lock.json``: each model's pinned revision, files, sizes and hashes."""

from __future__ import annotations

import functools
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from voiced.models.catalogue import DEFAULT_ENGINES
from voiced.models.lookup import default_model, required

# The lock ----------------------------------------------------------------

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


def read_lock(path: Path = LOCK_FILE) -> dict[str, Locked]:
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return {
        model_id: Locked(
            model_id,
            entry["repo"],
            entry["revision"],
            tuple(entry["patterns"]),
            {name: LockedFile(f["bytes"], f["sha256"]) for name, f in entry["files"].items()},
            tuple(entry.get("previous", ())),
        )
        for model_id, entry in data.get("models", {}).items()
    }


def write_lock(lock: dict[str, Locked], path: Path = LOCK_FILE) -> None:
    data = {
        "comment": f"Written by `{PIN_COMMAND}`; see voiced/models.py.",
        "models": {model_id: lock[model_id].to_json() for model_id in sorted(lock)},
    }
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
    """Every ``(repo, revision)`` voiced pins or pinned, which is what
    voiced may delete from the cache."""
    return {(e.repo, rev) for e in lock().values() for rev in (e.revision, *e.previous)}


def manifest() -> dict[str, Any]:
    """The default engines' models, which ``voiced install`` downloads."""
    ids = [m for name in DEFAULT_ENGINES.values() for m in required(default_model(name).id)]
    return {
        "models": [
            {"repo": e.repo, "revision": e.revision, "bytes": e.bytes}
            for e in (lock()[m] for m in ids)
        ]
    }
