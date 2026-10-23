"""Watchdog decision logic and health polling against a fake endpoint."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterator, Sequence
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from voiced import watchdog as wd
from voiced.config import WatchdogConfig
from voiced.launchd.control import CommandResult
from voiced.watchdog import (
    Decision,
    Observation,
    RestartResult,
    ServiceSpec,
    ServiceState,
    Watchdog,
    decide,
    mark_restarted,
    poll_health,
    restart_service,
)

POLICY = WatchdogConfig(
    interval_s=1,
    timeout_s=0.5,
    failures_before_restart=3,
    startup_grace_s=100,
    degraded_window_s=60,
    restart_cooldown_s=120,
    stall_after_s=30,
    probe=False,
    probe_timeout_s=1,
)
# Every test below starts its clock after the initial grace period.
T0 = 1000.0


def ok(uptime: float = 10, pid: int | None = 500, **payload: Any) -> Observation:
    return Observation(
        "ok", "", uptime, {"status": "ok", "queue_depth": 0, **payload}, loaded=True, pid=pid
    )


def down(detail: str = "connection refused", pid: int | None = 500) -> Observation:
    return Observation("unreachable", detail, loaded=True, pid=pid)


def fresh() -> ServiceState:
    return ServiceState(grace_started_at=0.0, last_pid=500)


# decide() ------------------------------------------------------------------


def test_healthy_is_a_no_op() -> None:
    state = fresh()
    assert decide(state, ok(), T0, POLICY) == Decision("none", "healthy", False)
    assert state.failures == 0


def test_consecutive_failures_restart() -> None:
    state = fresh()
    assert decide(state, down(), T0, POLICY).action == "none"
    assert decide(state, down(), T0 + 1, POLICY).action == "none"
    d = decide(state, down(), T0 + 2, POLICY)
    assert d.action == "restart"
    assert "3x in a row" in d.reason


def test_refused_during_startup_grace_is_not_a_failure() -> None:
    """The socket is not bound while the model loads; polls are refused."""
    state = ServiceState(grace_started_at=T0, last_pid=500)
    for t in range(0, 99, 7):
        d = decide(state, down(), T0 + t, POLICY)
        assert d.action == "none", t
        assert "startup grace" in d.reason
    assert state.failures == 0
    for t in (101, 102):
        assert decide(state, down(), T0 + t, POLICY).action == "none"
    assert decide(state, down(), T0 + 103, POLICY).action == "restart"


def test_grace_restarts_after_a_restart() -> None:
    state = fresh()
    for t in (0, 1, 2):
        d = decide(state, down(), T0 + t, POLICY)
    assert d.action == "restart"
    mark_restarted(state, T0 + 2)
    for t in (10, 20, 30):
        d = decide(state, down(), T0 + t, POLICY)
        assert d.action == "none" and "startup grace" in d.reason
    assert state.failures == 0


def test_new_pid_from_launchd_restarts_grace() -> None:
    """A KeepAlive restart after a crash reloads the model; do not count it."""
    state = fresh()
    decide(state, ok(pid=500), T0, POLICY)
    decide(state, down(pid=None), T0 + 15, POLICY)  # crashed, launchd respawning
    # New pid appears, still refused while loading: grace restarts at T0 + 30.
    for t in (30, 45, 60, 100, 120):
        d = decide(state, down(pid=501), T0 + t, POLICY)
        assert d.action == "none", t
    assert state.last_pid == 501
    # Grace from the pid change (T0 + 30) ends at T0 + 130.
    for t in (131, 132):
        assert decide(state, down(pid=501), T0 + t, POLICY).action == "none"
    assert decide(state, down(pid=501), T0 + 133, POLICY).action == "restart"


def test_new_pid_refused_while_loading_is_not_a_failure() -> None:
    state = fresh()
    for t in range(0, 99, 15):
        d = decide(state, down(pid=501), T0 + t, POLICY)
        assert d.action == "none" and "startup grace" in d.reason, t
    assert state.failures == 0


def test_hang_after_first_ok_ends_grace_for_new_pid() -> None:
    """Loaded, answered, then hung (SIGSTOP): count failures at once."""
    state = fresh()
    decide(state, down(pid=501), T0, POLICY)  # launchd respawned it; grace from T0
    assert decide(state, ok(pid=501), T0 + 3, POLICY).action == "none"
    for t in (18, 33):
        d = decide(state, down("timed out after 5s", pid=501), T0 + t, POLICY)
        assert d.action == "none" and "startup grace" not in d.reason, t
    assert state.failures == 2
    d = decide(state, down("timed out after 5s", pid=501), T0 + 48, POLICY)
    assert d.action == "restart" and "3x in a row" in d.reason
    assert 48 < POLICY.startup_grace_s


def test_hang_after_first_ok_ends_grace_from_watchdog_start() -> None:
    state = ServiceState(grace_started_at=T0)  # the watchdog just started
    assert decide(state, ok(pid=500), T0, POLICY).action == "none"
    assert decide(state, down(pid=500), T0 + 15, POLICY).action == "none"
    assert decide(state, down(pid=500), T0 + 30, POLICY).action == "none"
    assert decide(state, down(pid=500), T0 + 45, POLICY).action == "restart"


def test_restart_reopens_grace_after_an_ok() -> None:
    state = ServiceState(grace_started_at=T0, last_pid=500)
    decide(state, ok(pid=500), T0, POLICY)
    assert not state.in_grace(T0 + 1, POLICY.startup_grace_s)
    mark_restarted(state, T0 + 200)
    assert state.in_grace(T0 + 201, POLICY.startup_grace_s)
    for t in (210, 225, 240, 255):
        d = decide(state, down(pid=600), T0 + t, POLICY)
        assert d.action == "none" and "startup grace" in d.reason, t
    decide(state, ok(pid=600), T0 + 270, POLICY)
    assert not state.in_grace(T0 + 271, POLICY.startup_grace_s)


def test_first_pid_seen_starts_grace() -> None:
    """Stopped when the watchdog started; `voiced start` long after: still loading."""
    state = ServiceState(grace_started_at=T0)
    stopped = Observation("unreachable", "refused", loaded=False, pid=None)
    decide(state, stopped, T0, POLICY)
    for t in (400, 415, 430, 445, 460):
        d = decide(state, down(pid=700), T0 + t, POLICY)
        assert d.action == "none" and "startup grace" in d.reason, t
    assert state.failures == 0


def test_answer_with_model_loaded_ends_grace() -> None:
    """A degraded answer, or ok turned stalled, also proves the load finished."""
    for kind, status in (("degraded", "degraded"), ("stalled", "ok")):  # stalled: probe timeout
        payload = {"status": status, "model_loaded": True, "queue_depth": 0}
        first = Observation(kind, "boom", 10, payload, loaded=True, pid=501)  # type: ignore[arg-type]
        state = fresh()
        decide(state, down(pid=501), T0, POLICY)  # new pid: grace from T0
        assert decide(state, first, T0 + 1, POLICY).reason.startswith(kind)
        assert not state.in_grace(T0 + 2, POLICY.startup_grace_s)
        assert "startup grace" not in decide(state, down(pid=501), T0 + 45, POLICY).reason


def test_starting_does_not_end_grace() -> None:
    state = fresh()
    loading = Observation(
        "starting", "model loading", 5, {"status": "starting", "model_loaded": False}, True, 501
    )
    decide(state, loading, T0, POLICY)
    assert state.in_grace(T0 + 15, POLICY.startup_grace_s)
    assert "startup grace" in decide(state, down(pid=501), T0 + 15, POLICY).reason


def test_unloaded_service_is_never_restarted_and_reported_once() -> None:
    state = fresh()
    stopped = Observation("unreachable", "refused", loaded=False, pid=None)
    first = decide(state, stopped, T0, POLICY)
    assert first.action == "none" and "not loaded in launchd (stopped?)" in first.reason
    for t in range(1, 50):
        d = decide(state, stopped, T0 + t, POLICY)
        assert d == Decision("none", "not loaded in launchd", quiet=True)
    assert state.failures == 0
    assert decide(state, ok(), T0 + 60, POLICY).recovered is False
    assert decide(state, stopped, T0 + 61, POLICY).reason.endswith("not restarting")


def test_a_missing_model_is_never_restarted_and_reported_once_per_process() -> None:
    """A restart cannot download it (ADR 0022), however long it lasts."""
    state = fresh()
    missing = Observation(
        "missing_model", "org/m is not downloaded", 900, {"status": "missing_model"}, True, 500
    )
    first = decide(state, missing, T0, POLICY)
    assert first == Decision(
        "none", "model not downloaded, not restarting: org/m is not downloaded"
    )
    for t in range(1, 500, 50):
        d = decide(state, missing, T0 + t, POLICY)
        assert d.action == "none" and d.quiet
    assert state.failures == 0
    restarted = Observation("missing_model", "x", 1, {"status": "missing_model"}, True, 501)
    assert decide(state, restarted, T0 + 600, POLICY).quiet is False  # a new process


def test_recovery_resets_the_counter() -> None:
    state = fresh()
    decide(state, down(), T0, POLICY)
    decide(state, down(), T0 + 1, POLICY)
    d = decide(state, ok(), T0 + 2, POLICY)
    assert d.recovered and d.reason == "healthy again"
    assert state.failures == 0
    decide(state, down(), T0 + 3, POLICY)
    assert decide(state, down(), T0 + 4, POLICY).action == "none"


def test_degraded_restarts_only_after_window() -> None:
    state = fresh()
    degraded = Observation("degraded", "RuntimeError: metal", 50, {"status": "degraded"}, True, 500)
    assert decide(state, degraded, T0, POLICY).action == "none"
    assert decide(state, degraded, T0 + 30, POLICY).action == "none"
    d = decide(state, degraded, T0 + 61, POLICY)
    assert d.action == "restart"
    assert "degraded for 61s" in d.reason


def test_ok_clears_degraded_timer() -> None:
    state = fresh()
    degraded = Observation("degraded", "x", 1, {}, True, 500)
    decide(state, degraded, T0, POLICY)
    decide(state, ok(), T0 + 10, POLICY)
    assert state.degraded_since is None
    assert decide(state, degraded, T0 + 100, POLICY).action == "none"


def test_starting_within_grace_is_fine_but_hung_load_restarts() -> None:
    state = fresh()
    assert (
        decide(state, Observation("starting", "", 50, None, True, 500), T0, POLICY).action == "none"
    )
    d = decide(state, Observation("starting", "", 500, None, True, 500), T0 + 1, POLICY)
    assert d.action == "restart"
    assert "still starting" in d.reason


def test_restart_rate_limit() -> None:
    state = fresh()
    for t in (0, 1, 2):
        d = decide(state, down(), T0 + t, POLICY)
    assert d.action == "restart"
    mark_restarted(state, T0 + 2)
    assert state.restarts == 1 and state.failures == 0
    for t in (110, 111, 112):
        d = decide(state, down(), T0 + t, POLICY)
    assert d.action == "cooldown"
    assert "cooldown" in d.reason
    d = decide(state, down(), T0 + 2 + 120, POLICY)
    assert d.action == "restart"


# Stall detection ---------------------------------------------------------------


def busy(
    now: float, depth: int = 1, last: float | None = None, uptime: float = 5000
) -> Observation:
    return ok(uptime=uptime, queue_depth=depth, last_inference_at=last)


def test_first_long_request_after_idle_is_not_a_stall() -> None:
    """Idle 600 s, then one request arrives: ok for stall_after_s, then stalled."""
    state = fresh()
    idle_since = T0 - 600
    assert decide(state, busy(T0, last=idle_since), T0, POLICY).action == "none"
    assert state.busy_since == T0
    for t in (10, 20, 30):
        d = decide(state, busy(T0 + t, last=idle_since), T0 + t, POLICY)
        assert d.action == "none" and "stalled" not in d.reason, t
    d = decide(state, busy(T0 + 31, last=idle_since), T0 + 31, POLICY)
    assert "stalled (1/3)" in d.reason
    assert "queue depth 1" in d.reason


def test_very_first_request_on_long_running_server_is_not_a_stall() -> None:
    state = fresh()
    assert decide(state, busy(T0, last=None, uptime=99_999), T0, POLICY).action == "none"
    d = decide(state, busy(T0 + 29, last=None, uptime=99_999), T0 + 29, POLICY)
    assert "stalled" not in d.reason


def test_progress_resets_the_stall_timer() -> None:
    state = fresh()
    decide(state, busy(T0, depth=3, last=T0 - 100), T0, POLICY)
    # One request completes at T0 + 25: the timer restarts.
    decide(state, busy(T0 + 25, depth=2, last=T0 + 25), T0 + 25, POLICY)
    assert state.busy_since == T0 + 25
    d = decide(state, busy(T0 + 50, depth=2, last=T0 + 25), T0 + 50, POLICY)
    assert "stalled" not in d.reason
    d = decide(state, busy(T0 + 56, depth=2, last=T0 + 25), T0 + 56, POLICY)
    assert "stalled" in d.reason


def test_empty_queue_clears_busy_timer_and_stall_counts_as_failure() -> None:
    state = fresh()
    decide(state, busy(T0, last=T0 - 1), T0, POLICY)
    decide(state, ok(), T0 + 10, POLICY)
    assert state.busy_since is None
    for t in (100, 140, 180, 220):  # busy from 100; stalled at 140, 180, 220
        d = decide(state, busy(T0 + t, last=T0 - 1), T0 + t, POLICY)
    assert d.action == "restart" and "stalled 3x" in d.reason


def test_wedged_service_answering_every_other_poll_is_still_caught() -> None:
    """A wedged process may time out on /health on alternate polls; the busy
    timer must survive the timeouts or the service is never restarted."""
    state = fresh()
    stuck = T0 - 1
    actions: list[str] = []
    for i, t in enumerate(range(0, 200, 10)):
        obs = busy(T0 + t, last=stuck) if i % 2 == 0 else down("timed out after 0.5s")
        actions.append(decide(state, obs, T0 + t, POLICY).action)
    assert "restart" in actions
    assert state.busy_since == T0  # never cleared by the timeouts


def test_pid_change_clears_busy_timer() -> None:
    state = fresh()
    decide(state, busy(T0, last=T0 - 1), T0, POLICY)
    assert state.busy_since == T0
    decide(state, down(pid=777), T0 + 5, POLICY)
    assert state.busy_since is None


def test_launchd_view_fails_closed() -> None:
    def rc(code: int, out: str = "") -> wd.Runner:
        return lambda argv: CommandResult(code, out)

    assert wd.launchd_view("x", rc(113)) == (False, None)  # not found: stopped
    assert wd.launchd_view("x", rc(124)) == (None, None)  # timeout: unknown
    assert wd.launchd_view("x", rc(127)) == (None, None)  # no launchctl: unknown
    assert wd.launchd_view("x", rc(0, "system/x = {\n\tstate = running\n\tpid = 9\n}")) == (
        True,
        9,
    )


def test_unknown_launchd_state_keeps_counting_failures() -> None:
    state = fresh()
    unknown = Observation("unreachable", "refused", loaded=None, pid=None)
    decide(state, unknown, T0, POLICY)
    decide(state, unknown, T0 + 1, POLICY)
    assert decide(state, unknown, T0 + 2, POLICY).action == "restart"


# Watchdog.tick ---------------------------------------------------------------


def test_tick_calls_restarter_and_records() -> None:
    spec = ServiceSpec(name="tts", label="a.b.tts", base_url="http://127.0.0.1:1")
    observations = iter([down(), down(), down(), ok()])
    restarted: list[str] = []

    def restart(s: ServiceSpec) -> RestartResult:
        restarted.append(s.label)
        return RestartResult(True, "restarted")

    clock = iter(range(1000, 2000))
    dog = Watchdog(
        services=[spec],
        policy=POLICY,
        observe=lambda s, p: next(observations),
        restart=restart,
        clock=lambda: float(next(clock)),
    )
    dog.states["tts"] = fresh()
    assert dog.tick()["tts"].action == "none"
    assert dog.tick()["tts"].action == "none"
    assert dog.tick()["tts"].action == "restart"
    assert restarted == ["a.b.tts"]
    assert dog.states["tts"].restarts == 1
    assert dog.tick()["tts"] == Decision("none", "healthy again", True)


def test_tick_does_not_count_a_restart_that_found_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    spec = ServiceSpec(name="tts", label="a.b.tts", base_url="")
    dog = Watchdog(
        services=[spec],
        policy=POLICY,
        observe=lambda s, p: down(pid=None),
        restart=lambda s: RestartResult(False, "not loaded"),
        clock=lambda: 5000.0,
    )
    dog.states["tts"] = fresh()
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            dog.tick()
    assert dog.states["tts"].restarts == 0
    assert "not loaded" in caplog.text


def test_tick_starts_grace_at_first_sight() -> None:
    spec = ServiceSpec(name="stt", label="x", base_url="")
    dog = Watchdog(
        services=[spec],
        policy=POLICY,
        observe=lambda s, p: down(),
        restart=lambda s: pytest.fail("must not restart during grace"),
        clock=lambda: 5000.0,
    )
    for _ in range(10):
        assert dog.tick()["stt"].action == "none"


# restart_service ---------------------------------------------------------------


def test_restart_kills_pid_then_kickstarts(monkeypatch: pytest.MonkeyPatch) -> None:
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(wd.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    calls: list[list[str]] = []

    def runner(argv: Sequence[str]) -> CommandResult:
        calls.append(list(argv))
        if argv[1] == "print":
            return CommandResult(0, "system/a.stt = {\n\tstate = running\n\tpid = 777\n}")
        return CommandResult(0)

    result = restart_service(ServiceSpec(name="stt", label="a.stt", base_url=""), runner)
    assert killed == [(777, 9)]
    assert result == RestartResult(True, "sent SIGKILL to pid 777; kickstart ok")
    assert calls == [
        ["launchctl", "print", "system/a.stt"],
        ["launchctl", "kickstart", "system/a.stt"],
    ]


def test_restart_without_pid_just_kickstarts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wd.os, "kill", lambda pid, sig: pytest.fail("must not kill"))

    def runner(argv: Sequence[str]) -> CommandResult:
        if argv[1] == "print":
            return CommandResult(0, "system/a.stt = {\n\tstate = waiting\n}")
        return CommandResult(0)

    result = restart_service(ServiceSpec(name="stt", label="a.stt", base_url=""), runner)
    assert result == RestartResult(True, "no running pid; kickstart ok")


@pytest.mark.parametrize("pid", [0, 1])
def test_restart_never_kills_pid_0_or_1(monkeypatch: pytest.MonkeyPatch, pid: int) -> None:
    monkeypatch.setattr(wd.os, "kill", lambda p, sig: pytest.fail("must not kill"))

    def runner(argv: Sequence[str]) -> CommandResult:
        if argv[1] == "print":
            return CommandResult(0, f"system/a.stt = {{\n\tstate = running\n\tpid = {pid}\n}}")
        return CommandResult(0)

    result = restart_service(ServiceSpec(name="stt", label="a.stt", base_url=""), runner)
    assert f"refusing to kill pid {pid}" in result.detail


def test_restart_of_unloaded_service_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wd.os, "kill", lambda pid, sig: pytest.fail("must not kill"))
    runner = lambda argv: CommandResult(113, "", "not found")  # noqa: E731
    result = restart_service(ServiceSpec(name="stt", label="a.stt", base_url=""), runner)
    assert result.done is False and "not loaded" in result.detail


# poll_health against a real local HTTP server ------------------------------------


class _Handler(BaseHTTPRequestHandler):
    responses: dict[str, tuple[int, dict[str, Any] | str | float]] = {}

    def do_GET(self) -> None:
        code, body = self.responses.get(self.path, (404, "nope"))
        if isinstance(body, float):
            time.sleep(body)
            body = {"status": "ok"}
        payload = json.dumps(body).encode() if isinstance(body, dict) else str(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def server() -> Iterator[str]:
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _health(status: str, **extra: Any) -> dict[str, Any]:
    return {
        "status": status,
        "service": "tts",
        "engine": "fake",
        "model": "m",
        "model_loaded": status != "starting",
        "uptime_s": 12.5,
        "queue_depth": 0,
        "queue_capacity": 8,
        "last_inference_at": None,
        "last_inference_ms": None,
        "last_error": None,
        "version": "0",
        **extra,
    }


def test_poll_health_ok(server: str) -> None:
    _Handler.responses = {"/health": (200, _health("ok"))}
    obs = poll_health(f"{server}/health", timeout_s=1)
    assert obs.kind == "ok"
    assert obs.uptime_s == 12.5
    assert obs.payload is not None and obs.payload["engine"] == "fake"


def test_poll_health_starting_is_503(server: str) -> None:
    _Handler.responses = {"/health": (503, _health("starting"))}
    obs = poll_health(f"{server}/health", timeout_s=1)
    assert obs.kind == "starting"
    assert obs.uptime_s == 12.5


def test_poll_health_degraded(server: str) -> None:
    _Handler.responses = {"/health": (200, _health("degraded", last_error="RuntimeError: x"))}
    obs = poll_health(f"{server}/health", timeout_s=1)
    assert obs.kind == "degraded"
    assert obs.detail == "RuntimeError: x"


def test_poll_health_missing_model(server: str) -> None:
    body = _health("missing_model", model_loaded=False, last_error="ModelNotDownloaded: m")
    _Handler.responses = {"/health": (503, body)}
    obs = poll_health(f"{server}/health", timeout_s=1)
    assert obs.kind == "missing_model"
    assert obs.detail == "ModelNotDownloaded: m"


def test_poll_health_timeout(server: str) -> None:
    _Handler.responses = {"/health": (200, 2.0)}
    obs = poll_health(f"{server}/health", timeout_s=0.2)
    assert obs.kind == "unreachable"
    assert obs.detail == "timed out after 0.2s"


def test_poll_health_connection_refused() -> None:
    obs = poll_health("http://127.0.0.1:9/health", timeout_s=0.5)
    assert obs.kind == "unreachable"
    assert "timed out" not in obs.detail


def test_poll_health_garbage(server: str) -> None:
    _Handler.responses = {"/health": (500, "<html>oops</html>")}
    obs = poll_health(f"{server}/health", timeout_s=1)
    assert obs.kind == "error"


def test_observe_adds_launchd_pid_and_skips_probe_when_busy(server: str) -> None:
    _Handler.responses = {"/health": (200, _health("ok"))}
    spec = ServiceSpec(name="tts", label="x", base_url=server, probe=wd.default_probe("tts", "m"))
    probing = POLICY.model_copy(update={"probe": True})
    launchctl = lambda argv: CommandResult(0, "system/x = {\n\tstate = running\n\tpid = 9\n}")  # noqa: E731
    # The handler has no do_POST, so the probe gets a 501 immediately: not a stall.
    assert wd.run_probe(spec, timeout_s=1) is None
    obs = wd.observe(spec, probing, launchctl)
    assert obs.kind == "ok" and obs.loaded is True and obs.pid == 9
    # With requests in flight the probe is not sent at all.
    _Handler.responses = {"/health": (200, _health("ok", queue_depth=3))}
    original = wd.run_probe
    wd.run_probe = lambda s, timeout_s: pytest.fail("probe must be skipped")  # type: ignore[assignment]
    try:
        assert wd.observe(spec, probing, launchctl).kind == "ok"
    finally:
        wd.run_probe = original


def test_observe_reads_pid_before_health(monkeypatch: pytest.MonkeyPatch) -> None:
    """An ok from a process launchd has since replaced must not be credited to
    the new pid, so the pid is read first."""
    calls: list[str] = []

    def view(label: str, runner: object) -> tuple[bool, int]:
        calls.append("launchd")
        return True, 9

    def poll(url: str, **kw: object) -> Observation:
        calls.append("health")
        return ok(pid=None)

    monkeypatch.setattr(wd, "launchd_view", view)
    monkeypatch.setattr(wd, "poll_health", poll)
    spec = ServiceSpec(name="tts", label="x", base_url="http://unused")
    assert wd.observe(spec, POLICY).pid == 9
    assert calls == ["launchd", "health"]


def test_default_probe_bodies() -> None:
    stt = wd.default_probe("stt", "m/stt")
    assert stt.path == "/v1/audio/transcriptions"
    assert stt.content_type.startswith("multipart/form-data; boundary=")
    assert b'name="file"; filename="probe.wav"' in stt.body and b"RIFF" in stt.body
    tts = wd.default_probe("tts", "m/tts", "bf_emma")
    assert tts.path == "/v1/audio/speech" and tts.content_type == "application/json"
    assert json.loads(tts.body) == {
        "model": "m/tts",
        "input": "ok",
        "voice": "bf_emma",
        "response_format": "pcm",
    }


def test_logging_file_only_when_configured(tmp_path: Path) -> None:
    wd.configure_watchdog_logging("info", tmp_path / "watchdog.log")
    handlers = logging.getLogger().handlers
    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.handlers.RotatingFileHandler)
    wd.configure_watchdog_logging("info", None)
    assert isinstance(logging.getLogger().handlers[0], logging.StreamHandler)


# Idle unload and a service switched off (ADR 0033) ---------------------------


def answer(kind: str, depth: int = 0, pid: int = 500, **payload: Any) -> Observation:
    body = {"status": kind, "queue_depth": depth, "model_loaded": kind == "ok", **payload}
    return Observation(kind, "", 10, body, loaded=True, pid=pid)  # type: ignore[arg-type]


def parked(pid: int = 500) -> Observation:
    return Observation("off", "off", loaded=True, pid=pid)


@pytest.mark.parametrize("kind", ["idle", "loading"])
def test_idle_and_loading_are_healthy(kind: str) -> None:
    state = fresh()
    decide(state, down(), T0, POLICY)
    decide(state, down(), T0 + 1, POLICY)
    d = decide(state, answer(kind), T0 + 2, POLICY)
    assert d == Decision("none", "healthy again", True)
    assert state.failures == 0
    assert state.answered, "an idle answer ends the startup grace"


def test_idle_clears_the_degraded_timer() -> None:
    state = fresh()
    decide(state, Observation("degraded", "boom", 10, {}, loaded=True, pid=500), T0, POLICY)
    assert state.degraded_since == T0
    decide(state, answer("idle"), T0 + 1, POLICY)
    assert state.degraded_since is None


def test_a_load_that_hangs_holds_the_queue_and_stalls() -> None:
    """A request waits on a load that never finishes: the stall rule
    restarts it, as it would a wedged inference."""
    state = fresh()
    assert decide(state, answer("loading", depth=1), T0, POLICY).action == "none"
    late = T0 + POLICY.stall_after_s + 1
    actions = [decide(state, answer("loading", depth=1), late + i, POLICY) for i in range(3)]
    assert [d.action for d in actions] == ["none", "none", "restart"]
    assert "stalled" in actions[-1].reason


def test_off_is_never_restarted_or_counted_and_reported_once_per_process() -> None:
    state = ServiceState(grace_started_at=T0, last_pid=None)
    first = decide(state, parked(), T0, POLICY)
    assert first.action == "none" and not first.quiet
    for i in range(1, 30):
        d = decide(state, parked(), T0 + i * POLICY.startup_grace_s, POLICY)
        assert d.action == "none" and d.quiet
    assert state.failures == 0
    assert not state.answered, "a parked process never ends a startup grace"
    # Restarted into serving (switched on): a new pid, its grace starts now.
    later = T0 + 10_000
    assert decide(state, down(pid=501), later, POLICY).action == "none"
    assert state.grace_started_at == later
    # Switched off again: reported again.
    assert not decide(state, parked(pid=502), later + 1, POLICY).quiet


def test_observe_takes_the_parked_pid_for_off_without_polling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from voiced.layout import off_marker

    marker = off_marker(tmp_path, "stt")
    marker.write_text("9\n")
    monkeypatch.setattr(wd, "launchd_view", lambda label, runner: (True, 9))
    monkeypatch.setattr(wd, "poll_health", lambda *a, **k: pytest.fail("not polled"))
    spec = ServiceSpec(name="stt", label="x", base_url="http://unused", off_marker=marker)
    obs = wd.observe(spec, POLICY)
    assert obs.kind == "off" and obs.pid == 9
    assert "voiced settings set stt.enabled=true" in obs.detail


def test_a_stale_marker_is_not_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The marker of a parked process that was killed names a pid launchd no
    longer runs: the service is polled as usual."""
    from voiced.layout import off_marker

    marker = off_marker(tmp_path, "stt")
    marker.write_text("8\n")
    monkeypatch.setattr(wd, "launchd_view", lambda label, runner: (True, 9))
    monkeypatch.setattr(wd, "poll_health", lambda *a, **k: answer("ok", pid=9))
    spec = ServiceSpec(name="stt", label="x", base_url="http://unused", off_marker=marker)
    assert wd.observe(spec, POLICY).kind == "ok"


@pytest.mark.parametrize("kind", ["idle", "loading"])
def test_poll_health_idle_and_loading(server: str, kind: str) -> None:
    _Handler.responses = {"/health": (200, _health(kind))}
    assert poll_health(f"{server}/health", timeout_s=1).kind == kind


def test_no_probe_while_idle(server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe would load the model the service just unloaded."""
    _Handler.responses = {"/health": (200, _health("idle"))}
    spec = ServiceSpec(name="tts", label="x", base_url=server, probe=wd.default_probe("tts", "m"))
    monkeypatch.setattr(wd, "run_probe", lambda s, timeout_s: pytest.fail("no probe"))
    launchctl = lambda argv: CommandResult(0, "system/x = {\n\tstate = running\n\tpid = 9\n}")  # noqa: E731
    probing = POLICY.model_copy(update={"probe": True})
    assert wd.observe(spec, probing, launchctl).kind == "idle"
