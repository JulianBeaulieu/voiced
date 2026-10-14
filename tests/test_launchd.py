from __future__ import annotations

import plistlib
from pathlib import Path

from voiced.config import Settings
from voiced.launchd import render_plist


def test_render_plist(settings: Settings) -> None:
    data = render_plist(
        label=settings.launchd.label("tts"),
        program_arguments=["/opt/voiced/.venv/bin/voiced", "serve", "tts"],
        log_dir=Path("/Users/someone/Library/Logs/voiced"),
        user_name="someone",
    )
    plist = plistlib.loads(data)
    assert plist["Label"] == "io.github.julianbeaulieu.voiced.tts"
    assert plist["KeepAlive"] is True
    assert plist["RunAtLoad"] is True
    assert plist["ThrottleInterval"] == 10
    assert plist["StandardErrorPath"].endswith("/tts.stderr.log")
