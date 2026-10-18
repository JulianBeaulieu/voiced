"""NOTICE lists exactly what ``requirements.lock`` installs (``scripts/notice.py``)."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest

from tests.scripts_support import load


@pytest.fixture(scope="module")
def notice() -> ModuleType:
    return load("notice")


@pytest.fixture(scope="module")
def lock_target() -> ModuleType:
    return load("lock_target")


def test_notice_matches_the_lock(notice: ModuleType) -> None:
    assert notice.main(["--check"]) == 0


def test_markers_select_what_installs_on_the_mac(lock_target: ModuleType, tmp_path: Path) -> None:
    lock = tmp_path / "requirements.lock"
    lock.write_text(
        "Numpy==2.4.6 ; python_full_version < '3.12' \\\n"
        "    --hash=sha256:aa\n"
        "numpy==2.5.4 ; python_full_version >= '3.12' \\\n"
        "    --hash=sha256:bb\n"
        "colorama==0.4.6 ; sys_platform == 'win32' \\\n"
        "    --hash=sha256:cc\n"
        "mlx-metal==0.32.3 ; sys_platform == 'darwin' \\\n"
        "    --hash=sha256:dd\n"
        "Espeakng_Loader==0.2.4 \\\n"
        "    --hash=sha256:ee\n"
        "    # via kokoro-onnx\n"
    )
    assert lock_target.installed(lock) == [
        ("espeakng-loader", "0.2.4"),
        ("mlx-metal", "0.32.3"),
        ("numpy", "2.5.4"),
    ]


def test_check_names_every_difference(notice: ModuleType) -> None:
    packages = [("espeakng-loader", "0.2.4"), ("numpy", "2.5.4")]
    good = notice.render(packages, {"espeakng-loader": "MIT", "numpy": "BSD-3-Clause"})
    assert notice.check(good, packages) == []

    stale = good.replace("numpy==2.5.4", "numpy==2.4.6")
    assert notice.check(stale, packages) == [
        "missing numpy==2.5.4",
        "not in the lock: numpy==2.4.6",
    ]

    without_espeak = good.replace("  espeak-ng (library and data)\n", "")
    assert notice.check(without_espeak, packages) == ["missing espeak-ng (library and data)"]

    # ffmpeg is listed only while imageio-ffmpeg, which carries it, is locked.
    with_ffmpeg = notice.render(
        [*packages, ("imageio-ffmpeg", "0.6.0")],
        {"espeakng-loader": "MIT", "numpy": "BSD-3-Clause", "imageio-ffmpeg": "BSD-2-Clause"},
    )
    ffmpeg = "ffmpeg (static GPL build with x264, x265, LAME, Opus and others)"
    assert f"  {ffmpeg}\n" in with_ffmpeg
    assert notice.check(with_ffmpeg, packages) == [
        "not in the lock: imageio-ffmpeg==0.6.0",
        f"{ffmpeg} listed, but imageio-ffmpeg is not in the lock",
    ]


def test_licences_are_spdx(notice: ModuleType) -> None:
    for expression in [
        "MIT",
        "Apache-2.0 OR BSD-2-Clause",
        "BSD-2-Clause AND Apache-2.0 WITH LLVM-exception",
    ]:
        assert notice.is_spdx(expression), expression
    for expression in ["BSD", "MIT License", "Apache Software License", ""]:
        assert not notice.is_spdx(expression), expression
    listed = notice.NOTICE.read_text()
    for line in listed.splitlines():
        if line.startswith("  ") and "==" in line:
            assert notice.is_spdx(line.split("  ")[2]), line


def test_the_python_version_is_a_patch_version(lock_target: ModuleType, tmp_path: Path) -> None:
    assert lock_target.python_version().count(".") == 2
    bad = tmp_path / "app-python-version"
    bad.write_text("3.12\n")
    with pytest.raises(SystemExit, match="not a CPython patch version"):
        lock_target.python_version(bad)
