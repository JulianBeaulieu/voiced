"""A Hugging Face cache laid out as ``huggingface_hub`` 2.x lays it out.

File bytes live once in the cache-wide store, ``hub/blobs/<2 hex>/<xet
hash>``, with a ``.refs`` manifest naming every repository blob that links
to them; a repository's ``blobs/<etag>`` is a relative symlink into the
store, and ``snapshots/<revision>/<file>`` a relative symlink to that.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

MARKER = ".huggingface-shared-blobs"


def repo_dir(hub: Path, repo: str) -> Path:
    return hub / f"models--{repo.replace('/', '--')}"


def store_path(hub: Path, data: bytes) -> Path:
    xet = hashlib.sha256(data).hexdigest()
    return hub / "blobs" / xet[:2] / xet


def add_revision(
    hub: Path, repo: str, revision: str, files: dict[str, bytes], ref: str | None = None
) -> Path:
    """Cache ``files`` as ``repo`` at ``revision``; returns the snapshot."""
    (hub / "blobs").mkdir(parents=True, exist_ok=True)
    (hub / "blobs" / MARKER).write_text("1\n")
    folder = repo_dir(hub, repo)
    snapshot = folder / "snapshots" / revision
    snapshot.mkdir(parents=True, exist_ok=True)
    (folder / "blobs").mkdir(exist_ok=True)
    for name, data in files.items():
        store = store_path(hub, data)
        if not store.exists():
            store.parent.mkdir(exist_ok=True)
            store.write_bytes(data)
        etag = hashlib.sha1(data).hexdigest()
        blob = folder / "blobs" / etag
        if not blob.is_symlink():
            blob.symlink_to(os.path.relpath(store, blob.parent))
            with open(f"{store}.refs", "a") as refs:
                refs.write(f"{folder.name}/blobs/{etag}\n")
        entry = snapshot / name
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.symlink_to(os.path.relpath(blob, entry.parent))
    if ref is not None:
        (folder / "refs").mkdir(exist_ok=True)
        (folder / "refs" / ref).write_text(revision)
    return snapshot


def du(path: Path) -> int:
    """Bytes of the regular files under ``path``, not following links."""
    return sum(p.lstat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())


def add_locked(hub: Path, entry: Any, revision: str | None = None, ref: str | None = None) -> Path:
    """Cache a ``voiced.models.Locked`` model with every file at its locked
    size: sparse files, so a 2.5 GB model costs nothing on disk."""
    rev = revision or entry.revision
    files = {name: f"{entry.id}/{name}/{rev}".encode() for name in entry.files}
    snapshot = add_revision(hub, entry.repo, rev, files, ref=ref)
    for name, locked in entry.files.items():
        os.truncate(snapshot / name, locked.bytes)
    return snapshot
