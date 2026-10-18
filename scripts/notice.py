"""Writes NOTICE: what the menu bar app ships and what an install puts on the Mac.

    uv run python scripts/notice.py            # rewrite NOTICE (asks PyPI for licences)
    uv run python scripts/notice.py --check    # exit 1 if NOTICE does not list the lock

The package list is ``src/voiced/requirements.lock`` as uv installs it on an
Apple Silicon Mac with the app's CPython (ADR 0018), so NOTICE cannot drift
from the lock: ``tests/test_notice.py`` runs the check. Licences come from
each release's PyPI metadata (``license_expression``, else a short
``license``, else the licence classifiers); a package with none of these is
named in ``LICENCE_OVERRIDES`` after reading its source, and the script
refuses to write NOTICE until it is.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from packaging.markers import Marker
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "src" / "voiced" / "requirements.lock"
NOTICE = ROOT / "NOTICE"

# The Mac the app installs on: CPython 3.12 (the manifest's patch version) on
# Apple Silicon. Only the lock entries whose markers hold here get installed.
TARGET = {
    "implementation_name": "cpython",
    "platform_machine": "arm64",
    "platform_python_implementation": "CPython",
    "platform_system": "Darwin",
    "python_full_version": "3.12.15",
    "python_version": "3.12",
    "sys_platform": "darwin",
    "os_name": "posix",
}

# Packages whose PyPI metadata names no licence, from their source repositories.
LICENCE_OVERRIDES = {
    "espeakng-loader": "MIT",
    "kokoro-onnx": "MIT",
}


@dataclass(frozen=True)
class Component:
    """Code that is not a Python package of its own: what the app bundle carries, the
    Python uv installs, and the native programs inside some wheels. ``carrier`` is the
    locked package that brings it, if any; it is listed only while that package is."""

    name: str
    licence: str
    source: str
    where: str
    carrier: str | None = None


COMPONENTS = [
    Component(
        "uv",
        "MIT OR Apache-2.0",
        "https://github.com/astral-sh/uv",
        "in the app bundle (Contents/Helpers/uv)",
    ),
    Component(
        "CPython (python-build-standalone)",
        "PSF-2.0, with the licences of the libraries it links (listed in its distribution)",
        "https://github.com/astral-sh/python-build-standalone",
        "installed by uv into ~/.voiced/python",
    ),
    Component(
        "espeak-ng (library and data)",
        "GPL-3.0-or-later",
        "https://github.com/espeak-ng/espeak-ng",
        "inside the espeakng-loader wheel",
        carrier="espeakng-loader",
    ),
    Component(
        "ffmpeg (static GPL build with x264, x265, LAME, Opus and others)",
        "GPL-2.0-or-later",
        "https://ffmpeg.org/download.html",
        "inside the imageio-ffmpeg wheel (the Python package itself is BSD-2-Clause)",
        carrier="imageio-ffmpeg",
    ),
]

HEADER = """\
voiced is released under the MIT licence (LICENSE). It uses the third-party
software below, under the licences given. The menu bar app carries only
voiced and uv. Everything else is downloaded from PyPI and the Python
releases by your own Mac when voiced is installed; it is not part of voiced
and voiced does not distribute it. Each entry links to its source.
"""

_REQUIREMENT = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)\s*(?:;\s*([^\\]+?))?\s*\\?$")
_PACKAGE_LINE = re.compile(r"^  (\S+)==(\S+)  ")


def installed(lock: Path = LOCK) -> list[tuple[str, str]]:
    """``(name, version)`` for every lock entry installed on the target Mac, by name."""
    found = []
    for line in lock.read_text().splitlines():
        match = _REQUIREMENT.match(line)
        if match is None:
            continue
        name, version, marker = match.groups()
        if marker is None or Marker(marker).evaluate(dict(TARGET)):
            found.append((canonicalize_name(name), version))
    return sorted(found)


def components(packages: list[tuple[str, str]]) -> list[Component]:
    names = {name for name, _ in packages}
    return [c for c in COMPONENTS if c.carrier is None or c.carrier in names]


def licence(name: str, version: str) -> str:
    if name in LICENCE_OVERRIDES:
        return LICENCE_OVERRIDES[name]
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    with urllib.request.urlopen(url, timeout=30) as response:
        info = json.load(response)["info"]
    expression = (info.get("license_expression") or "").strip()
    if expression:
        return str(expression)
    text = (info.get("license") or "").strip()
    if text and "\n" not in text and len(text) <= 60:
        return text
    prefix = "License :: OSI Approved :: "
    classifiers = [
        c.removeprefix(prefix) for c in info.get("classifiers", []) if c.startswith(prefix)
    ]
    if classifiers:
        return " / ".join(classifiers)
    raise SystemExit(f"{name} {version}: no licence on PyPI; add it to LICENCE_OVERRIDES")


def render(packages: list[tuple[str, str]], licences: dict[str, str]) -> str:
    lines = [HEADER]
    lines.append("Programs and data\n")
    for c in components(packages):
        lines += [f"  {c.name}", f"    {c.licence}", f"    {c.where}", f"    {c.source}"]
    lines.append("\nPython packages (src/voiced/requirements.lock)\n")
    for name, version in packages:
        source = f"https://pypi.org/project/{name}/{version}/"
        lines.append(f"  {name}=={version}  {licences[name]}  {source}")
    return "\n".join(lines) + "\n"


def listed(notice: str) -> list[tuple[str, str]]:
    """The ``(name, version)`` pairs a NOTICE lists."""
    found = []
    for line in notice.splitlines():
        match = _PACKAGE_LINE.match(line)
        if match is not None:
            found.append((match[1], match[2]))
    return found


def check(notice: str, packages: list[tuple[str, str]]) -> list[str]:
    """What NOTICE gets wrong about the lock, empty when nothing."""
    problems = []
    have = listed(notice)
    for name, version in sorted(set(packages) - set(have)):
        problems.append(f"missing {name}=={version}")
    for name, version in sorted(set(have) - set(packages)):
        problems.append(f"not in the lock: {name}=={version}")
    for c in components(packages):
        if f"  {c.name}\n" not in notice:
            problems.append(f"missing {c.name}")
    for c in COMPONENTS:
        if c not in components(packages) and f"  {c.name}\n" in notice:
            problems.append(f"{c.name} listed, but {c.carrier} is not in the lock")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only check NOTICE against the lock")
    args = parser.parse_args(argv)
    packages = installed()
    if args.check:
        problems = check(NOTICE.read_text() if NOTICE.exists() else "", packages)
        for problem in problems:
            print(f"NOTICE: {problem}", file=sys.stderr)
        if problems:
            print("Run: uv run python scripts/notice.py", file=sys.stderr)
        return 1 if problems else 0
    licences = {name: licence(name, version) for name, version in packages}
    NOTICE.write_text(render(packages, licences))
    return 0


if __name__ == "__main__":
    sys.exit(main())
