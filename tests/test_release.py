"""The release workflow's helpers (``scripts/release.py``)."""

from __future__ import annotations

import base64
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import pytest

from tests.scripts_support import load

SHA = "ab" * 32
URL = "https://github.com/example/voiced/archive/refs/tags/v1.2.0.tar.gz"


@pytest.fixture(scope="module")
def release() -> ModuleType:
    return load("release")


CHANGELOG = """\
# Changelog

## Unreleased

### Added

- Something new.

## [1.2.0] - 2026-10-19

### Fixed

- A bug.

## 1.1.0

- Older.

[1.2.0]: https://example.com/compare/v1.1.0...v1.2.0
"""


def test_notes_are_the_section_for_the_version(release: ModuleType, tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(CHANGELOG)
    assert release.notes("1.2.0", changelog) == "### Fixed\n\n- A bug.\n"
    assert release.notes("1.1.0", changelog) == "- Older.\n"
    assert release.notes("Unreleased", changelog) == "### Added\n\n- Something new.\n"
    assert release.notes("1.2", changelog) is None
    assert release.notes("9.9.9", changelog) is None


def test_the_version_is_pyprojects(release: ModuleType) -> None:
    text = (Path(release.ROOT) / "pyproject.toml").read_text()
    assert f'version = "{release.version()}"' in text


def test_formula_gets_one_url_and_sha256_after_homepage(release: ModuleType) -> None:
    text = release.formula(URL, SHA)
    assert re.findall(r'^  url "(.*)"$', text, re.MULTILINE) == [URL]
    assert re.findall(r'^  sha256 "(.*)"$', text, re.MULTILINE) == [SHA]
    lines = text.splitlines()
    homepage = next(i for i, line in enumerate(lines) if line.startswith("  homepage "))
    assert lines[homepage + 1 : homepage + 3] == [f'  url "{URL}"', f'  sha256 "{SHA}"']
    assert "\n\n\n" not in text
    # The rest of the formula is untouched.
    assert 'head "https://github.com/JulianBeaulieu/voiced.git", branch: "main"' in text
    assert text.rstrip().endswith("end")


def test_formula_replaces_the_lines_of_an_earlier_tag(release: ModuleType, tmp_path: Path) -> None:
    earlier = tmp_path / "voiced.rb"
    earlier.write_text(release.formula(URL, SHA))
    newer = URL.replace("1.2.0", "1.3.0")
    text = release.formula(newer, "cd" * 32, earlier)
    assert re.findall(r'^  url "(.*)"$', text, re.MULTILINE) == [newer]
    assert re.findall(r'^  sha256 "(.*)"$', text, re.MULTILINE) == ["cd" * 32]


def test_cask_sets_version_and_sha256(release: ModuleType) -> None:
    text = release.cask("1.2.0", SHA)
    assert re.findall(r'^  version "(.*)"$', text, re.MULTILINE) == ["1.2.0"]
    assert re.findall(r'^  sha256 "(.*)"$', text, re.MULTILINE) == [SHA]
    assert "voiced-#{version}-macos.zip" in text
    assert "AD HOC" not in text


def test_the_cask_leaves_the_services_alone_on_upgrade(release: ModuleType) -> None:
    # Homebrew runs `uninstall` on upgrade and reinstall as well, with sudo; only `zap`
    # waits for `brew uninstall --zap`.
    text = release.cask("1.2.0", SHA)
    stanzas = re.findall(r"^  (uninstall|zap) (.*?)\n\n", text, re.MULTILINE | re.DOTALL)
    assert [name for name, _ in stanzas] == ["uninstall", "zap"]
    uninstall, zap = (body for _, body in stanzas)
    assert uninstall == 'quit: "io.github.julianbeaulieu.voiced.menubar"'
    for service in ("stt", "tts", "watchdog"):
        label = f"io.github.julianbeaulieu.voiced.{service}"
        assert f'"{label}"' in zap
    assert 'trash:     "~/Library/Application Support/voiced"' in zap


def test_an_adhoc_cask_says_so_and_points_at_the_adhoc_asset(release: ModuleType) -> None:
    text = release.cask("1.2.0", SHA, adhoc=True)
    assert text.startswith("# AD HOC BUILD")
    assert "voiced-#{version}-macos-adhoc.zip" in text
    assert "-macos.zip" not in text


@pytest.mark.parametrize("bad", ["", "AB" * 32, "ab" * 31, "ab" * 32 + "\n"])
def test_a_bad_sha256_is_refused(release: ModuleType, bad: str) -> None:
    with pytest.raises(ValueError):
        release.cask("1.2.0", bad)
    with pytest.raises(ValueError):
        release.formula(URL, bad)


def test_main_fails_for_a_missing_section(
    release: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    assert release.main(["notes", "0.0.0-no-such-version"]) == 1
    assert "no CHANGELOG section" in capsys.readouterr().err


SIGNATURE = base64.b64encode(bytes(range(64))).decode()
SPARKLE = "{http://www.andymatuschak.org/xml-namespaces/sparkle}"


def test_the_appcast_is_one_item_for_the_signed_zip(release: ModuleType) -> None:
    published = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    text = release.appcast("1.2.0rc1", "1.2.0", SIGNATURE, 16_384_000, published)
    assert text.startswith("<?xml ")
    rss = ET.fromstring(text)
    items = rss.findall("channel/item")
    assert len(items) == 1
    item = items[0]
    page = "https://github.com/JulianBeaulieu/voiced/releases/tag/v1.2.0rc1"
    assert item.findtext("link") == page
    assert item.findtext(f"{SPARKLE}version") == "1.2.0"
    assert item.findtext(f"{SPARKLE}shortVersionString") == "1.2.0rc1"
    assert item.findtext(f"{SPARKLE}minimumSystemVersion") == "14.0"
    assert item.findtext("pubDate") == "Fri, 23 Oct 2026 12:00:00 +0000"
    assert page in (item.findtext("description") or "")
    enclosure = item.find("enclosure")
    assert enclosure is not None
    assert enclosure.attrib == {
        "url": "https://github.com/JulianBeaulieu/voiced/releases/download/v1.2.0rc1/voiced-1.2.0rc1-macos.zip",
        "length": "16384000",
        "type": "application/octet-stream",
        f"{SPARKLE}edSignature": SIGNATURE,
    }


def test_the_minimum_macos_is_the_packages_and_the_bundles(
    release: ModuleType, tmp_path: Path
) -> None:
    minimum = release.minimum_macos()
    bundle = (Path(release.ROOT) / "menubar" / "Scripts" / "bundle.sh").read_text()
    assert f"<key>LSMinimumSystemVersion</key>\n    <string>{minimum}</string>" in bundle
    package = tmp_path / "Package.swift"
    package.write_text("platforms: [.macOS(.v10_15)],\n")
    assert release.minimum_macos(package) == "10.15"
    package.write_text("platforms: [.iOS(.v17)],\n")
    with pytest.raises(ValueError):
        release.minimum_macos(package)


@pytest.mark.parametrize(
    ("bundle_version", "signature", "length"),
    [
        ("1.2", SIGNATURE, 1),
        ("1.2.0rc1", SIGNATURE, 1),
        ("1.2.0", "", 1),
        ("1.2.0", "not base64!", 1),
        ("1.2.0", base64.b64encode(bytes(32)).decode(), 1),
        ("1.2.0", SIGNATURE, 0),
    ],
)
def test_a_bad_appcast_input_is_refused(
    release: ModuleType, bundle_version: str, signature: str, length: int
) -> None:
    with pytest.raises(ValueError):
        release.appcast("1.2.0", bundle_version, signature, length)


def test_main_prints_the_appcast(release: ModuleType, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["appcast", "--version", "1.2.0", "--bundle-version", "1.2.0"]
    assert release.main([*args, "--signature", SIGNATURE, "--length", "10"]) == 0
    assert ET.fromstring(capsys.readouterr().out).find("channel/item") is not None
    assert release.main([*args, "--signature", "x", "--length", "10"]) == 1
    assert "signature" in capsys.readouterr().err
