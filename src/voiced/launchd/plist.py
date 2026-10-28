"""Render the LaunchAgent plists: the three services' and the Metal probe's
(ADR 0035)."""

from __future__ import annotations

import plistlib
from dataclasses import dataclass
from pathlib import Path

APP_BUNDLE_ID = "io.github.julianbeaulieu.voiced.menubar"
"""Named in ``AssociatedBundleIdentifiers``, so Login Items lists the
services under the app's name and icon."""

UMASK = 0o077
"""The services' umask: launchd creates their stdout and stderr files 0600
with it, and the services create their own files with it too. Nobody but
the user reads any of them."""

# The rotating application log, shared by voiced.logging and the stdlib-only
# watchdog so both rotate the same way.
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5


@dataclass(frozen=True)
class LogPaths:
    """Where one service's output ends up under ``log_dir``."""

    app: Path  # written by voiced.logging (rotating)
    stdout: Path  # the process stdout (launchd's StandardOutPath)
    stderr: Path  # the process stderr (launchd's StandardErrorPath)


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

    launchd opens ``<service>.stdout.log`` and ``.stderr.log`` as the user,
    appending, before it starts the process (ADR 0006), so tracebacks and
    C-level output (MLX, espeak-ng) land there. It creates a missing file
    with ``Umask`` (0600), and a missing directory too (0744, whatever the
    umask), though ``voiced install`` creates the log directory first. A
    path it cannot open is not skipped: launchd does not start the process
    and records exit code 78 (EX_CONFIG), which ``launchctl print`` shows.
    """
    service = label.rsplit(".", 1)[-1]
    logs = log_paths(log_dir, service)
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
        "EnvironmentVariables": environment,
        "StandardOutPath": str(logs.stdout),
        "StandardErrorPath": str(logs.stderr),
        "Umask": UMASK,
        "AssociatedBundleIdentifiers": [APP_BUNDLE_ID],
    }
    return plistlib.dumps(plist, sort_keys=False)
