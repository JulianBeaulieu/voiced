"""The ``/health`` statuses, for the services and the stdlib-only watchdog alike."""

from __future__ import annotations

from typing import Literal

HealthStatus = Literal["starting", "ok", "degraded", "missing_model", "idle", "loading"]

HEALTHY: frozenset[HealthStatus] = frozenset({"ok", "idle", "loading"})
"""Statuses the watchdog takes for healthy: ``idle`` (the model unloaded on
purpose) and ``loading`` (loading it again for a request) as well as ``ok``
(ADR 0033)."""
