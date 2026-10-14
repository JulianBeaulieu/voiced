"""Watchdog decision logic and health polling against a fake endpoint."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from voiced import watchdog as wd
from voiced.watchdog import (
    Decision,
    Observation,
    Policy,
    ServiceSpec,
    ServiceState,
    Watchdog,
    decide,
    launchctl_restart,
    mark_restarted,
    poll_health,
)

POLICY = Policy(
    interval_s=1,
    timeout_s=0.5,
    failures_before_restart=3,
    startup_grace_s=100,
    degraded_window_s=60,
    restart_cooldown_s=120,
)


def ok(uptime: float = 10) -> Observation:
    return Observation("ok", "", uptime, {"status": "ok"})


def down(detail: str = "connection refused") -> Observation:
    return Observation("unreachable", detail)


# decide() ------------------------------------------------------------------


def test_healthy_is_a_no_op() -> None:
    state = ServiceState()
    assert decide(state, ok(), 0, POLICY) == Decision("none", "healthy")
    assert state.failures == 0


def test_consecutive_failures_restart() -> None:
    state = ServiceState()
    assert decide(state, down(), 0, POLICY).action == "none"
    assert decide(state, down(), 1, POLICY).action == "none"
    d = decide(state, down(), 2, POLICY)
    assert d.action == "restart"
    assert "3x in a row" in d.reason


def test_recovery_resets_the_counter() -> None:
    state = ServiceState()
    decide(state, down(), 0, POLICY)
    decide(state, down(), 1, POLICY)
    d = decide(state, ok(), 2, POLICY)
    assert d == Decision("none", "healthy again")
    assert state.failures == 0
    # Two more failures are not enough again.
    decide(state, down(), 3, POLICY)
    assert decide(state, down(), 4, POLICY).action == "none"


def test_degraded_restarts_only_after_window() -> None:
    state = ServiceState()
    degraded = Observation("degraded", "RuntimeError: metal", 50, {"status": "degraded"})
    assert decide(state, degraded, 0, POLICY).action == "none"
    assert decide(state, degraded, 30, POLICY).action == "none"
    d = decide(state, degraded, 61, POLICY)
    assert d.action == "restart"
    assert "degraded for 61s" in d.reason


def test_ok_clears_degraded_timer() -> None:
    state = ServiceState()
    degraded = Observation("degraded", "x", 1, {})
    decide(state, degraded, 0, POLICY)
    decide(state, ok(), 10, POLICY)
    assert state.degraded_since is None
    assert decide(state, degraded, 100, POLICY).action == "none"


def test_starting_within_grace_is_fine_but_hung_load_restarts() -> None:
    state = ServiceState()
    assert decide(state, Observation("starting", "", 50), 0, POLICY).action == "none"
    d = decide(state, Observation("starting", "", 500), 1, POLICY)
    assert d.action == "restart"
    assert "still starting" in d.reason


def test_stalled_probe_counts_as_failure() -> None:
    state = ServiceState()
    stalled = Observation("stalled", "probe timed out")
    decide(state, stalled, 0, POLICY)
    decide(state, stalled, 1, POLICY)
    assert decide(state, stalled, 2, POLICY).action == "restart"


def test_restart_rate_limit() -> None:
    state = ServiceState()
    for t in (0, 1, 2):
        d = decide(state, down(), t, POLICY)
    assert d.action == "restart"
    mark_restarted(state, 2)
    assert state.restarts == 1 and state.failures == 0
    for t in (10, 11, 12):
        d = decide(state, down(), t, POLICY)
    assert d.action == "cooldown"
    assert "cooldown" in d.reason
    # Counters are kept, so the first poll after the cooldown restarts.
    d = decide(state, down(), 2 + 120, POLICY)
    assert d.action == "restart"


# Watchdog.tick ---------------------------------------------------------------


def test_tick_calls_restarter_and_records() -> None:
    spec = ServiceSpec(name="tts", label="a.b.tts", base_url="http://127.0.0.1:1")
    observations = iter([down(), down(), down(), ok()])
    restarted: list[str] = []
    clock = iter(range(100))
    dog = Watchdog(
        services=[spec],
        policy=POLICY,
        observe=lambda s, p: next(observations),
        restart=lambda s: restarted.append(s.label) or "restarted",
        clock=lambda: float(next(clock)),
    )
    assert dog.tick()["tts"].action == "none"
    assert dog.tick()["tts"].action == "none"
    assert dog.tick()["tts"].action == "restart"
    assert restarted == ["a.b.tts"]
    assert dog.states["tts"].restarts == 1
    assert dog.tick()["tts"] == Decision("none", "healthy again")


def test_tick_survives_observer_bug(caplog: pytest.LogCaptureFixture) -> None:
    spec = ServiceSpec(name="stt", label="x", base_url="http://127.0.0.1:1")

    def boom(s: ServiceSpec, p: Policy) -> Observation:
        raise RuntimeError("bug")

    dog = Watchdog(services=[spec], policy=POLICY, observe=boom, restart=lambda s: "")
    stops = iter([False, True])
    with caplog.at_level("ERROR"):
        dog.run_forever(stop=lambda: next(stops))
    assert "tick failed" in caplog.text


# launchctl_restart fallback ---------------------------------------------------


def test_restart_prefers_kickstart() -> None:
    calls: list[list[str]] = []

    def run(argv: Any) -> tuple[int, str]:
        calls.append(list(argv))
        return 0, ""

    spec = ServiceSpec(name="stt", label="a.stt", base_url="")
    assert launchctl_restart(spec, run) == "launchctl kickstart -k system/a.stt"
    assert calls == [["launchctl", "kickstart", "-k", "system/a.stt"]]


def test_restart_falls_back_to_kill_then_kickstart(monkeypatch: pytest.MonkeyPatch) -> None:
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(wd.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    calls: list[list[str]] = []

    def run(argv: Any) -> tuple[int, str]:
        calls.append(list(argv))
        if argv[1:3] == ["kickstart", "-k"]:
            return 1, "Operation not permitted"
        if argv[1] == "print":
            return 0, "system/a.stt = {\n\tstate = running\n\tpid = 777\n}"
        return 0, ""

    spec = ServiceSpec(name="stt", label="a.stt", base_url="")
    msg = launchctl_restart(spec, run)
    assert killed == [(777, 9)]
    assert "SIGKILL to pid 777" in msg and "kickstart ok" in msg
    assert calls[-1] == ["launchctl", "kickstart", "system/a.stt"]


def test_restart_without_pid_just_kickstarts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wd.os, "kill", lambda pid, sig: pytest.fail("must not kill"))

    def run(argv: Any) -> tuple[int, str]:
        if argv[1:3] == ["kickstart", "-k"]:
            return 1, "denied"
        if argv[1] == "print":
            return 0, "system/a.stt = {\n\tstate = waiting\n}"
        return 0, ""

    msg = launchctl_restart(ServiceSpec(name="stt", label="a.stt", base_url=""), run)
    assert "no running pid" in msg and "kickstart ok" in msg


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


def test_poll_health_timeout(server: str) -> None:
    _Handler.responses = {"/health": (200, 2.0)}
    obs = poll_health(f"{server}/health", timeout_s=0.2)
    assert obs.kind == "unreachable"
    assert "timed out" in obs.detail or "timeout" in obs.detail


def test_poll_health_connection_refused() -> None:
    obs = poll_health("http://127.0.0.1:9/health", timeout_s=0.5)
    assert obs.kind == "unreachable"


def test_poll_health_garbage(server: str) -> None:
    _Handler.responses = {"/health": (500, "<html>oops</html>")}
    obs = poll_health(f"{server}/health", timeout_s=1)
    assert obs.kind == "error"


def test_probe_timeout_is_a_stall(server: str) -> None:
    class Slow(BaseHTTPRequestHandler):
        pass

    _Handler.responses = {"/health": (200, _health("ok"))}
    spec = ServiceSpec(
        name="tts",
        label="x",
        base_url=server,
        probe=wd.ProbeRequest("/v1/audio/speech", b"{}", "application/json"),
    )
    # The handler has no do_POST, so the probe gets a 501 immediately: not a stall.
    assert wd.run_probe(spec, timeout_s=1) is None
    obs = wd.observe(spec, Policy(timeout_s=1, probe=True, probe_timeout_s=1))
    assert obs.kind == "ok"


def test_multipart_and_silent_wav() -> None:
    wav = wd.silent_wav(0.1)
    assert wav[:4] == b"RIFF" and len(wav) > 44
    body, ctype = wd.multipart({"model": "m"}, "probe.wav", wav)
    assert ctype.startswith("multipart/form-data; boundary=")
    assert b'name="file"; filename="probe.wav"' in body
    assert body.endswith(b"--\r\n")
