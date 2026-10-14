"""The watchdog as the control channel's :class:`~voiced.control.ops.Supervisor`.

Status comes from a fresh, probe-free observation of each service (``/health``
and ``launchctl print``) combined with what the watchdog remembers (restart
count, startup grace). Restarts go through :meth:`Watchdog.restart_now`, the
same path the watchdog's own decisions take.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Sequence
from typing import Any, Literal

from voiced.config import Settings
from voiced.control.ops import NotInstalled
from voiced.launchd.control import Runner, plist_path, run_command
from voiced.watchdog import (
    Observation,
    ServiceSpec,
    ServiceState,
    Watchdog,
    launchd_view,
    poll_health,
)

State = Literal["running", "starting", "stopped", "degraded", "unknown"]

STATUS_TIMEOUT_S = 2.0


def service_state(obs: Observation, in_grace: bool) -> State:
    """Map one observation to the protocol's service state."""
    if obs.kind == "ok":
        return "running"
    if obs.kind == "starting":
        return "starting"
    if obs.kind in ("degraded", "stalled", "error"):
        return "degraded"
    # unreachable
    if obs.loaded is False:
        return "stopped"
    if obs.loaded is None:
        return "unknown"
    return "starting" if in_grace else "degraded"


def service_view(
    obs: Observation,
    state: ServiceState | None,
    *,
    in_grace: bool,
    engine: str | None,
    model: str | None,
) -> dict[str, Any]:
    """One service's entry in the ``status`` result."""
    payload = obs.payload
    if payload is not None:
        engine = str(payload.get("engine") or engine or "") or None
        model = str(payload.get("model") or model or "") or None
    uptime = payload.get("uptime_s") if payload else None
    return {
        "state": service_state(obs, in_grace),
        "pid": obs.pid,
        "health": payload,
        "engine": engine,
        "model": model,
        "warm": bool(payload and payload.get("model_loaded")),
        "uptime_s": float(uptime) if isinstance(uptime, int | float) else None,
        "restarts": state.restarts if state else 0,
    }


class WatchdogSupervisor:
    def __init__(
        self,
        dog: Watchdog,
        settings: Settings,
        *,
        specs_for: Callable[[Settings], Sequence[ServiceSpec]],
        runner: Runner = run_command,
        installed: Callable[[str], bool] | None = None,
        observe: Callable[[ServiceSpec], Observation] | None = None,
    ) -> None:
        self.dog = dog
        self.settings = settings
        self.specs_for = specs_for
        self.runner = runner
        self.installed = installed or (lambda label: plist_path(label).is_file())
        self._observe = observe or self._observe_now
        self.started = time.monotonic()

    def _observe_now(self, spec: ServiceSpec) -> Observation:
        obs = poll_health(spec.health_url, timeout_s=STATUS_TIMEOUT_S, api_key=spec.api_key)
        loaded, pid = launchd_view(spec.label, self.runner)
        return Observation(obs.kind, obs.detail, obs.uptime_s, obs.payload, loaded, pid)

    def status(self) -> dict[str, Any]:
        services: dict[str, Any] = {}
        configured = {
            "stt": (self.settings.stt.engine, self.settings.stt.model),
            "tts": (self.settings.tts.engine, self.settings.tts.model),
        }
        for spec in self.dog.services:
            obs = self._observe(spec)
            state = self.dog.states.get(spec.name)
            in_grace = (
                state is not None
                and self.dog.clock() - state.grace_started_at < self.dog.policy.startup_grace_s
            )
            engine, model = configured.get(spec.name, (None, None))
            services[spec.name] = service_view(
                obs, state, in_grace=in_grace, engine=engine, model=model
            )
        services["watchdog"] = {
            "state": "running",
            "pid": os.getpid(),
            "health": None,
            "engine": None,
            "model": None,
            "warm": True,
            "uptime_s": round(time.monotonic() - self.started, 1),
            "restarts": 0,
        }
        installed = all(self.installed(spec.label) for spec in self.dog.services)
        return {"installed": installed, "services": services}

    def restart(self, service: Literal["stt", "tts"]) -> str:
        try:
            result = self.dog.restart_now(service, "requested over the control socket")
        except KeyError:
            raise NotInstalled(f"{service} is not supervised by this watchdog") from None
        if not result.done:
            raise NotInstalled(result.detail)
        return result.detail

    def settings_changed(self, settings: Settings) -> None:
        """Keep the probe's API key and URLs in step with the file. The
        services themselves pick the change up when they restart."""
        self.settings = settings
        specs = list(self.specs_for(settings))
        with self.dog.lock:
            self.dog.services = specs
