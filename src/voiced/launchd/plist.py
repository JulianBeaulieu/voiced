"""Render LaunchDaemon plists.

Labels come from ``settings.launchd.label(service)`` so the reverse-DNS prefix
lives in exactly one place (``voiced.config.DEFAULT_LAUNCHD_LABEL_PREFIX``).
The installer writes these to ``/Library/LaunchDaemons`` through a printed
sudo script, never silently.
"""

from __future__ import annotations

import plistlib
from dataclasses import dataclass
from pathlib import Path

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
    user_name: str,
    working_directory: Path | None = None,
    environment: dict[str, str] | None = None,
    throttle_interval_s: int = 10,
    keep_alive: bool = True,
) -> bytes:
    """A LaunchDaemon plist with RunAtLoad and KeepAlive, logging to ``log_dir``.

    ``keep_alive=False`` gives a one-shot job (used for the Metal probe).

    There is no ``StandardOutPath``/``StandardErrorPath``: launchd would open
    them as root in a directory the user owns. ``VOICED_STDIO_LOG`` tells the
    process to open them itself (``voiced.launchd.stdio``, ADR 0006).
    """
    service = label.rsplit(".", 1)[-1]
    plist: dict[str, object] = {
        "Label": label,
        "ProgramArguments": program_arguments,
        "UserName": user_name,
        "RunAtLoad": True,
        "KeepAlive": keep_alive,
        "ThrottleInterval": throttle_interval_s,
        "ProcessType": "Interactive",
    }
    if working_directory is not None:
        plist["WorkingDirectory"] = str(working_directory)
    plist["EnvironmentVariables"] = {
        **(environment or {}),
        ENV_STDIO_LOG: str(log_dir / service),
    }
    return plistlib.dumps(plist, sort_keys=False)
