"""Watchdog decision logic and health polling against a fake endpoint."""

from __future__ import annotations

import json
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
    Policy,
    ServiceSpec,
    ServiceState,
    Watchdog,
    decide,
    detect_stall,
    mark_restarted,
    poll_health,
    restart_service,
)

POLICY = Policy(
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


def ok(uptime: float = 10, **payload: Any) -> Observation:
    return Observation("ok", "", uptime, {"status": "ok", "queue_depth": 0, **payload})


def down(detail: str = "connection refused") -> Observation:
    return Observation("unreachable", detail)


def fresh() -> ServiceState:
    return ServiceState(grace_started_at=0.0)


def test_policy_mirrors_watchdog_config() -> None:
    policy = Policy(**WatchdogConfig().model_dump())
    assert policy.failures_before_restart == 3
    assert set(Policy.__dataclass_fields__) == set(WatchdogConfig.model_fields)


# decide() ------------------------------------------------------------------


def test_healthy_is_a_no_op() -> None:
    state = fresh()
    assert decide(state, ok(), T0, POLICY) == Decision("none", "healthy")
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
    state = ServiceState(grace_started_at=T0)
    for t in range(0, 99, 7):
        d = decide(state, down(), T0 + t, POLICY)
        assert d.action == "none", t
        assert "startup grace" in d.reason
    assert state.failures == 0
    # Grace over: now they count.
    for t in (101, 102):
        assert decide(state, down(), T0 + t, POLICY).action == "none"
    assert decide(state, down(), T0 + 103, POLICY).action == "restart"


def test_grace_restarts_after_a_restart() -> None:
    state = fresh()
    for t in (0, 1, 2):
        d = decide(state, down(), T0 + t, POLICY)
    assert d.action == "restart"
    mark_restarted(state, T0 + 2)
    # Refused polls right after the restart are the new instance loading.
    for t in (10, 20, 30):
        d = decide(state, down(), T0 + t, POLICY)
        assert d.action == "none" and "startup grace" in d.reason
    assert state.failures == 0


def test_recovery_resets_the_counter() -> None:
    state = fresh()
    decide(state, down(), T0, POLICY)
    decide(state, down(), T0 + 1, POLICY)
    assert decide(state, ok(), T0 + 2, POLICY) == Decision("none", "healthy again")
    assert state.failures == 0
    decide(state, down(), T0 + 3, POLICY)
    assert decide(state, down(), T0 + 4, POLICY).action == "none"


def test_degraded_restarts_only_after_window() -> None:
    state = fresh()
    degraded = Observation("degraded", "RuntimeError: metal", 50, {"status": "degraded"})
    assert decide(state, degraded, T0, POLICY).action == "none"
    assert decide(state, degraded, T0 + 30, POLICY).action == "none"
    d = decide(state, degraded, T0 + 61, POLICY)
    assert d.action == "restart"
    assert "degraded for 61s" in d.reason


def test_ok_clears_degraded_timer() -> None:
    state = fresh()
    degraded = Observation("degraded", "x", 1, {})
    decide(state, degraded, T0, POLICY)
    decide(state, ok(), T0 + 10, POLICY)
    assert state.degraded_since is None
    assert decide(state, degraded, T0 + 100, POLICY).action == "none"


def test_starting_within_grace_is_fine_but_hung_load_restarts() -> None:
    state = fresh()
    assert decide(state, Observation("starting", "", 50), T0, POLICY).action == "none"
    d = decide(state, Observation("starting", "", 500), T0 + 1, POLICY)
    assert d.action == "restart"
    assert "still starting" in d.reason


def test_stalled_counts_as_failure() -> None:
    state = fresh()
    stalled = Observation("stalled", "queue stuck")
    decide(state, stalled, T0, POLICY)
    decide(state, stalled, T0 + 1, POLICY)
    assert decide(state, stalled, T0 + 2, POLICY).action == "restart"


def test_restart_rate_limit() -> None:
    state = fresh()
    for t in (0, 1, 2):
        d = decide(state, down(), T0 + t, POLICY)
    assert d.action == "restart"
    mark_restarted(state, T0 + 2)
    assert state.restarts == 1 and state.failures == 0
    # After the new grace period, failures again: cooldown holds them back.
    for t in (110, 111, 112):
        d = decide(state, down(), T0 + t, POLICY)
    assert d.action == "cooldown"
    assert "cooldown" in d.reason
    d = decide(state, down(), T0 + 2 + 120, POLICY)
    assert d.action == "restart"


# detect_stall() ----------------------------------------------------------------


def test_stall_from_queue_depth_and_last_inference() -> None:
    now = 10_000.0
    busy = ok(queue_depth=2, last_inference_at=now - 5)
    assert detect_stall(busy, POLICY, now).kind == "ok"
    stuck = ok(queue_depth=2, last_inference_at=now - 31)
    obs = detect_stall(stuck, POLICY, now)
    assert obs.kind == "stalled" and "queue depth 2" in obs.detail
    idle = ok(queue_depth=0, last_inference_at=now - 999)
    assert detect_stall(idle, POLICY, now).kind == "ok"


def test_stall_without_any_inference_uses_uptime() -> None:
    now = 10_000.0
    first_request = ok(uptime=10, queue_depth=1, last_inference_at=None)
    assert detect_stall(first_request, POLICY, now).kind == "ok"
    stuck_first = ok(uptime=100, queue_depth=1, last_inference_at=None)
    assert detect_stall(stuck_first, POLICY, now).kind == "stalled"


# Watchdog.tick ---------------------------------------------------------------


def test_tick_calls_restarter_and_records() -> None:
    spec = ServiceSpec(name="tts", label="a.b.tts", base_url="http://127.0.0.1:1")
    observations = iter([down(), down(), down(), ok()])
    restarted: list[str] = []
    clock = iter(range(1000, 2000))
    dog = Watchdog(
        services=[spec],
        policy=POLICY,
        observe=lambda s, p: next(observations),
        restart=lambda s: restarted.append(s.label) or "restarted",
        clock=lambda: float(next(clock)),
    )
    dog.states["tts"] = fresh()
    assert dog.tick()["tts"].action == "none"
    assert dog.tick()["tts"].action == "none"
    assert dog.tick()["tts"].action == "restart"
    assert restarted == ["a.b.tts"]
    assert dog.states["tts"].restarts == 1
    assert dog.tick()["tts"] == Decision("none", "healthy again")


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


def test_tick_survives_observer_bug(caplog: pytest.LogCaptureFixture) -> None:
    spec = ServiceSpec(name="stt", label="x", base_url="http://127.0.0.1:1")

    def boom(s: ServiceSpec, p: Policy) -> Observation:
        raise RuntimeError("bug")

    dog = Watchdog(services=[spec], policy=POLICY, observe=boom, restart=lambda s: "")
    stops = iter([False, True])
    with caplog.at_level("ERROR"):
        dog.run_forever(stop=lambda: next(stops))
    assert "tick failed" in caplog.text


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

    msg = restart_service(ServiceSpec(name="stt", label="a.stt", base_url=""), runner)
    assert killed == [(777, 9)]
    assert msg == "sent SIGKILL to pid 777; kickstart ok"
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

    msg = restart_service(ServiceSpec(name="stt", label="a.stt", base_url=""), runner)
    assert msg == "no running pid; kickstart ok"


def test_restart_of_unloaded_service_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wd.os, "kill", lambda pid, sig: pytest.fail("must not kill"))
    runner = lambda argv: CommandResult(113, "", "not found")  # noqa: E731
    msg = restart_service(ServiceSpec(name="stt", label="a.stt", base_url=""), runner)
    assert "not loaded" in msg


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
    assert obs.detail == "timed out after 0.2s"


def test_poll_health_connection_refused() -> None:
    obs = poll_health("http://127.0.0.1:9/health", timeout_s=0.5)
    assert obs.kind == "unreachable"
    assert "timed out" not in obs.detail


def test_poll_health_garbage(server: str) -> None:
    _Handler.responses = {"/health": (500, "<html>oops</html>")}
    obs = poll_health(f"{server}/health", timeout_s=1)
    assert obs.kind == "error"


def test_probe_answer_is_not_a_stall_and_busy_queue_skips_probe(server: str) -> None:
    _Handler.responses = {"/health": (200, _health("ok"))}
    spec = ServiceSpec(
        name="tts",
        label="x",
        base_url=server,
        probe=wd.ProbeRequest("/v1/audio/speech", b"{}", "application/json"),
    )
    probing = Policy(**{**POLICY.__dict__, "probe": True})
    # The handler has no do_POST, so the probe gets a 501 immediately: not a stall.
    assert wd.run_probe(spec, timeout_s=1) is None
    assert wd.observe(spec, probing, now=time.time()).kind == "ok"
    # With requests in flight the probe is not sent at all.
    _Handler.responses = {"/health": (200, _health("ok", queue_depth=3))}
    calls: list[str] = []
    original = wd.run_probe
    wd.run_probe = lambda s, timeout_s: calls.append(s.name)  # type: ignore[assignment]
    try:
        obs = wd.observe(spec, probing, now=time.time())
    finally:
        wd.run_probe = original
    assert obs.kind == "ok" and calls == []


def test_multipart_and_silent_wav() -> None:
    wav = wd.silent_wav(0.1)
    assert wav[:4] == b"RIFF" and len(wav) > 44
    body, ctype = wd.multipart({"model": "m"}, "probe.wav", wav)
    assert ctype.startswith("multipart/form-data; boundary=")
    assert b'name="file"; filename="probe.wav"' in body
    assert body.endswith(b"--\r\n")


def test_logging_file_only_when_configured(tmp_path: Path) -> None:
    import logging

    wd.configure_watchdog_logging("info", tmp_path / "watchdog.log")
    handlers = logging.getLogger().handlers
    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.handlers.RotatingFileHandler)
    wd.configure_watchdog_logging("info", None)
    assert isinstance(logging.getLogger().handlers[0], logging.StreamHandler)
