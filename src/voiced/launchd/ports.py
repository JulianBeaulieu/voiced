"""Who holds the services' ports, for ``voiced serve``, ``voiced install``'s
ports step and ``voiced doctor``.

Whether a port is free is answered by the same ``bind()`` the services do,
which sees every account's sockets. ``lsof`` then names the holder, but run
as the user it lists only the user's own processes: a holder it cannot name
belongs to another account or the system.
"""

from __future__ import annotations

import errno
import socket
from collections.abc import Mapping, Sequence
from pathlib import Path

import psutil

from voiced.launchd.control import Runner


def bind_port(bind: str, port: int) -> socket.socket:
    """Bind ``bind:port`` the way uvicorn's own ``bind_socket`` would (same
    family and ``SO_REUSEADDR``), so the answer matches what it would see."""
    family = socket.AF_INET6 if ":" in bind else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((bind, port))
    except OSError:
        sock.close()
        raise
    sock.set_inheritable(True)
    return sock


def bind_error(bind: str, port: int) -> OSError | None:
    """Why a service could not bind ``bind:port`` now, or None."""
    try:
        bind_port(bind, port).close()
    except OSError as exc:
        return exc
    return None


def listener_pids(port: int, runner: Runner) -> list[int] | None:
    """Pids listening on ``port`` via lsof; None if lsof is unavailable."""
    result = runner(["lsof", "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"])
    if result.returncode == 127:
        return None
    return [int(line) for line in result.stdout.split() if line.isdigit()]


def process_of(pid: int) -> tuple[str, list[str]]:
    """The name and the exact argument vector of ``pid`` (``ps -o args=``
    joins it with spaces, so argv[1] cannot be told apart from later
    arguments)."""
    try:
        proc = psutil.Process(pid)
        return proc.name(), list(proc.cmdline())
    except (psutil.Error, OSError):
        return "unknown", []


def runs_entry(argv: Sequence[str], entry: Path) -> bool:
    """``argv`` is a voiced service started from ``entry``: the plists run
    ``<entry> serve ...``, a console script, so the kernel runs it as
    ``<interpreter> <entry> serve ...``."""
    return len(argv) > 1 and argv[1] == str(entry)


def foreign_listeners(
    ports: Mapping[str, int], bind: str, entry: Path, query: Runner
) -> tuple[list[str], list[str]]:
    """The configured ports (by config key) another program holds, each
    with its holder, and notes on ports in use that lsof cannot tell apart.

    A port is held when the services could not bind it, or when lsof lists
    a listener on it that is not voiced's own (a reinstall or an update):
    with ``SO_REUSEADDR`` a listener on 127.0.0.1 does not stop a bind on
    0.0.0.0, yet would answer local clients in voiced's place."""
    conflicts: list[str] = []
    notes: list[str] = []
    for key, port in ports.items():
        error = bind_error(bind, port)
        if error is not None and error.errno != errno.EADDRINUSE:
            conflicts.append(f"port {port} ({key}) cannot be bound on {bind}: {error.strerror}")
            continue
        pids = listener_pids(port, query)
        if pids is None:
            if error is not None:
                notes.append(f"port {port} ({key}) is in use; lsof not found to tell by whom")
            continue
        holders = [(pid, *process_of(pid)) for pid in pids]
        others = [
            f"{name} (pid {pid})" for pid, name, argv in holders if not runs_entry(argv, entry)
        ]
        if others:
            conflicts.append(f"port {port} ({key}) is in use by {', '.join(others)}")
        elif error is not None and not holders:
            conflicts.append(f"port {port} ({key}) is in use by another account or the system")
    return conflicts, notes
