"""The Mac the app installs on, and what ``requirements.lock`` installs there.

Shared by ``scripts/notice.py`` (NOTICE lists what an install downloads) and
``scripts/app_manifest.py`` (the bundle's download size): an Apple Silicon Mac
running the CPython patch version installs use, which
``menubar/app-python-version`` holds for these scripts and bundle.sh, on the
app's floor, macOS 14, or on another of :data:`MACOS_VERSIONS`.
"""

from __future__ import annotations

import re
from pathlib import Path

from packaging.markers import Marker
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "src" / "voiced" / "requirements.lock"
PYTHON_VERSION_FILE = ROOT / "menubar" / "app-python-version"
MACOS = (14, 0)
DARWIN = {MACOS: 23, (15, 0): 24}
"""The macOS versions whose lock entries differ, each with its Darwin major,
which ``platform_release`` markers read: moonshine-voice installs on macOS 15
and later only."""
MACOS_VERSIONS = tuple(DARWIN)

_REQUIREMENT = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)\s*(?:;\s*([^\\]+?))?\s*\\?$")


def python_version(path: Path = PYTHON_VERSION_FILE) -> str:
    """The patch version, such as ``3.12.15``."""
    version = path.read_text().strip()
    if not re.fullmatch(r"3\.\d+\.\d+", version):
        raise SystemExit(f"{path}: {version!r} is not a CPython patch version such as 3.12.15")
    return version


def environment(python: str, macos: tuple[int, int] = MACOS) -> dict[str, str]:
    """PEP 508 marker values on the target Mac, which runs ``macos``."""
    major, minor = python.split(".")[:2]
    return {
        "platform_release": f"{DARWIN[macos]}.0.0",
        "implementation_name": "cpython",
        "platform_machine": "arm64",
        "platform_python_implementation": "CPython",
        "platform_system": "Darwin",
        "python_full_version": python,
        "python_version": f"{major}.{minor}",
        "sys_platform": "darwin",
        "os_name": "posix",
    }


def installed(
    lock: Path = LOCK, python: str | None = None, macos: tuple[int, int] = MACOS
) -> list[tuple[str, str]]:
    """``(name, version)`` for every lock entry installed on the target Mac, by name."""
    values = environment(python or python_version(), macos)
    found = []
    for line in lock.read_text().splitlines():
        match = _REQUIREMENT.match(line)
        if match is None:
            continue
        name, version, marker = match.groups()
        if marker is None or Marker(marker).evaluate(dict(values)):
            found.append((canonicalize_name(name), version))
    return sorted(found)
