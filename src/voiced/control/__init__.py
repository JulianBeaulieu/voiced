"""Local control channel: a Unix socket served by the watchdog.

A menu bar app, ``voiced status``, ``voiced restart`` and ``voiced settings``
talk to the running installation through ``<state dir>/control.sock`` with
newline-delimited JSON (``voiced.control.protocol``). Nothing here listens
on a network interface and nothing needs root (ADR 0013).

The watchdog serves the socket, so this package imports only the standard
library, ``voiced.config``, ``voiced.settings_store`` (pydantic, tomlkit) and
the watchdog: no FastAPI, no numpy, no engine plugins. The engine catalogue
is read in a child process (``voiced.control.ops.engines_in_subprocess``).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from voiced.config import Settings

SOCKET_NAME = "control.sock"
NOT_SERVED = "watchdog running but control socket not served; see `voiced logs watchdog`"


def state_dir(settings: Settings) -> Path:
    """The per-user state directory of the install layout (ADR 0006): the
    venv directory, so moving the venv moves the socket."""
    return settings.install.venv_dir


def socket_path(settings: Settings) -> Path:
    return state_dir(settings) / SOCKET_NAME
