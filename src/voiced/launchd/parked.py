"""A service switched off parks its process instead of serving (ADR 0033).

With ``[stt] enabled = false`` (or ``[tts]``) the daemon still starts, at
boot and after every restart: unloading a LaunchDaemon so that it stays
down needs root, and switching a service off must not. The process reads
the setting, loads no engine, binds no port, advertises nothing, writes
its pid to :func:`marker` and waits for launchd to stop it.

The marker is how everything else tells a parked process from a hung one:
a service is parked exactly when the pid in its marker is the pid launchd
runs for its label now (:func:`is_parked`). That is a fact about the
running process, not about the config file, so it cannot drift: a setting
changed while the service runs takes effect, and shows, only once the
service restarts. A serving process removes the marker when it starts.

Standard library only: the watchdog reads it (ADR 0012).
"""

from __future__ import annotations

import contextlib
import os
import signal
from pathlib import Path


def off_detail(service: str) -> str:
    """What every surface says about a parked service, with the way back."""
    return (
        f"off ([{service}] enabled = false); turn it on with "
        f"`voiced settings set {service}.enabled=true` and `voiced restart {service}`"
    )


def marker(state_dir: Path, service: str) -> Path:
    """Where a parked ``service`` writes its pid: ``<state dir>/<service>.off``."""
    return state_dir / f"{service}.off"


def parked_pid(path: Path) -> int | None:
    """The pid in the marker at ``path``; None without a readable one."""
    try:
        text = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return int(text) if text.isdigit() else None


def is_parked(path: Path, pid: int | None) -> bool:
    """Whether ``pid``, the process launchd runs for the service now, is parked."""
    return pid is not None and parked_pid(path) == pid


def clear(path: Path) -> None:
    """Remove the marker: the service is about to serve."""
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


def park(path: Path) -> None:
    """Write this process's pid to ``path``, then wait for SIGTERM or SIGINT.

    The marker stays when the process is killed (the watchdog's restart
    sends SIGKILL); its pid then names a process that is gone, which
    :func:`is_parked` never matches.
    """
    stop = {signal.SIGTERM, signal.SIGINT}
    signal.pthread_sigmask(signal.SIG_BLOCK, stop)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}")
    tmp.write_text(f"{os.getpid()}\n", encoding="ascii")
    tmp.replace(path)
    try:
        signal.sigwait(stop)
    finally:
        if parked_pid(path) == os.getpid():
            clear(path)
