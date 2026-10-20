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
