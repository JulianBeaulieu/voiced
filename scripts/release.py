"""Helpers for .github/workflows/release.yml (ADR 0018, "Signing and release CI").

    python scripts/release.py version
    python scripts/release.py notes VERSION
    python scripts/release.py formula --url URL --sha256 SHA
    python scripts/release.py cask --version VERSION --sha256 SHA [--adhoc]

``version`` prints ``version`` from pyproject.toml. ``notes`` prints the
CHANGELOG section headed VERSION (``## [0.1.0] - 2026-10-19`` or
``## 0.1.0``), the release notes, and fails when there is none. ``formula``
prints packaging/homebrew/voiced.rb with ``url`` and ``sha256`` set to the
tag's source archive. ``cask`` prints packaging/homebrew/cask/voiced.rb with
``version`` and ``sha256`` set to the app's zip; ``--adhoc`` points it at the
ad hoc asset and marks it as not for the tap. Standard library only: the
workflow runs it with whatever Python the runner has.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"
FORMULA = ROOT / "packaging" / "homebrew" / "voiced.rb"
CASK = ROOT / "packaging" / "homebrew" / "cask" / "voiced.rb"

SHA256 = re.compile(r"[0-9a-f]{64}")
# The zip asset names: what the cask downloads, and the ad hoc build that says what it is.
APP_ASSET = "-macos.zip"
ADHOC_ASSET = "-macos-adhoc.zip"


def version(pyproject: Path = ROOT / "pyproject.toml") -> str:
    with pyproject.open("rb") as file:
        value = tomllib.load(file)["project"]["version"]
    if not isinstance(value, str):
        raise ValueError(f"{pyproject}: [project] version is not a string")
    return value


def notes(heading: str, changelog: Path = CHANGELOG) -> str | None:
    """The section under ``## [heading]``, without blank lines at either end; None if absent."""
    section: list[str] | None = None
    for line in changelog.read_text().splitlines():
        if line.startswith("## "):
            if section is not None:
                break
            title = re.sub(r"^## \[?", "", line)
            if re.sub(r"\]?( .*)?$", "", title) == heading:
                section = []
        elif section is not None:
            # Link reference definitions at the end of the file belong to no section.
            if re.match(r"^\[[^\]]+\]: ", line):
                break
            section.append(line)
    if section is None:
        return None
    text = "\n".join(section).strip("\n")
    return text + "\n" if text else None


def _set_stanza(text: str, name: str, value: str) -> str:
    """Replaces the one top-level ``  name "..."`` line in a formula or cask."""
    pattern = re.compile(rf'^  {name} ".*"$', re.MULTILINE)
    replaced, count = pattern.subn(lambda _: f'  {name} "{value}"', text)
    if count != 1:
        raise ValueError(f"expected one {name} stanza, found {count}")
    return replaced


def _check_sha256(sha256: str) -> None:
    if not SHA256.fullmatch(sha256):
        raise ValueError(f"not a lowercase hex sha256: {sha256!r}")


def formula(url: str, sha256: str, source: Path = FORMULA) -> str:
    """The formula with ``url`` and ``sha256`` after ``homepage``, the commented stand-ins gone.

    Before the first tag the formula carries the two lines in a comment block that starts
    ``# FIRST RELEASE`` and ends at the commented ``sha256``; after it, the real lines.
    Either way the result has exactly one of each.
    """
    _check_sha256(sha256)
    lines = source.read_text().splitlines()
    out: list[str] = []
    skipping = False
    for line in lines:
        if line.startswith("  # FIRST RELEASE"):
            skipping = True
        if skipping:
            if re.match(r'^  #\s+sha256 "', line):
                skipping = False
            continue
        if re.match(r'^  (url|sha256) "', line):
            continue
        out.append(line)
        if line.startswith("  homepage "):
            out += [f'  url "{url}"', f'  sha256 "{sha256}"']
    if skipping:
        raise ValueError(f"{source}: the FIRST RELEASE comment has no commented sha256 line")
    if sum(line.startswith("  homepage ") for line in out) != 1:
        raise ValueError(f"{source}: expected one homepage stanza")
    # Removing the comment block can leave two blank lines in a row.
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out) + "\n")
    return text


def cask(version: str, sha256: str, adhoc: bool = False, source: Path = CASK) -> str:
    _check_sha256(sha256)
    text = _set_stanza(source.read_text(), "version", version)
    text = _set_stanza(text, "sha256", sha256)
    if adhoc:
        if text.count(APP_ASSET) != 1:
            raise ValueError(f"{source}: expected one url ending {APP_ASSET}")
        text = text.replace(APP_ASSET, ADHOC_ASSET)
        text = (
            "# AD HOC BUILD: not signed with a Developer ID and not notarized. Gatekeeper\n"
            "# rejects the download, so this cask must not go to the tap.\n" + text
        )
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("version", help="print the version in pyproject.toml")
    notes_parser = commands.add_parser("notes", help="print the CHANGELOG section for VERSION")
    notes_parser.add_argument("version")
    formula_parser = commands.add_parser("formula", help="print the formula for a tag")
    formula_parser.add_argument("--url", required=True)
    formula_parser.add_argument("--sha256", required=True)
    cask_parser = commands.add_parser("cask", help="print the cask for a release")
    cask_parser.add_argument("--version", required=True)
    cask_parser.add_argument("--sha256", required=True)
    cask_parser.add_argument("--adhoc", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.command == "version":
            print(version())
        elif args.command == "notes":
            text = notes(args.version)
            if text is None:
                print(f"release.py: no CHANGELOG section for {args.version}", file=sys.stderr)
                return 1
            sys.stdout.write(text)
        elif args.command == "formula":
            sys.stdout.write(formula(args.url, args.sha256))
        else:
            sys.stdout.write(cask(args.version, args.sha256, args.adhoc))
    except ValueError as error:
        print(f"release.py: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
