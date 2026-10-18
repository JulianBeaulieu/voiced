"""Which ffmpeg voiced runs: the wheel's first, PATH only without it (ADR 0018)."""

from __future__ import annotations

import builtins
import os
import stat
from pathlib import Path
from typing import Any

import imageio_ffmpeg
import pytest

from voiced.ffmpeg_exe import ffmpeg_origin, ffmpeg_path


@pytest.fixture
def path_ffmpeg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An ffmpeg on PATH, as Homebrew's would be."""
    exe = tmp_path / "ffmpeg"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("IMAGEIO_FFMPEG_EXE", raising=False)
    return exe


def test_the_wheels_ffmpeg_comes_before_one_on_path(path_ffmpeg: Path) -> None:
    found = ffmpeg_path()
    assert found is not None and found != str(path_ffmpeg)
    assert os.path.isabs(found)
    assert ffmpeg_origin(found) == f"imageio-ffmpeg {imageio_ffmpeg.__version__}"


def test_without_the_wheels_binary_the_path_one_is_used(
    path_ffmpeg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing() -> str:
        raise RuntimeError("No ffmpeg exe could be found.")

    monkeypatch.setattr(imageio_ffmpeg, "get_ffmpeg_exe", missing)
    assert ffmpeg_path() == str(path_ffmpeg)
    assert ffmpeg_origin(str(path_ffmpeg)) == "PATH"


def test_without_the_wheel_the_path_one_is_used(
    path_ffmpeg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_import = builtins.__import__

    def no_wheel(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "imageio_ffmpeg":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_wheel)
    assert ffmpeg_path() == str(path_ffmpeg)
    assert ffmpeg_origin(str(path_ffmpeg)) == "PATH"


def test_no_ffmpeg_at_all(path_ffmpeg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path_ffmpeg.unlink()
    monkeypatch.setattr(imageio_ffmpeg, "get_ffmpeg_exe", lambda: "ffmpeg")
    assert ffmpeg_path() is None


def test_the_override_is_named(path_ffmpeg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """imageio-ffmpeg's own variable still wins, and doctor says so."""
    monkeypatch.setenv("IMAGEIO_FFMPEG_EXE", str(path_ffmpeg))
    assert ffmpeg_path() == str(path_ffmpeg)
    assert ffmpeg_origin(str(path_ffmpeg)) == "IMAGEIO_FFMPEG_EXE"
