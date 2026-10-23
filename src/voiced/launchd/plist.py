"""Render the LaunchAgent plists: the three services' and the Metal probe's
(ADR 0035)."""

from __future__ import annotations

import plistlib
from dataclasses import dataclass
from pathlib import Path

APP_BUNDLE_ID = "io.github.julianbeaulieu.voiced.menubar"
"""Named in ``AssociatedBundleIdentifiers``, so Login Items lists the
services under the app's name and icon."""

ENV_STDIO_LOG = "VOICED_STDIO_LOG"
"""``<log_dir>/<service>``, the prefix of the two files ``voiced.launchd.stdio``
points stdout and stderr at."""

# The rotating application log, shared by voiced.logging and the stdlib-only
# watchdog so both rotate the same way.
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5


@dataclass(frozen=True)
class LogPaths:
    """Where one service's output ends up under ``log_dir``."""

    app: Path  # written by voiced.logging (rotating)
    stdout: Path  # the process stdout (voiced.launchd.stdio)
    stderr: Path  # the process stderr (voiced.launchd.stdio)


def log_paths(log_dir: Path, service: str) -> LogPaths:
    return LogPaths(
        app=log_dir / f"{service}.log",
        stdout=log_dir / f"{service}.stdout.log",
        stderr=log_dir / f"{service}.stderr.log",
    )


def render_plist(
    *,
    label: str,
    program_arguments: list[str],
    log_dir: Path,
    working_directory: Path,
    environment: dict[str, str],
    keep_alive: bool | dict[str, object] = True,
    throttle_interval_s: int = 10,
) -> bytes:
    """A LaunchAgent plist with RunAtLoad, logging to ``log_dir``.

    ``keep_alive`` is ``False`` for a one-shot job (the Metal probe), and for
    the services ``{"PathState": {<entry point>: True}}``: launchd restarts
    them while the entry point exists, and stops respawning while it
    dangles (ADR 0017). No ``UserName``: an agent runs as the user whose
    domain it is in.

    There is no ``StandardOutPath``/``StandardErrorPath``: ``VOICED_STDIO_LOG``
    tells the process to open its own (``voiced.launchd.stdio``, ADR 0006).
    """
    service = label.rsplit(".", 1)[-1]
    plist: dict[str, object] = {
        "Label": label,
        "ProgramArguments": program_arguments,
        "RunAtLoad": True,
        "KeepAlive": keep_alive,
        "ThrottleInterval": throttle_interval_s,
        # Interactive: no resource limits (launchd.plist(5)); the task role
        # is set in-process (cli.cmd_serve).
        "ProcessType": "Interactive",
        "WorkingDirectory": str(working_directory),
        "EnvironmentVariables": {**environment, ENV_STDIO_LOG: str(log_dir / service)},
        "AssociatedBundleIdentifiers": [APP_BUNDLE_ID],
    }
    return plistlib.dumps(plist, sort_keys=False)
