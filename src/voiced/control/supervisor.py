"""The watchdog as the control channel's :class:`~voiced.control.ops.Supervisor`.

Status comes from a fresh, probe-free observation of each service (``/health``
and ``launchctl print``) combined with what the watchdog remembers (restart
count, startup grace, and the engine and model each service was last started
with). Restarts go through :meth:`Watchdog.restart_now`, the same path the
watchdog's own decisions take.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any, Literal

from voiced.config import ServiceName
from voiced.control.ops import NotInstalled, ServiceUnavailable
from voiced.launchd.control import Runner, plist_path, run_command
from voiced.watchdog import (
    Observation,
    ServiceSpec,
    ServiceState,
    Watchdog,
    launchd_view,
    poll_health,
)

State = Literal["running", "starting", "stopped", "degraded", "missing_model", "unknown"]

STATUS_TIMEOUT_S = 2.0


def service_state(obs: Observation, in_grace: bool) -> State:
    """Map one observation to the protocol's service state."""
    if obs.kind == "ok":
        return "running"
    if obs.kind == "starting":
        return "starting"
    if obs.kind == "missing_model":
        return "missing_model"
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
        *,
        runner: Runner = run_command,
        installed: Callable[[str], bool] | None = None,
        observe: Callable[[ServiceSpec], Observation] | None = None,
    ) -> None:
        self.dog = dog
        self.runner = runner
        self.installed = installed or (lambda label: plist_path(label).is_file())
        self._observe = observe or self._observe_now
        self.started = time.monotonic()

    def _observe_now(self, spec: ServiceSpec) -> Observation:
        loaded, pid = launchd_view(spec.label, self.runner)  # first, as in watchdog.observe
        obs = poll_health(spec.health_url, timeout_s=STATUS_TIMEOUT_S, api_key=spec.api_key)
        return Observation(obs.kind, obs.detail, obs.uptime_s, obs.payload, loaded, pid)

    def status(self) -> dict[str, Any]:
        with self.dog.lock:
            specs = list(self.dog.services)
            states = {
                s.name: replace(self.dog.states[s.name]) for s in specs if s.name in self.dog.states
            }
            now = self.dog.clock()
        # In parallel: a hung service costs one check timeout, not one each.
        observed: list[Observation] = []
        if specs:
            with ThreadPoolExecutor(len(specs), thread_name_prefix="voiced-status") as pool:
                observed = list(pool.map(self._observe, specs))
        services: dict[str, Any] = {}
        for spec, obs in zip(specs, observed, strict=True):
            state = states.get(spec.name)
            in_grace = state is not None and state.in_grace(now, self.dog.policy.startup_grace_s)
            services[spec.name] = service_view(
                obs, state, in_grace=in_grace, engine=spec.engine or None, model=spec.model or None
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
        installed = all(self.installed(spec.label) for spec in specs)
        return {"installed": installed, "services": services}

    def check_restart(self, service: ServiceName) -> None:
        spec = self.dog.spec(service)
        if spec is None:
            raise NotInstalled(f"{service} is not supervised by this watchdog")
        if not self.installed(spec.label):
            raise NotInstalled(
                f"{spec.label} has no launchd plist; "
                "run `voiced install` and the sudo command it prints"
            )
        formula = self.dog.held_by_homebrew()
        if formula is not None:
            # Refused here, before the op answers, not later in the
            # background: the caller must hear it (ADR 0017).
            raise ServiceUnavailable(f"Homebrew is installing {formula}; try again when it is done")
        loaded, _pid = launchd_view(spec.label, self.runner)
        if loaded is False:
            raise ServiceUnavailable(
                f"{spec.label} is installed but not loaded in launchd; `voiced start {service}`"
            )

    def restart(self, service: Literal["stt", "tts"]) -> str:
        try:
            result = self.dog.restart_now(service, "requested over the control socket")
        except KeyError:
            raise NotInstalled(f"{service} is not supervised by this watchdog") from None
        if not result.done:
            raise ServiceUnavailable(result.detail)
        return result.detail
