"""``python -m voiced.models pin``: a maintainer's command that writes the lock."""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from voiced.models.catalogue import Model
from voiced.models.lockfile import LOCK_FILE, Locked, LockedFile, read_lock, write_lock
from voiced.models.lookup import catalogued

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
    """``model`` locked at ``revision`` of its repository (default: its
    current pin, else ``main``), resolved to a commit with the catalogue's
    patterns. A replaced revision is added to ``previous``."""
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
    return Locked(model.id, model.repo, hub.commit, model.patterns, locked_files, tuple(previous))


def stale(lock: dict[str, Locked]) -> list[str]:
    """Catalogued models the lock lacks or locked from another repository
    or with other patterns."""
    return [
        m.id
        for m in catalogued()
        if (e := lock.get(m.id)) is None or (e.repo, e.patterns) != (m.repo, m.patterns)
    ]


def pin(models: Iterable[str], revision: str | None = None, path: Path = LOCK_FILE) -> list[str]:
    """Update the lock at ``path`` for ``models`` (none: every stale one,
    and models the catalogue dropped are removed). Returns what changed."""
    named = list(models)
    current = read_lock(path)
    known = {m.id: m for m in catalogued()}
    targets = named or stale(current)
    unknown = [m for m in targets if m not in known]
    if unknown:
        raise SystemExit(f"not in the catalogue: {', '.join(unknown)}; add it to CATALOGUE first")
    changed = []
    for model_id in targets:
        current[model_id] = pin_repo(known[model_id], current.get(model_id), revision)
        changed.append(model_id)
    if not named:
        for model_id in sorted(set(current) - set(known)):
            del current[model_id]
            changed.append(model_id)
    write_lock(current, path)
    return changed
