"""Checks on values that arrive as JSON from clients.

Standard library only: the watchdog's control channel uses it too.
"""

from __future__ import annotations

from typing import TypeGuard


def is_int(value: object) -> TypeGuard[int]:
    """An integer, and not a bool (``True`` is an ``int`` in Python, not in JSON)."""
    return isinstance(value, int) and not isinstance(value, bool)
