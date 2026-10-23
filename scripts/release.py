"""Helpers for .github/workflows/release.yml (ADR 0018, "Signing and release CI").

    python scripts/release.py version
    python scripts/release.py notes VERSION
    python scripts/release.py formula --url URL --sha256 SHA
    python scripts/release.py cask --version VERSION --sha256 SHA [--adhoc]
    python scripts/release.py appcast --version VERSION --app APP --zip ZIP \
        --signature SIGNATURE

``version`` prints ``version`` from pyproject.toml. ``notes`` prints the
CHANGELOG section headed VERSION (``## [0.1.0] - 2026-10-19`` or
``## 0.1.0``), the release notes, and fails when there is none. ``formula``
prints packaging/homebrew/voiced.rb with ``url`` and ``sha256`` set to the
tag's source archive. ``cask`` prints packaging/homebrew/cask/voiced.rb with
``version`` and ``sha256`` set to the app's zip; ``--adhoc`` points it at the
ad hoc asset and marks it as not for the tap. ``appcast`` prints the feed the
menu bar app checks for its own updates (ADR 0034), one item for VERSION: the
zip, named and sized as it is on disk, its EdDSA signature (``sign_update
-p``), the built app's ``CFBundleVersion`` and ``LSMinimumSystemVersion``
from its Info.plist, and the release page. Standard library only: the
workflow runs it with whatever Python the runner has.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import plistlib
import re
import sys
import tomllib
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"
FORMULA = ROOT / "packaging" / "homebrew" / "voiced.rb"
CASK = ROOT / "packaging" / "homebrew" / "cask" / "voiced.rb"
REPOSITORY = "https://github.com/JulianBeaulieu/voiced"
SPARKLE = "http://www.andymatuschak.org/xml-namespaces/sparkle"

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
    _check_sha256(sha256)
    text = _set_stanza(source.read_text(), "url", url)
    return _set_stanza(text, "sha256", sha256)


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


def appcast(
    version: str, app: Path, archive: Path, signature: str, published: datetime | None = None
) -> str:
    """The Sparkle feed for one release: the item Sparkle compares and installs.

    Sparkle compares ``sparkle:version`` with the running app's ``CFBundleVersion``
    and shows ``sparkle:shortVersionString``. The bundle version and the minimum
    macOS come from the Info.plist of ``app``, the bundle ``archive`` was made
    from. The enclosure is ``archive``'s
    name in the release and its size, so the signature cannot be attached to
    another file. The app links to the release page (the item's ``link``) rather
    than show notes, so the description is a link to it too.
    """
    with (app / "Contents" / "Info.plist").open("rb") as file:
        info = plistlib.load(file)
    bundle_version = info.get("CFBundleVersion")
    minimum = info.get("LSMinimumSystemVersion")
    if not isinstance(bundle_version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", bundle_version):
        raise ValueError(f"{app}: not a CFBundleVersion of three numbers: {bundle_version!r}")
    if not isinstance(minimum, str) or not re.fullmatch(r"\d+(\.\d+)*", minimum):
        raise ValueError(f"{app}: no LSMinimumSystemVersion")
    try:
        signature_bytes = base64.b64decode(signature, validate=True)
    except binascii.Error:
        signature_bytes = b""
    if len(signature_bytes) != 64:
        raise ValueError(f"not a base64 EdDSA signature: {signature!r}")
    length = archive.stat().st_size
    if length <= 0:
        raise ValueError(f"{archive} is empty")
    page = f"{REPOSITORY}/releases/tag/v{version}"
    url = f"{REPOSITORY}/releases/download/v{version}/{archive.name}"

    ET.register_namespace("sparkle", SPARKLE)
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = "voiced"
    ET.SubElement(channel, "link").text = REPOSITORY
    ET.SubElement(channel, "description").text = "Updates of the voiced menu bar app"
    ET.SubElement(channel, "language").text = "en"
    item = ET.SubElement(channel, "item")
    ET.SubElement(item, "title").text = f"voiced {version}"
    ET.SubElement(item, "link").text = page
    ET.SubElement(item, f"{{{SPARKLE}}}version").text = bundle_version
    ET.SubElement(item, f"{{{SPARKLE}}}shortVersionString").text = version
    ET.SubElement(item, f"{{{SPARKLE}}}minimumSystemVersion").text = minimum
    ET.SubElement(item, "description").text = f'<p>Release notes: <a href="{page}">{page}</a></p>'
    ET.SubElement(item, "pubDate").text = format_datetime(published or datetime.now(UTC))
    ET.SubElement(
        item,
        "enclosure",
        {
            "url": url,
            "length": str(length),
            "type": "application/octet-stream",
            f"{{{SPARKLE}}}edSignature": signature,
        },
    )
    ET.indent(rss)
    return ET.tostring(rss, encoding="unicode", xml_declaration=True) + "\n"


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
    appcast_parser = commands.add_parser("appcast", help="print the app's update feed")
    appcast_parser.add_argument("--version", required=True)
    appcast_parser.add_argument("--app", required=True, type=Path)
    appcast_parser.add_argument("--zip", required=True, type=Path)
    appcast_parser.add_argument("--signature", required=True)
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
        elif args.command == "cask":
            sys.stdout.write(cask(args.version, args.sha256, args.adhoc))
        else:
            sys.stdout.write(appcast(args.version, args.app, args.zip, args.signature))
    except (ValueError, OSError) as error:
        print(f"release.py: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
