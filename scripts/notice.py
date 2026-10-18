"""Writes NOTICE: what the menu bar app ships and what an install puts on the Mac.

    uv run python scripts/notice.py            # rewrite NOTICE (asks PyPI for licences)
    uv run python scripts/notice.py --check    # exit 1 if NOTICE does not list the lock

The package list is ``src/voiced/requirements.lock`` as uv installs it on the
Mac the app targets (``lock_target``), so NOTICE cannot drift from the lock:
``tests/test_notice.py`` runs the check. Licences are SPDX expressions, from
each release's PyPI metadata: ``license_expression``, else a short
``license`` or the licence classifiers mapped through ``SPDX_NAMES``. A
package whose metadata is missing or ambiguous ("BSD") is named in
``LICENCE_OVERRIDES`` after reading its source, and the script refuses to
write NOTICE until it is.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from dataclasses import dataclass

from lock_target import ROOT, installed

NOTICE = ROOT / "NOTICE"

# From each package's source repository: PyPI names no licence, or an ambiguous one.
LICENCE_OVERRIDES = {
    "espeakng-loader": "MIT",
    "kokoro-onnx": "MIT",
    "numba": "BSD-2-Clause",
    "safetensors": "Apache-2.0",
    "scipy": "BSD-3-Clause",
    "tokenizers": "Apache-2.0",
}

# Free-text licences and classifiers that name exactly one SPDX licence.
SPDX_NAMES = {
    "3-Clause BSD License": "BSD-3-Clause",
    "Apache 2.0": "Apache-2.0",
    "Apache 2.0 License": "Apache-2.0",
    "BSD 3-Clause License": "BSD-3-Clause",
    "GNU General Public License v3 or later (GPLv3+)": "GPL-3.0-or-later",
    "ISC License": "ISC",
    "ISC License (ISCL)": "ISC",
    "MIT License": "MIT",
}

# The SPDX identifiers NOTICE may use. A new one is added here after checking it
# against https://spdx.org/licenses/.
SPDX_IDS = {
    "0BSD", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "CC0-1.0", "CNRI-Python",
    "GPL-2.0-or-later", "GPL-3.0-or-later", "ISC", "LGPL-2.1-or-later", "LLVM-exception",
    "MIT", "MIT-0", "MPL-2.0", "PSF-2.0", "Zlib",
}  # fmt: skip


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
        "PSF-2.0",
        "https://github.com/astral-sh/python-build-standalone",
        "installed by uv into ~/.voiced/python; the libraries it links list their own licences",
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

_PACKAGE_LINE = re.compile(r"^  (\S+)==(\S+)  ")


def is_spdx(expression: str) -> bool:
    tokens = [t for t in re.split(r"\s+|[()]", expression) if t]
    ids = [t for t in tokens if t not in {"AND", "OR", "WITH"}]
    return bool(ids) and all(t in SPDX_IDS for t in ids)


def components(packages: list[tuple[str, str]]) -> list[Component]:
    names = {name for name, _ in packages}
    return [c for c in COMPONENTS if c.carrier is None or c.carrier in names]


def licence(name: str, version: str) -> str:
    if name in LICENCE_OVERRIDES:
        return LICENCE_OVERRIDES[name]
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    with urllib.request.urlopen(url, timeout=30) as response:
        info = json.load(response)["info"]
    found = (info.get("license_expression") or "").strip()
    text = (info.get("license") or "").strip()
    if not found and text and "\n" not in text and len(text) <= 60:
        found = SPDX_NAMES.get(text, text)
    if not found:
        prefix = "License :: OSI Approved :: "
        classifiers = [
            SPDX_NAMES.get(c.removeprefix(prefix), c.removeprefix(prefix))
            for c in info.get("classifiers", [])
            if c.startswith(prefix)
        ]
        found = " OR ".join(classifiers)
    if not is_spdx(found):
        raise SystemExit(
            f"{name} {version}: licence {found or 'missing'!r} is not a known SPDX expression; "
            "check its source and add it to LICENCE_OVERRIDES (or the identifier to SPDX_IDS)"
        )
    return found


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
