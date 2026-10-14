"""Hang watchdog for the two service daemons.

Runs as its own LaunchDaemon (``voiced watchdog``). Every ``interval_s`` it
polls each service's ``/health`` and decides, per service, whether to restart
it. Restarts go through ``launchctl kickstart -k`` with a kill-the-pid
fallback (ADR 0005). The decision logic is a pure function of the previous
state and the new observation so it can be unit-tested without a network.

This module deliberately imports only the standard library: the watchdog is
the thing that must keep working when everything else is wedged, and it must
start fast. Settings are converted to plain values by the CLI.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

log = logging.getLogger("voiced.watchdog")

Kind = Literal["ok", "starting", "degraded", "unreachable", "error", "stalled"]
Action = Literal["none", "restart", "cooldown"]


@dataclass(frozen=True)
class Policy:
    interval_s: float = 15.0
    timeout_s: float = 5.0
    failures_before_restart: int = 3
    startup_grace_s: float = 300.0
    degraded_window_s: float = 120.0
    restart_cooldown_s: float = 120.0
    probe: bool = False
    probe_timeout_s: float = 20.0


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
    failures: int = 0
    degraded_since: float | None = None
    last_restart_at: float | None = None
    restarts: int = 0
    last_kind: Kind | None = None
    awaiting_recovery: bool = False


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str


# Pure decision logic ------------------------------------------------------


def decide(state: ServiceState, obs: Observation, now: float, policy: Policy) -> Decision:
    """Update ``state`` with ``obs`` and say what to do.

    Rules:

    * ``ok`` resets the failure counter and the degraded timer.
    * ``starting`` is tolerated while the service's own uptime is below
      ``startup_grace_s``; beyond that the model load is considered hung.
    * ``degraded`` (service answers, last inference failed) starts a timer;
      longer than ``degraded_window_s`` restarts.
    * ``unreachable``, ``error`` and ``stalled`` count as failures; reaching
      ``failures_before_restart`` consecutive failures restarts.
    * A restart within ``restart_cooldown_s`` of the previous one is deferred
      (``cooldown``); counters are kept so it fires right after the cooldown.
    """
    previous = state.last_kind
    state.last_kind = obs.kind
    wanted: str | None = None

    if obs.kind == "ok":
        recovered = (
            state.failures > 0 or state.degraded_since is not None or state.awaiting_recovery
        )
        state.failures = 0
        state.degraded_since = None
        state.awaiting_recovery = False
        reason = "healthy again" if recovered else "healthy"
        return Decision("none", reason)

    if obs.kind == "starting":
        state.failures = 0
        state.degraded_since = None
        if obs.uptime_s is not None and obs.uptime_s > policy.startup_grace_s:
            wanted = f"still starting after {obs.uptime_s:.0f}s (grace {policy.startup_grace_s:g}s)"
        else:
            return Decision("none", f"starting ({obs.uptime_s or 0:.0f}s)")

    elif obs.kind == "degraded":
        state.failures = 0
        if state.degraded_since is None:
            state.degraded_since = now
        elapsed = now - state.degraded_since
        if elapsed > policy.degraded_window_s:
            wanted = f"degraded for {elapsed:.0f}s (window {policy.degraded_window_s:g}s)"
        else:
            return Decision("none", f"degraded for {elapsed:.0f}s: {obs.detail}")

    else:  # unreachable, error, stalled
        state.degraded_since = None
        state.failures += 1
        if state.failures >= policy.failures_before_restart:
            wanted = f"{obs.kind} {state.failures}x in a row: {obs.detail}"
        else:
            return Decision(
                "none",
                f"{obs.kind} ({state.failures}/{policy.failures_before_restart}): {obs.detail}",
            )

    assert wanted is not None
    if state.last_restart_at is not None:
        since = now - state.last_restart_at
        if since < policy.restart_cooldown_s:
            return Decision(
                "cooldown",
                f"{wanted}; last restart {since:.0f}s ago, cooldown {policy.restart_cooldown_s:g}s",
            )
    if previous != obs.kind:
        wanted += " (changed)"
    return Decision("restart", wanted)


def mark_restarted(state: ServiceState, now: float) -> None:
    state.last_restart_at = now
    state.restarts += 1
    state.failures = 0
    state.degraded_since = None
    state.awaiting_recovery = True


# Health polling (stdlib urllib) -------------------------------------------


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
    except TimeoutError as exc:
        return Observation("unreachable", f"timeout after {timeout_s:g}s: {exc}")
    except (urllib.error.URLError, OSError) as exc:
        return Observation("unreachable", str(exc.reason if hasattr(exc, "reason") else exc))
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
    started = time.monotonic()
    try:
        _request(
            url,
            timeout_s=timeout_s,
            api_key=spec.api_key,
            data=spec.probe.body,
            ctype=spec.probe.content_type,
        )
    except TimeoutError:
        return Observation("stalled", f"probe {spec.probe.path} timed out after {timeout_s:g}s")
    except (urllib.error.URLError, OSError) as exc:
        if "timed out" in str(exc).lower():
            return Observation("stalled", f"probe {spec.probe.path} timed out: {exc}")
        return None
    log.debug("%s probe answered in %.0f ms", spec.name, (time.monotonic() - started) * 1000)
    return None


def observe(spec: ServiceSpec, policy: Policy) -> Observation:
    """Health poll plus, when enabled and the service looks fine, the probe."""
    obs = poll_health(spec.health_url, timeout_s=policy.timeout_s, api_key=spec.api_key)
    if policy.probe and obs.kind == "ok":
        stalled = run_probe(spec, timeout_s=policy.probe_timeout_s)
        if stalled is not None:
            return stalled
    return obs


# Restarting ---------------------------------------------------------------

Restarter = Callable[[ServiceSpec], str]
"""Restarts a service and returns a one-line description of what it did."""


def _run(argv: Sequence[str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _pid_from_print(text: str) -> int | None:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("pid = "):
            try:
                return int(line[6:])
            except ValueError:
                return None
    return None


def launchctl_restart(
    spec: ServiceSpec, run: Callable[[Sequence[str]], tuple[int, str]] = _run
) -> str:
    """Restart a service without root.

    ``launchctl kickstart -k`` is the clean way, but for a non-root caller it
    is denied while the service has a running process (observed on macOS 15).
    Plain ``kickstart`` (start a non-running service, skipping the throttle)
    is allowed. So: try ``-k``; if that fails, SIGKILL the pid ourselves (the
    service runs under the same ``UserName`` as the watchdog, so the kill is
    permitted) and then ``kickstart`` so KeepAlive does not have to wait out
    ``ThrottleInterval``. See ADR 0005.
    """
    target = f"system/{spec.label}"
    code, out = run(["launchctl", "kickstart", "-k", target])
    if code == 0:
        return f"launchctl kickstart -k {target}"
    steps = [f"kickstart -k denied ({out or code})"]
    code2, printed = run(["launchctl", "print", target])
    pid = _pid_from_print(printed) if code2 == 0 else None
    if pid is not None:
        try:
            os.kill(pid, signal.SIGKILL)
            steps.append(f"sent SIGKILL to pid {pid}")
        except OSError as exc:
            steps.append(f"kill {pid} failed: {exc}")
    else:
        steps.append("no running pid")
    code3, out3 = run(["launchctl", "kickstart", target])
    steps.append(f"kickstart {'ok' if code3 == 0 else 'failed: ' + out3}")
    return "; ".join(steps)


# The loop -----------------------------------------------------------------


@dataclass
class Watchdog:
    services: Sequence[ServiceSpec]
    policy: Policy = field(default_factory=Policy)
    observe: Callable[[ServiceSpec, Policy], Observation] = observe
    restart: Restarter = launchctl_restart
    clock: Callable[[], float] = time.monotonic
    states: dict[str, ServiceState] = field(default_factory=dict)

    def state(self, spec: ServiceSpec) -> ServiceState:
        return self.states.setdefault(spec.name, ServiceState())

    def tick(self) -> dict[str, Decision]:
        """Observe every service once and act on the decisions."""
        decisions: dict[str, Decision] = {}
        for spec in self.services:
            now = self.clock()
            obs = self.observe(spec, self.policy)
            state = self.state(spec)
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


def configure_watchdog_logging(level: str = "info", log_dir: Path | None = None) -> None:
    """Stdlib-only logging: stderr always, plus a rotating file under ``log_dir``."""
    root = logging.getLogger()
    root.setLevel(level.upper())
    for handler in list(root.handlers):
        root.removeHandler(handler)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    root.addHandler(stream)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "watchdog.log", maxBytes=10 * 1024 * 1024, backupCount=5
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


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
