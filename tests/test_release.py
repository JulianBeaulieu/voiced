"""The release workflow's helpers (``scripts/release.py``)."""

from __future__ import annotations

import re
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
    assert "FIRST RELEASE" not in text
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


def test_formula_replaces_the_first_release_comment(release: ModuleType, tmp_path: Path) -> None:
    untagged = tmp_path / "voiced.rb"
    untagged.write_text(
        "class Voiced < Formula\n"
        '  homepage "https://github.com/JulianBeaulieu/voiced"\n'
        '  head "https://github.com/JulianBeaulieu/voiced.git", branch: "main"\n'
        "\n"
        "  # FIRST RELEASE: no tag yet.\n"
        "  #\n"
        f'  #   url "{URL}"\n'
        f'  #   sha256 "{"0" * 64}"\n'
        "\n"
        '  depends_on "uv" => :build\n'
        "end\n"
    )
    text = release.formula(URL, SHA, untagged)
    assert "FIRST RELEASE" not in text
    assert re.findall(r'^  url "(.*)"$', text, re.MULTILINE) == [URL]
    assert re.findall(r'^  sha256 "(.*)"$', text, re.MULTILINE) == [SHA]
    assert "\n\n\n" not in text


def test_cask_sets_version_and_sha256(release: ModuleType) -> None:
    text = release.cask("1.2.0", SHA)
    assert re.findall(r'^  version "(.*)"$', text, re.MULTILINE) == ["1.2.0"]
    assert re.findall(r'^  sha256 "(.*)"$', text, re.MULTILINE) == [SHA]
    assert "voiced-#{version}-macos.zip" in text
    assert "AD HOC" not in text


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
