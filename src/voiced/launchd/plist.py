"""Render LaunchDaemon plists.

Labels come from ``settings.launchd.label(service)`` so the reverse-DNS prefix
lives in exactly one place (``voiced.config.DEFAULT_LAUNCHD_LABEL_PREFIX``).
The installer (Phase 1) writes these to ``/Library/LaunchDaemons`` through a
printed sudo script, never silently.
"""

from __future__ import annotations

import plistlib
from pathlib import Path


def render_plist(
    *,
    label: str,
    program_arguments: list[str],
    log_dir: Path,
    user_name: str,
    working_directory: Path | None = None,
    environment: dict[str, str] | None = None,
    throttle_interval_s: int = 10,
) -> bytes:
    """A LaunchDaemon plist with RunAtLoad and KeepAlive, logging to ``log_dir``."""
    short = label.rsplit(".", 1)[-1]
    plist: dict[str, object] = {
        "Label": label,
        "ProgramArguments": program_arguments,
        "UserName": user_name,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": throttle_interval_s,
        "StandardOutPath": str(log_dir / f"{short}.stdout.log"),
        "StandardErrorPath": str(log_dir / f"{short}.stderr.log"),
        "ProcessType": "Interactive",
    }
    if working_directory is not None:
        plist["WorkingDirectory"] = str(working_directory)
    if environment:
        plist["EnvironmentVariables"] = dict(environment)
    return plistlib.dumps(plist, sort_keys=False)
