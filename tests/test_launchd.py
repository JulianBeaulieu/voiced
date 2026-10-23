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
        working_directory=Path("/Users/someone"),
        environment={"HOME": "/Users/someone"},
    )
    plist = plistlib.loads(data)
    assert plist["Label"] == "io.github.julianbeaulieu.voiced.tts"
    assert plist["KeepAlive"] is True
    assert plist["RunAtLoad"] is True
    assert plist["ThrottleInterval"] == 10
    # The process opens its own log files (ADR 0006).
    assert "StandardOutPath" not in plist
    assert "StandardErrorPath" not in plist
    assert plist["EnvironmentVariables"] == {
        "HOME": "/Users/someone",
        "VOICED_STDIO_LOG": "/Users/someone/Library/Logs/voiced/tts",
    }
    # An agent runs as the user whose domain it is in.
    assert "UserName" not in plist
