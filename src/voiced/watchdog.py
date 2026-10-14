"""Hang watchdog for the two service daemons.

Runs as its own LaunchDaemon (``voiced watchdog``). Every ``interval_s`` it
polls each service's ``/health`` and decides, per service, whether to restart
it. The decision logic is a pure function of the previous state and the new
observation so it can be unit-tested without a network (ADR 0005).

This module and ``voiced.launchd.control`` import only the standard library:
the watchdog is the thing that must keep working when everything else is
wedged. The CLI converts ``Settings`` to a :class:`Policy` before handing it
over.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import signal
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from voiced.launchd.control import (
    Runner,
    kickstart_argv,
    parse_print_output,
    print_argv,
    run_command,
)

log = logging.getLogger("voiced.watchdog")

Kind = Literal["ok", "starting", "degraded", "unreachable", "error", "stalled"]
Action = Literal["none", "restart", "cooldown"]


@dataclass(frozen=True)
class Policy:
    """Mirror of ``WatchdogConfig``; build with ``Policy(**config.model_dump())``."""

    interval_s: float
    timeout_s: float
    failures_before_restart: int
    startup_grace_s: float
    degraded_window_s: float
    restart_cooldown_s: float
    stall_after_s: float
    probe: bool
    probe_timeout_s: float


@dataclass(frozen=True)
class ProbeRequest:
    """A small inference request whose timeout means "the GPU is wedged"."""

    path: str
    body: bytes
    content_type: str


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    label: str
    base_url: str
    api_key: str = ""
    probe: ProbeRequest | None = None

    @property
    def health_url(self) -> str:
        return f"{self.base_url}/health"


@dataclass(frozen=True)
class Observation:
    kind: Kind
    detail: str = ""
    uptime_s: float | None = None
    payload: dict[str, Any] | None = None


@dataclass
class ServiceState:
    """Per-service memory between polls.

    ``grace_started_at`` is when the current startup grace period began: the
    watchdog's own start, or the last restart it issued. While the service is
    loading its model the socket is not bound yet, so polls are refused; those
    must not count as failures during the grace period.
    """

    grace_started_at: float = 0.0
    failures: int = 0
    degraded_since: float | None = None
    last_restart_at: float | None = None
    restarts: int = 0
    awaiting_recovery: bool = False


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str


# Pure decision logic ------------------------------------------------------


def decide(state: ServiceState, obs: Observation, now: float, policy: Policy) -> Decision:
    """Update ``state`` with ``obs`` and say what to do.

    * ``ok`` resets the failure counter and the degraded timer.
    * ``starting`` (503, model loading) is tolerated while the service's own
      uptime is below ``startup_grace_s``; beyond that the load is hung.
    * ``unreachable`` within ``startup_grace_s`` of the watchdog start or of
      the last restart is the model loading before the socket is bound: not
      a failure.
    * ``degraded`` (answers, last inference failed) starts a timer; longer
      than ``degraded_window_s`` restarts.
    * ``unreachable``, ``error`` and ``stalled`` otherwise count as failures;
      ``failures_before_restart`` consecutive ones restart.
    * A restart within ``restart_cooldown_s`` of the previous one is deferred
      (``cooldown``); counters are kept so it fires right after the cooldown.
    """
    if obs.kind == "ok":
        recovered = (
            state.failures > 0 or state.degraded_since is not None or state.awaiting_recovery
        )
        state.failures = 0
        state.degraded_since = None
        state.awaiting_recovery = False
        return Decision("none", "healthy again" if recovered else "healthy")

    in_grace = now - state.grace_started_at < policy.startup_grace_s
    if obs.kind == "unreachable" and in_grace:
        state.failures = 0
        return Decision("none", f"unreachable during startup grace: {obs.detail}")

    if obs.kind == "starting":
        state.failures = 0
        state.degraded_since = None
        uptime = obs.uptime_s or 0.0
        if uptime <= policy.startup_grace_s:
            return Decision("none", f"starting ({uptime:.0f}s)")
        wanted = f"still starting after {uptime:.0f}s (grace {policy.startup_grace_s:g}s)"
    elif obs.kind == "degraded":
        state.failures = 0
        if state.degraded_since is None:
            state.degraded_since = now
        elapsed = now - state.degraded_since
        if elapsed <= policy.degraded_window_s:
            return Decision("none", f"degraded for {elapsed:.0f}s: {obs.detail}")
        wanted = f"degraded for {elapsed:.0f}s (window {policy.degraded_window_s:g}s)"
    else:  # unreachable, error, stalled
        state.degraded_since = None
        state.failures += 1
        if state.failures < policy.failures_before_restart:
            progress = f"{state.failures}/{policy.failures_before_restart}"
            return Decision("none", f"{obs.kind} ({progress}): {obs.detail}")
        wanted = f"{obs.kind} {state.failures}x in a row: {obs.detail}"

    if state.last_restart_at is not None:
        since = now - state.last_restart_at
        if since < policy.restart_cooldown_s:
            cooldown = f"last restart {since:.0f}s ago, cooldown {policy.restart_cooldown_s:g}s"
            return Decision("cooldown", f"{wanted}; {cooldown}")
    return Decision("restart", wanted)


def mark_restarted(state: ServiceState, now: float) -> None:
    state.last_restart_at = now
    state.grace_started_at = now
    state.restarts += 1
    state.failures = 0
    state.degraded_since = None
    state.awaiting_recovery = True


def detect_stall(obs: Observation, policy: Policy, now: float) -> Observation:
    """Turn an ``ok`` observation into ``stalled`` if the queue is stuck.

    The service reports ``queue_depth`` (waiting plus running requests) and
    ``last_inference_at`` (last completed inference, wall clock). A non-empty
    queue with no completion for ``stall_after_s`` means the running request
    never finishes: the GPU is wedged while ``/health`` still answers.
    """
    if obs.kind != "ok" or obs.payload is None:
        return obs
    depth = obs.payload.get("queue_depth")
    if not isinstance(depth, int) or depth <= 0:
        return obs
    last = obs.payload.get("last_inference_at")
    if not isinstance(last, int | float):
        # Nothing has completed yet: measure from process start.
        last = now - (obs.uptime_s or 0.0)
    idle = now - float(last)
    if idle > policy.stall_after_s:
        return Observation(
            "stalled",
            f"queue depth {depth}, no inference completed for {idle:.0f}s",
            obs.uptime_s,
            obs.payload,
        )
    return obs


# Health polling (stdlib urllib) -------------------------------------------


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    reason = getattr(exc, "reason", None)
    return isinstance(reason, TimeoutError)


def _request(
    url: str, *, timeout_s: float, api_key: str = "", data: bytes | None = None, ctype: str = ""
) -> tuple[int, bytes]:
    headers = {"User-Agent": "voiced-watchdog"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if data is not None:
        headers["Content-Type"] = ctype
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return int(resp.status), bytes(resp.read())
    except urllib.error.HTTPError as exc:
        return int(exc.code), bytes(exc.read())


def poll_health(url: str, *, timeout_s: float, api_key: str = "") -> Observation:
    """One ``GET /health``. Never raises."""
    try:
        code, body = _request(url, timeout_s=timeout_s, api_key=api_key)
    except (urllib.error.URLError, OSError) as exc:
        if _is_timeout(exc):
            return Observation("unreachable", f"timed out after {timeout_s:g}s")
        return Observation("unreachable", str(getattr(exc, "reason", exc)))
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return Observation("error", f"HTTP {code}, body is not JSON")
    if not isinstance(payload, dict):
        return Observation("error", f"HTTP {code}, unexpected body")
    uptime = payload.get("uptime_s")
    uptime_s = float(uptime) if isinstance(uptime, int | float) else None
    status = payload.get("status")
    if status == "ok" and code == 200:
        return Observation("ok", "", uptime_s, payload)
    if status == "starting":
        return Observation("starting", "model loading", uptime_s, payload)
    if status == "degraded":
        return Observation("degraded", str(payload.get("last_error") or ""), uptime_s, payload)
    return Observation("error", f"HTTP {code}, status={status!r}", uptime_s, payload)


def run_probe(spec: ServiceSpec, *, timeout_s: float) -> Observation | None:
    """Send the probe request. Only a timeout is a finding; anything else is None."""
    if spec.probe is None:
        return None
    url = f"{spec.base_url}{spec.probe.path}"
    try:
        _request(
            url,
            timeout_s=timeout_s,
            api_key=spec.api_key,
            data=spec.probe.body,
            ctype=spec.probe.content_type,
        )
    except (urllib.error.URLError, OSError) as exc:
        if _is_timeout(exc):
            return Observation("stalled", f"probe {spec.probe.path} timed out after {timeout_s:g}s")
    return None


def observe(spec: ServiceSpec, policy: Policy, now: float | None = None) -> Observation:
    """Health poll, stall detection from the payload, then the optional probe.

    The probe is only sent while the queue is empty: with requests in flight a
    probe timeout would just be queueing, and restarting would kill real work.
    """
    obs = poll_health(spec.health_url, timeout_s=policy.timeout_s, api_key=spec.api_key)
    obs = detect_stall(obs, policy, time.time() if now is None else now)
    if policy.probe and obs.kind == "ok" and obs.payload and obs.payload.get("queue_depth") == 0:
        stalled = run_probe(spec, timeout_s=policy.probe_timeout_s)
        if stalled is not None:
            return stalled
    return obs


# Restarting ---------------------------------------------------------------

Restarter = Callable[[ServiceSpec], str]
"""Restarts a service and returns a one-line description of what it did."""


def restart_service(spec: ServiceSpec, runner: Runner = run_command) -> str:
    """Restart without root: SIGKILL the pid, then ``launchctl kickstart``.

    ``launchctl kickstart -k`` is denied to a non-root caller while the job
    has a running process (ADR 0004). The service runs under the same
    ``UserName`` as the watchdog, so killing its pid is permitted; SIGKILL
    because a wedged Metal process may ignore SIGTERM. The plain ``kickstart``
    then starts it immediately instead of after ``ThrottleInterval``.
    """
    status = parse_print_output(spec.label, runner(print_argv(spec.label)))
    steps: list[str] = []
    if not status.loaded:
        return f"{spec.label} is not loaded in launchd; nothing to restart"
    if status.pid is None:
        steps.append("no running pid")
    else:
        try:
            os.kill(status.pid, signal.SIGKILL)
            steps.append(f"sent SIGKILL to pid {status.pid}")
        except OSError as exc:
            steps.append(f"kill {status.pid} failed: {exc}")
    result = runner(kickstart_argv(spec.label))
    steps.append("kickstart ok" if result.ok else f"kickstart failed: {result.output}")
    return "; ".join(steps)


# The loop -----------------------------------------------------------------


@dataclass
class Watchdog:
    services: Sequence[ServiceSpec]
    policy: Policy
    observe: Callable[[ServiceSpec, Policy], Observation] = observe
    restart: Restarter = restart_service
    clock: Callable[[], float] = time.monotonic
    states: dict[str, ServiceState] = field(default_factory=dict)

    def state(self, spec: ServiceSpec) -> ServiceState:
        if spec.name not in self.states:
            self.states[spec.name] = ServiceState(grace_started_at=self.clock())
        return self.states[spec.name]

    def tick(self) -> dict[str, Decision]:
        """Observe every service once and act on the decisions."""
        decisions: dict[str, Decision] = {}
        for spec in self.services:
            state = self.state(spec)
            now = self.clock()
            obs = self.observe(spec, self.policy)
            decision = decide(state, obs, now, self.policy)
            decisions[spec.name] = decision
            if decision.action == "restart":
                log.warning("%s: restarting: %s", spec.name, decision.reason)
                mark_restarted(state, now)
                log.warning("%s: %s (restart #%d)", spec.name, self.restart(spec), state.restarts)
            elif decision.action == "cooldown":
                log.warning("%s: not restarting: %s", spec.name, decision.reason)
            elif obs.kind != "ok" or decision.reason == "healthy again":
                log.info("%s: %s", spec.name, decision.reason)
            else:
                log.debug("%s: %s", spec.name, decision.reason)
        return decisions

    def run_forever(self, stop: Callable[[], bool] = lambda: False) -> None:
        for spec in self.services:
            self.state(spec)  # start every grace period now
        log.info(
            "watchdog started: services=%s interval=%gs failures_before_restart=%d probe=%s",
            [s.name for s in self.services],
            self.policy.interval_s,
            self.policy.failures_before_restart,
            self.policy.probe,
        )
        while not stop():
            started = self.clock()
            try:
                self.tick()
            except Exception:
                log.exception("tick failed")
            elapsed = self.clock() - started
            time.sleep(max(0.0, self.policy.interval_s - elapsed))


def configure_watchdog_logging(level: str = "info", log_file: Path | None = None) -> None:
    """Stdlib-only logging: a rotating file when given, else stderr.

    Under launchd stderr is captured unrotated by the plist, so the file is
    the only handler there; stderr is for running ``voiced watchdog`` by hand.
    """
    root = logging.getLogger()
    root.setLevel(level.upper())
    for existing in list(root.handlers):
        root.removeHandler(existing)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=5
        )
    else:
        handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root.addHandler(handler)


def silent_wav(duration_s: float = 0.2, rate: int = 16000) -> bytes:
    """A tiny valid WAV of silence for the STT probe."""
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(duration_s * rate))
    return buf.getvalue()


def multipart(fields: dict[str, str], filename: str, file_bytes: bytes) -> tuple[bytes, str]:
    """Minimal multipart/form-data encoder (one file field named ``file``)."""
    boundary = "voicedwatchdog7f3a9c"
    parts: list[bytes] = []
    for name, value in fields.items():
        head = f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
        parts.append(f"{head}{value}\r\n".encode())
    parts.append(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{filename}"\r\nContent-Type: audio/wav\r\n\r\n'
        ).encode()
        + file_bytes
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"
