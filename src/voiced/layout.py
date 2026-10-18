"""The per-user state directory every way of installing voiced shares (ADR 0017).

::

    <state dir>/             [install] venv_dir, ~/.voiced by default
      bin/voiced             symlink to the active environment's bin/voiced
      envs/<id>/             environments built by `voiced install --source` (or the app)
      python/                uv-managed Pythons those environments run on
      control.sock           the watchdog's control socket (ADR 0013)
      install.json           the manifest: which environment is active, and that
                             the directory is voiced's

The plists run ``<state dir>/bin/voiced`` whatever the environment is, so
switching between Homebrew, a source build and the app is repointing one
symlink. A ``<state dir>`` that is itself voiced's virtual environment
(installs before ADR 0017) is adopted as the environment until the first
switch.

Every rule about what in the directory is voiced's lives here, once:
:func:`claim` for install and uninstall alike, :func:`legacy_venv_paths`,
:func:`built_environments`. Standard library only: the watchdog imports
this.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import shlex
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

MANIFEST = "install.json"
ENVS = "envs"
PYTHONS = "python"
# A build in progress (ADR 0018 builds into envs/<id>.partial, then renames).
PARTIAL_SUFFIX = ".partial"
Origin = Literal["cli", "homebrew", "app"]
ORIGINS: tuple[Origin, ...] = ("cli", "homebrew", "app")

# What a virtual environment has at its top level, for the legacy layout
# where the state directory is one. ``bin`` is not listed: its ``voiced`` is
# the entry point, and only the rest of ``bin`` belongs to the environment.
VENV_ENTRIES = ("lib", "include", "share", "pyvenv.cfg", ".gitignore", "CACHEDIR.TAG", ".lock")
# Files a state directory may hold without a manifest: what a watchdog that
# ran before any install leaves, and Finder's.
UNCLAIMED_OK = frozenset({"control.sock", ".DS_Store"})


def entry_point(state_dir: Path) -> Path:
    """What the plists run."""
    return state_dir / "bin" / "voiced"


def linked_environment(entry: Path) -> Path:
    """The environment ``entry`` runs, resolved: the directory above the
    ``bin/voiced`` it leads to."""
    return Path(os.path.realpath(entry)).parent.parent


def environment_python(entry: Path) -> Path:
    """The interpreter of the environment ``entry`` runs.

    ``bin/python`` next to the resolved console script, not resolved itself:
    Python finds its venv from the path it was started as. Falls back to
    this interpreter while nothing is installed.
    """
    if not entry.exists():
        return Path(sys.executable)
    return linked_environment(entry) / "bin" / "python"


def voiced_dist_info(environment: Path) -> Path | None:
    """voiced's ``.dist-info`` in ``environment``, if it is installed there."""
    found = sorted(environment.glob("lib/python3*/site-packages/voiced-*.dist-info"))
    return found[0] if found else None


def environment_version(environment: Path) -> str:
    """voiced's version in ``environment``, from its dist-info; "" if absent."""
    info = voiced_dist_info(environment)
    return "" if info is None else info.name.removeprefix("voiced-").removesuffix(".dist-info")


def stable_environment(prefix: str | Path) -> tuple[Path, Origin]:
    """The path to link for the environment at ``prefix``, and who made it.

    Homebrew builds voiced in ``<prefix>/Cellar/voiced/<version>/libexec``,
    a path every upgrade replaces; ``<prefix>/opt/voiced/libexec`` follows
    the current keg. That mapping, and :func:`homebrew_busy`, are the only
    Homebrew-specific rules.
    """
    path = Path(prefix)
    parts = path.parts
    if len(parts) >= 5 and parts[-1] == "libexec" and parts[-4:-2] == ("Cellar", "voiced"):
        return Path(*parts[:-4], "opt", "voiced", "libexec"), "homebrew"
    return path, "cli"


def is_legacy_venv(state_dir: Path) -> bool:
    """Whether ``state_dir`` is voiced's own venv from before ADR 0017: a
    venv with voiced installed in it, not editable (``voiced install``
    always made a regular install; a checkout's ``.venv`` from ``uv sync``
    has an editable one). Any other venv is someone else's."""
    if not (state_dir / "pyvenv.cfg").is_file():
        return False
    info = voiced_dist_info(state_dir)
    if info is None:
        return False
    try:
        direct = json.loads((info / "direct_url.json").read_text())
    except (OSError, ValueError):
        return True  # installed by name, not from a directory
    dir_info = direct.get("dir_info") if isinstance(direct, dict) else None
    return not (isinstance(dir_info, dict) and dir_info.get("editable"))


def legacy_venv_paths(state_dir: Path) -> list[Path]:
    """A legacy venv's files: its top-level entries and the rest of ``bin``,
    never the entry point."""
    entry = entry_point(state_dir)
    paths = [state_dir / name for name in VENV_ENTRIES]
    paths += sorted(p for p in entry.parent.glob("*") if p != entry and not p.is_dir())
    return [p for p in paths if p.exists() or p.is_symlink()]


def environment_dirs(state_dir: Path) -> list[Path]:
    """What is in ``envs/``; nothing if ``envs`` is a symlink, which leads
    somewhere that is not voiced's to prune or uninstall."""
    envs = state_dir / ENVS
    return [] if envs.is_symlink() else sorted(envs.glob("*"))


def built_environments(state_dir: Path) -> list[Path]:
    """The environments voiced built in ``envs/``: complete venvs, not a
    build still in progress."""
    return [
        env
        for env in environment_dirs(state_dir)
        if (env / "pyvenv.cfg").is_file() and not env.name.endswith(PARTIAL_SUFFIX)
    ]


def partial_environments(state_dir: Path) -> list[Path]:
    """Builds in ``envs/`` that never finished (``envs/<id>.partial``).
    Never pruned, since a build may still be writing into one; only
    uninstall removes them, holding the install lock the builder holds."""
    return [
        env
        for env in environment_dirs(state_dir)
        if env.name.endswith(PARTIAL_SUFFIX) and env.is_dir() and not env.is_symlink()
    ]


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class Manifest:
    """``install.json``. ``environment`` is None between claiming the
    directory and linking the first environment."""

    source: Origin
    environment: Path | None
    version: str = ""
    lock_sha256: str = ""
    python: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "environment": None if self.environment is None else str(self.environment),
            "version": self.version,
            "lock_sha256": self.lock_sha256,
            "python": self.python,
        }

    @classmethod
    def from_json(cls, data: Any) -> Manifest:
        if not isinstance(data, dict) or data.get("source") not in ORIGINS:
            raise ManifestError("not a voiced manifest")
        env = data.get("environment")
        if env is not None and not isinstance(env, str):
            raise ManifestError("environment is not a path")
        return cls(
            source=data["source"],
            environment=None if env is None else Path(env),
            version=str(data.get("version", "")),
            lock_sha256=str(data.get("lock_sha256", "")),
            python=str(data.get("python", "")),
        )


MAX_MANIFEST_BYTES = 64 * 1024


def _read_manifest_bytes(path: Path) -> bytes:
    """At most :data:`MAX_MANIFEST_BYTES` (one more, so a longer file fails
    to parse), opened without blocking: the watchdog reads it on its event
    loop for every ``hello``, so a FIFO or a huge file must not stall it. A
    FIFO without a writer reads empty, a directory fails to read."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        return os.read(fd, MAX_MANIFEST_BYTES + 1)
    finally:
        os.close(fd)


def read_manifest(state_dir: Path) -> Manifest | None:
    """The manifest, or None when there is none. Raises :class:`ManifestError`
    for one that cannot be read."""
    path = state_dir / MANIFEST
    try:
        data = _read_manifest_bytes(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ManifestError(f"{path}: {exc}") from exc
    try:
        return Manifest.from_json(json.loads(data))  # not UTF-8 is a ValueError too
    except (ValueError, ManifestError) as exc:
        raise ManifestError(f"{path}: {exc}") from exc


def installed_manifest(state_dir: Path) -> dict[str, Any] | None:
    """``install.json`` for the watchdog's ``hello``: None when there is none
    or it cannot be read."""
    try:
        manifest = read_manifest(state_dir)
    except ManifestError:
        return None
    return None if manifest is None else manifest.to_json()


def write_manifest(state_dir: Path, manifest: Manifest) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = state_dir / f".{MANIFEST}.tmp"
    tmp.write_text(json.dumps(manifest.to_json(), indent=2) + "\n")
    tmp.replace(state_dir / MANIFEST)


class NotVoiceds(ValueError):
    """The state directory holds someone else's files."""


class SymlinkedStateDir(NotVoiceds):
    """The state directory is a symlink: ``real`` is where it leads, the
    path to pass instead."""

    def __init__(self, link: Path) -> None:
        self.real = link.resolve()
        super().__init__(
            f"it is a symlink to {self.real}; pass the real path instead: "
            f"--state-dir {shlex.quote(str(self.real))} (or [install] venv_dir in the config)"
        )


def claim(state_dir: Path) -> Manifest | None:
    """What voiced may do with ``state_dir``, for install and uninstall alike.

    Returns its manifest; for a legacy venv one that adopts the directory as
    its own environment; None when it is missing or holds only
    :data:`UNCLAIMED_OK` files (voiced's to use, nothing installed). Raises
    :class:`NotVoiceds` for anything else, and :class:`ManifestError` for a
    manifest that cannot be read.

    A ``state_dir`` that is itself a symlink is refused: whatever it points
    at, uninstall would delete inside the target, which is not voiced's
    path.
    """
    if state_dir.is_symlink():
        raise SymlinkedStateDir(state_dir)
    manifest = read_manifest(state_dir)
    if manifest is not None:
        return manifest
    if is_legacy_venv(state_dir):
        return Manifest(source="cli", environment=state_dir)
    if not state_dir.is_dir():
        return None
    others = sorted(p.name for p in state_dir.iterdir() if p.name not in UNCLAIMED_OK)
    if others:
        shown = ", ".join(others[:5]) + (", ..." if len(others) > 5 else "")
        raise NotVoiceds(f"it is not empty and has no {MANIFEST}: {shown}")
    return None


def same_path(a: Path, b: Path) -> bool:
    """``a`` and ``b`` are the same directory, through any symlinks."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.realpath(a) == os.path.realpath(b)


def link_entry_point(state_dir: Path, environment: Path) -> None:
    """Point ``bin/voiced`` at ``environment``'s, atomically (a running
    daemon's next start sees either the old target or the new one). Refuses
    a target that is the entry point itself (a legacy venv reached through
    another path)."""
    entry = entry_point(state_dir)
    target = environment / "bin" / "voiced"
    if same_path(target.parent, entry.parent):
        raise ValueError(f"{target} is {entry} itself")
    entry.parent.mkdir(parents=True, exist_ok=True)
    tmp = entry.with_name(f".{entry.name}.tmp")
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(target)
    tmp.replace(entry)


@contextlib.contextmanager
def install_lock(state_dir: Path, waiting: Callable[[], None] = lambda: None) -> Iterator[None]:
    """Hold an exclusive ``flock`` on the state directory itself while
    installing or uninstalling, so that the app and the CLI never prune or
    relink under each other. No lock file: one in a directory that turns
    out not to be voiced's would be litter. ``state_dir`` must exist.
    ``waiting`` is called once if another holder makes this one wait."""
    fd = os.open(state_dir, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            waiting()
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # releases the lock


# The running environment, for the watchdog ----------------------------------


def read_link(path: Path) -> str | None:
    """Where the symlink ``path`` points, or None if it is not one (the
    entry point of a legacy venv is a plain script)."""
    try:
        return os.readlink(path)
    except OSError:
        return None


def homebrew_lock(path: Path) -> Path | None:
    """Homebrew's lock file for the formula whose keg holds ``path``
    (``<prefix>/Cellar/<name>/<version>/...`` ->
    ``<prefix>/var/homebrew/locks/<name>.formula.lock``), or None."""
    parts = path.parts
    if "Cellar" not in parts:
        return None
    i = parts.index("Cellar")
    if len(parts) < i + 3:
        return None
    return Path(*parts[:i], "var", "homebrew", "locks", f"{parts[i + 1]}.formula.lock")


def homebrew_busy(paths: tuple[Path, ...]) -> str | None:
    """The formula Homebrew is installing among those ``paths`` belong to,
    or None. Homebrew holds an exclusive ``flock`` on the formula's lock
    file for the whole install, reinstall or upgrade; a shared one that
    cannot be had means it still runs. It is released at once, so Homebrew
    starting in that instant is the only collision."""
    for path in paths:
        lock = homebrew_lock(path)
        if lock is None:
            continue
        try:
            fd = os.open(lock, os.O_RDONLY)
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return lock.name.removesuffix(".formula.lock")
        finally:
            os.close(fd)
    return None


@dataclass(frozen=True)
class RunningEnvironment:
    """The files this process runs from, as they were when it started.

    Two things replace them. Files vanish or are replaced: ``brew cleanup``
    deletes the old keg (by default at the end of ``brew upgrade``),
    ``brew reinstall`` moves the keg aside and builds a new one at the same
    path, a python@3.12 upgrade deletes the old interpreter, and ``voiced
    install`` prunes an old ``envs/<id>``. Or the entry point is repointed:
    ``voiced install`` and the app's switch. The process keeps answering
    ``/health`` from memory, but lazy imports, ``sys.executable`` and the
    engine catalogue fail; :meth:`replaced` notices, and the watchdog
    restarts everything from the new environment (ADR 0017).

    The files are resolved at start (Homebrew's venv reaches Python through
    ``opt/python@3.12``, which an upgrade repoints) and compared by inode.
    For Homebrew the entry point's link is always ``opt/voiced/...`` and
    never changes; its target is deliberately not followed: Homebrew
    repoints ``opt`` before it has rewritten and re-signed the new keg's
    extension modules, and a process that maps one of them in that window
    makes macOS kill or hang every later process that loads it. For the
    same reason nothing moves while Homebrew still holds the lock of a
    formula these files belong to (:func:`homebrew_busy`).
    """

    files: tuple[tuple[Path, int | None], ...]
    entry: Path
    entry_link: str | None

    @classmethod
    def current(cls, entry: Path) -> RunningEnvironment:
        paths = (sys.prefix, sys.base_prefix, sys.executable)
        return cls.of(tuple(Path(os.path.realpath(p)) for p in paths), entry)

    @classmethod
    def of(cls, paths: tuple[Path, ...], entry: Path) -> RunningEnvironment:
        return cls(
            files=tuple((p, inode(p)) for p in paths), entry=entry, entry_link=read_link(entry)
        )

    def replaced(self) -> str | None:
        """Why this process no longer runs the installed environment, or None."""
        for path, ino in self.files:
            now = inode(path)
            if now is None:
                return f"{path} no longer exists"
            if now != ino:
                return f"{path} was replaced"
        link = read_link(self.entry)
        if link != self.entry_link:
            return f"{self.entry} now points at {link}, not {self.entry_link}"
        return None

    def busy(self) -> str | None:
        """The Homebrew formula still being installed under these files."""
        return homebrew_busy(tuple(p for p, _ in self.files))


def inode(path: Path) -> int | None:
    try:
        return path.stat().st_ino
    except OSError:
        return None
