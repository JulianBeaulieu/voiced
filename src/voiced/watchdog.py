"""Hang watchdog for the two service daemons.

Runs as its own LaunchDaemon (``voiced watchdog``). Every ``interval_s`` it
polls each service's ``/health`` and decides, per service, whether to restart
it. The decision logic is a pure function of the previous state and the new
observation so it can be unit-tested without a network (ADR 0012).

This module and ``voiced.launchd.control`` import only the standard library
at runtime: the watchdog is the thing that must keep working when everything
else is wedged. The policy object is the ``WatchdogConfig`` section of the
settings, typed here under ``TYPE_CHECKING`` only.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import logging.handlers
import os
import signal
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from voiced.launchd.control import (
    Runner,
    kickstart_argv,
    parse_print_output,
    print_argv,
    print_service,
    run_command,
)
from voiced.launchd.plist import LOG_BACKUP_COUNT, LOG_FORMAT, LOG_MAX_BYTES

if TYPE_CHECKING:
    from voiced.config import WatchdogConfig

log = logging.getLogger("voiced.watchdog")

Kind = Literal["ok", "starting", "degraded", "unreachable", "error", "stalled"]
Action = Literal["none", "restart", "cooldown"]


@dataclass(frozen=True)
class ProbeRequest:
    """A small inference request whose timeout means "the GPU is wedged"."""

    path: str
    body: bytes
    content_type: str


def default_probe(service: str, model: str, voice: str = "af_heart") -> ProbeRequest:
    """The smallest real request each service accepts."""
    if service == "stt":
        body, ctype = multipart({"model": model}, "probe.wav", silent_wav())
        return ProbeRequest("/v1/audio/transcriptions", body, ctype)
    payload = {"model": model, "input": "ok", "voice": voice, "response_format": "pcm"}
    return ProbeRequest("/v1/audio/speech", json.dumps(payload).encode(), "application/json")


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
    # From ``launchctl print``: is the job loaded, and which pid runs it now.
    loaded: bool | None = None
    pid: int | None = None


@dataclass
class ServiceState:
    """Per-service memory between polls.

    ``grace_started_at`` is when the current startup grace period began: the
    watchdog's own start, the last restart it issued, or the moment launchd
    gave the service a new pid (crash restart under ``KeepAlive``). While the
    service loads its model the socket is not bound yet, so polls are
    refused; those must not count as failures during the grace period.

    ``busy_since`` is when the queue was first seen non-empty without a
    completed inference since; the stall timer measures from there, not from
    the last completion, so the first long request after an idle period is
    not mistaken for a hang.
    """

    grace_started_at: float = 0.0
    failures: int = 0
    degraded_since: float | None = None
    last_restart_at: float | None = None
    restarts: int = 0
    awaiting_recovery: bool = False
    last_pid: int | None = None
    busy_since: float | None = None
    last_completion: float | None = None
    not_loaded_reported: bool = False


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str
    recovered: bool = False
    quiet: bool = False  # repeat of something already reported; log at DEBUG


# Pure decision logic ------------------------------------------------------


def classify_stall(
    state: ServiceState, obs: Observation, policy: WatchdogConfig, now: float
) -> Observation:
    """Turn an ``ok`` observation into ``stalled`` if the queue is stuck.

    ``/health`` reports ``queue_depth`` (waiting plus running) and
    ``last_inference_at`` (last completed inference). The queue is stuck when
    it has been non-empty for ``stall_after_s`` with no completion in that
    time. Progress (a newer ``last_inference_at``) or an empty queue resets
    the timer; a poll that does not answer at all leaves it alone, so a
    service that is wedged and only answers ``/health`` every other poll is
    still caught.
    """
    if obs.kind != "ok" or obs.payload is None:
        return obs
    depth = obs.payload.get("queue_depth")
    completion = obs.payload.get("last_inference_at")
    completion_f = float(completion) if isinstance(completion, int | float) else None
    if not isinstance(depth, int) or depth <= 0:
        state.busy_since = None
        state.last_completion = completion_f
        return obs
    if state.busy_since is None or (
        completion_f is not None and completion_f != state.last_completion
    ):
        state.busy_since = now
    state.last_completion = completion_f
    stuck_for = now - state.busy_since
    if stuck_for > policy.stall_after_s:
        return Observation(
            "stalled",
            f"queue depth {depth}, no inference completed for {stuck_for:.0f}s",
            obs.uptime_s,
            obs.payload,
            obs.loaded,
            obs.pid,
        )
    return obs


def decide(state: ServiceState, obs: Observation, now: float, policy: WatchdogConfig) -> Decision:
    """Update ``state`` with ``obs`` and say what to do.

    * A new pid from launchd (crash restart under KeepAlive) restarts the
      grace period: the new process is loading its model.
    * ``ok`` resets the failure counter and the degraded timer.
    * ``unreachable`` within ``startup_grace_s`` of the grace start is the
      model loading before the socket is bound: not a failure.
    * ``starting`` (503, model loading) is tolerated while the service's own
      uptime is below ``startup_grace_s``; beyond that the load is hung.
    * ``degraded`` (answers, last inference failed) starts a timer; longer
      than ``degraded_window_s`` restarts.
    * ``unreachable``, ``error`` and ``stalled`` otherwise count as failures;
      ``failures_before_restart`` consecutive ones restart.
    * A service that launchd does not have loaded (after ``voiced stop``) is
      never restarted; this is reported once.
    * A restart within ``restart_cooldown_s`` of the previous one is deferred
      (``cooldown``); counters are kept so it fires right after the cooldown.
    """
    if obs.pid is not None and obs.pid != state.last_pid:
        if state.last_pid is not None:
            state.grace_started_at = now
            state.failures = 0
            state.busy_since = None
        state.last_pid = obs.pid
    obs = classify_stall(state, obs, policy, now)

    if obs.kind == "ok":
        recovered = (
            state.failures > 0 or state.degraded_since is not None or state.awaiting_recovery
        )
        state.failures = 0
        state.degraded_since = None
        state.awaiting_recovery = False
        state.not_loaded_reported = False
        return Decision("none", "healthy again" if recovered else "healthy", recovered)

    if obs.loaded is False:
        state.failures = 0
        if state.not_loaded_reported:
            return Decision("none", "not loaded in launchd", quiet=True)
        state.not_loaded_reported = True
        return Decision("none", f"{obs.kind} and not loaded in launchd (stopped?); not restarting")

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
    state.busy_since = None
    state.awaiting_recovery = True


# Health polling (stdlib urllib) -------------------------------------------


def _request(
    url: str, *, timeout_s: float, api_key: str = "", data: bytes | None = None, ctype: str = ""
) -> tuple[int, bytes]:
    """One HTTP request. Raises ``TimeoutError`` for any kind of timeout,
    ``OSError`` (including ``URLError``) for other transport failures."""
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
    except TimeoutError as exc:  # socket timeout while reading the body
        raise TimeoutError(f"timed out after {timeout_s:g}s") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):  # timeout while connecting
            raise TimeoutError(f"timed out after {timeout_s:g}s") from exc
        raise


def poll_health(url: str, *, timeout_s: float, api_key: str = "") -> Observation:
    """One ``GET /health``. Never raises."""
    try:
        code, body = _request(url, timeout_s=timeout_s, api_key=api_key)
    except TimeoutError as exc:
        return Observation("unreachable", str(exc))
    except OSError as exc:
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
    try:
        _request(
            f"{spec.base_url}{spec.probe.path}",
            timeout_s=timeout_s,
            api_key=spec.api_key,
            data=spec.probe.body,
            ctype=spec.probe.content_type,
        )
    except TimeoutError as exc:
        return Observation("stalled", f"probe {spec.probe.path} {exc}")
    except OSError:
        return None
    return None


NOT_FOUND_EXIT = 113  # launchctl print: "Could not find service"


def launchd_view(label: str, runner: Runner) -> tuple[bool | None, int | None]:
    """``(loaded, pid)`` from ``launchctl print``.

    Only the explicit "not found" exit means not loaded. Any other failure
    (timeout, launchctl missing) is unknown, so the caller keeps counting
    failures instead of treating the service as stopped.
    """
    result = runner(print_argv(label))
    if result.returncode == NOT_FOUND_EXIT:
        return False, None
    if not result.ok:
        return None, None
    return True, parse_print_output(label, result).pid


def observe(spec: ServiceSpec, policy: WatchdogConfig, runner: Runner = run_command) -> Observation:
    """Health poll, launchd pid, then the optional probe.

    The probe is only sent while the queue is empty: with requests in flight a
    probe timeout would just be queueing, and restarting would kill real work.
    """
    obs = poll_health(spec.health_url, timeout_s=policy.timeout_s, api_key=spec.api_key)
    loaded, pid = launchd_view(spec.label, runner)
    obs = Observation(obs.kind, obs.detail, obs.uptime_s, obs.payload, loaded, pid)
    if policy.probe and obs.kind == "ok" and obs.payload and obs.payload.get("queue_depth") == 0:
        stalled = run_probe(spec, timeout_s=policy.probe_timeout_s)
        if stalled is not None:
            return Observation(
                stalled.kind, stalled.detail, obs.uptime_s, obs.payload, obs.loaded, obs.pid
            )
    return obs


# Restarting ---------------------------------------------------------------


@dataclass(frozen=True)
class RestartResult:
    done: bool
    detail: str


Restarter = Callable[[ServiceSpec], RestartResult]


def restart_service(spec: ServiceSpec, runner: Runner = run_command) -> RestartResult:
    """Restart without root: SIGKILL the pid, then ``launchctl kickstart``.

    ``launchctl kickstart -k`` is denied to a non-root caller while the job
    has a running process (ADR 0004). The service runs under the same
    ``UserName`` as the watchdog, so killing its pid is permitted; SIGKILL
    because a wedged Metal process may ignore SIGTERM. The plain ``kickstart``
    then starts it immediately instead of after ``ThrottleInterval``.
    """
    status = print_service(spec.label, runner)
    if not status.loaded:
        return RestartResult(False, f"{spec.label} is not loaded in launchd; nothing to restart")
    steps: list[str] = []
    pid = status.pid
    if pid is None:
        steps.append("no running pid")
    elif pid <= 1 or pid == os.getpid():
        steps.append(f"refusing to kill pid {pid}")
    else:
        try:
            os.kill(pid, signal.SIGKILL)
            steps.append(f"sent SIGKILL to pid {pid}")
        except OSError as exc:
            steps.append(f"kill {pid} failed: {exc}")
    result = runner(kickstart_argv(spec.label))
    steps.append("kickstart ok" if result.ok else f"kickstart failed: {result.output}")
    return RestartResult(True, "; ".join(steps))


# The loop -----------------------------------------------------------------


@dataclass
class Watchdog:
    services: Sequence[ServiceSpec]
    policy: WatchdogConfig
    runner: Runner = run_command
    observe: Callable[[ServiceSpec, WatchdogConfig], Observation] | None = None
    restart: Restarter | None = None
    clock: Callable[[], float] = time.monotonic
    states: dict[str, ServiceState] = field(default_factory=dict)
    # Last observation per service, for the control channel's status.
    observations: dict[str, Observation] = field(default_factory=dict)
    # A tick and a restart asked for over the control socket run in background
    # threads; they must not interleave on the same ServiceState.
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def _observe(self, spec: ServiceSpec) -> Observation:
        if self.observe is not None:
            return self.observe(spec, self.policy)
        return observe(spec, self.policy, self.runner)

    def _restart(self, spec: ServiceSpec) -> RestartResult:
        if self.restart is not None:
            return self.restart(spec)
        return restart_service(spec, self.runner)

    def state(self, spec: ServiceSpec) -> ServiceState:
        if spec.name not in self.states:
            self.states[spec.name] = ServiceState(grace_started_at=self.clock())
        return self.states[spec.name]

    def _restart_logged(
        self, spec: ServiceSpec, state: ServiceState, now: float, reason: str
    ) -> RestartResult:
        """The one restart path: watchdog decisions and control requests."""
        log.warning("%s: restarting: %s", spec.name, reason)
        result = self._restart(spec)
        if result.done:
            mark_restarted(state, now)
            log.warning("%s: %s (restart #%d)", spec.name, result.detail, state.restarts)
        else:
            state.failures = 0
            log.warning("%s: %s", spec.name, result.detail)
        return result

    def restart_now(self, name: str, reason: str = "requested") -> RestartResult:
        """Restart a service on request, outside the poll cycle.

        Goes through the same path as a watchdog decision and records it the
        same way, so the startup grace period and the cooldown apply to the
        watchdog's own decisions afterwards. The cooldown does not hold back
        a requested restart.
        """
        spec = next((s for s in self.services if s.name == name), None)
        if spec is None:
            raise KeyError(name)
        with self.lock:
            return self._restart_logged(spec, self.state(spec), self.clock(), reason)

    def tick(self) -> dict[str, Decision]:
        """Observe every service once and act on the decisions."""
        with self.lock:
            return self._tick()

    def _tick(self) -> dict[str, Decision]:
        decisions: dict[str, Decision] = {}
        for spec in self.services:
            state = self.state(spec)
            now = self.clock()
            obs = self._observe(spec)
            self.observations[spec.name] = obs
            decision = decide(state, obs, now, self.policy)
            decisions[spec.name] = decision
            if decision.action == "restart":
                self._restart_logged(spec, state, now, decision.reason)
            elif decision.action == "cooldown":
                log.warning("%s: not restarting: %s", spec.name, decision.reason)
            elif decision.quiet:
                log.debug("%s: %s", spec.name, decision.reason)
            elif obs.kind != "ok" or decision.recovered:
                log.info("%s: %s", spec.name, decision.reason)
            else:
                log.debug("%s: %s", spec.name, decision.reason)
        return decisions

    def _announce(self) -> None:
        for spec in self.services:
            self.state(spec)  # start every grace period now
        log.info(
            "watchdog started: services=%s interval=%gs failures_before_restart=%d probe=%s",
            [s.name for s in self.services],
            self.policy.interval_s,
            self.policy.failures_before_restart,
            self.policy.probe,
        )

    async def run_async(self, stop: asyncio.Event) -> None:
        """:meth:`run_forever` on an event loop, so the control socket can be
        served next to it. Each tick runs in a background thread (it blocks on
        HTTP and ``launchctl``); ``stop`` ends the loop between ticks."""
        self._announce()
        while not stop.is_set():
            started = self.clock()
            try:
                await asyncio.to_thread(self.tick)
            except Exception:
                log.exception("tick failed")
            elapsed = self.clock() - started
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), max(0.0, self.policy.interval_s - elapsed))

    def run_forever(self, stop: Callable[[], bool] = lambda: False) -> None:
        self._announce()
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
    formatter = logging.Formatter(LOG_FORMAT)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT
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
