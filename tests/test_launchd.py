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
    # launchd opens the log files, as the user, 0600 (ADR 0006).
    assert plist["StandardOutPath"] == "/Users/someone/Library/Logs/voiced/tts.stdout.log"
    assert plist["StandardErrorPath"] == "/Users/someone/Library/Logs/voiced/tts.stderr.log"
    assert plist["Umask"] == 0o077
    assert plist["EnvironmentVariables"] == {"HOME": "/Users/someone"}
    # An agent runs as the user whose domain it is in.
    assert "UserName" not in plist
