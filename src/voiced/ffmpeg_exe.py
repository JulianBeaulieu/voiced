"""Which ffmpeg voiced runs (ADR 0018).

The static ffmpeg in the ``imageio-ffmpeg`` wheel comes first, so every Mac
runs the build the release was tested with; an ffmpeg on ``PATH`` is used
only when the wheel is missing. ``IMAGEIO_FFMPEG_EXE`` still overrides both,
as imageio-ffmpeg defines it. Standard library only, apart from that wheel:
``voiced doctor`` imports this without the HTTP layer.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

ENV_OVERRIDE = "IMAGEIO_FFMPEG_EXE"


def ffmpeg_path() -> str | None:
    """Absolute path of the ffmpeg binary, or ``None`` if there is none."""
    try:
        import imageio_ffmpeg

        # Its own fallbacks end in a bare "ffmpeg"; which() makes any answer absolute.
        exe: str = imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError):
        exe = "ffmpeg"
    return shutil.which(exe)


def ffmpeg_origin(path: str) -> str:
    """Where ``path`` comes from, for ``voiced doctor``: the wheel (with its
    version), the override, or ``PATH``."""
    if os.environ.get(ENV_OVERRIDE):
        return ENV_OVERRIDE
    try:
        import imageio_ffmpeg
    except ImportError:
        return "PATH"
    package = Path(imageio_ffmpeg.__file__).resolve().parent
    if package in Path(path).resolve().parents:
        return f"imageio-ffmpeg {imageio_ffmpeg.__version__}"
    return "PATH"
