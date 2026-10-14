"""Local control channel: a Unix socket served by the watchdog.

A menu bar app, ``voiced status``, ``voiced restart`` and ``voiced settings``
talk to the running installation through ``<state dir>/control.sock`` with
newline-delimited JSON (``voiced.control.protocol``). Nothing here listens
on a network interface and nothing needs root (ADR 0013).

This package imports only the standard library, ``voiced.config`` and the
watchdog, so the watchdog stays importable without FastAPI; the engine
registry is imported only when the catalogue is asked for.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from voiced.config import Settings

SOCKET_NAME = "control.sock"


def state_dir(settings: Settings) -> Path:
    """The per-user state directory of the install layout (ADR 0006)."""
    return settings.install.venv_dir


def socket_path(settings: Settings) -> Path:
    return state_dir(settings) / SOCKET_NAME
