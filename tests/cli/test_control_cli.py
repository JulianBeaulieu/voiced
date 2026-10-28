"""``voiced restart``, ``voiced settings`` and ``voiced status`` with and
without the control socket, and the doctor check for it."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import signal
import socket
import tempfile
import threading
import tomllib
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from voiced import cli_lifecycle as lc
from voiced import doctor
from voiced.cli import build_parser, main
from voiced.config import Settings, load_settings
from voiced.control import NOT_SERVED, socket_path
from voiced.control.client import ControlError
from voiced.control.ops import Operations
from voiced.control.server import ControlServer
from voiced.engines.catalogue import engines_list
from voiced.launchd.control import CommandResult

from ..control.conftest import FakeSupervisor


@pytest.fixture
def cfg(settings: Settings) -> Path:
    path = Path(os.environ["VOICED_CONFIG"])
    path.write_text('[stt]\nengine = "fake"\n[tts]\nengine = "fake"\n')
    return path


def settings_args(*argv: str) -> argparse.Namespace:
    return build_parser().parse_args(["settings", *argv])


# Without a socket ----------------------------------------------------------------


def test_settings_get_without_socket(settings: Settings, cfg: Path) -> None:
    out: list[str] = []
    assert lc.cmd_settings(settings_args(), load_settings(), out.append) == 0
    text = "\n".join(out)
    assert "tts.engine" in text and "fake" in text
    assert "api_key_set" in text
    out.clear()
    assert lc.cmd_settings(settings_args("get", "--json"), load_settings(), out.append) == 0
    assert json.loads(out[0])["paths"]["config"] == str(cfg)


def test_settings_set_without_socket(settings: Settings, cfg: Path) -> None:
    out: list[str] = []
    args = settings_args("set", "tts.voice=bf_emma", "zeroconf=off", "tts.speed=1.2")
    assert lc.cmd_settings(args, settings, out.append) == 0
    data = tomllib.loads(cfg.read_text())
    assert data["tts"]["default_voice"] == "bf_emma"
    assert data["tts"]["speed"] == 1.2
    assert data["server"]["zeroconf"] is False
    assert "Restart to apply: voiced restart all" in out
    # Only the changed keys are shown.
    assert "tts.voice" in out[0] and "zeroconf" in out[0] and "tts.speed" in out[0]
    assert "tts.engine" not in out[0] and "ports" not in out[0]
    out.clear()
    assert lc.cmd_settings(settings_args("set", "tts.voice=bf_emma"), settings, out.append) == 0
    assert out == ["Nothing changed."]


@pytest.mark.usefixtures("models_downloaded")
def test_settings_set_engine_takes_its_model(settings: Settings, cfg: Path) -> None:
    out: list[str] = []
    assert lc.cmd_settings(settings_args("set", "tts.engine=soprano"), settings, out.append) == 0
    tts = tomllib.loads(cfg.read_text())["tts"]
    assert (tts["engine"], tts["default_voice"]) == ("soprano", "default")
    assert "tts.model" in out[0]


def test_settings_set_api_key_from_stdin(settings: Settings, cfg: Path) -> None:
    out: list[str] = []
    args = settings_args("set", "api_key=-", "--json")
    assert lc.cmd_settings(args, settings, out.append, stdin=lambda: "k3y\n") == 0
    assert "k3y" not in out[0]
    assert json.loads(out[0])["api_key_set"] is True
    assert load_settings().server.api_key == "k3y"


def test_settings_set_rejects(settings: Settings, cfg: Path) -> None:
    before = cfg.read_text()
    for argv, code, message in [
        (["tts.speed=fast"], 2, 'tts.speed: must be a number, got "fast"'),
        (["tts.speed=5"], 2, "tts.speed: must be at most 4"),
        (["zeroconf=maybe"], 2, 'zeroconf: must be true or false, got "maybe"'),
        (["nope=1"], 2, "not settable"),
        (["bind"], 2, "expected KEY=VALUE"),
        (["tts.voice=nobody"], 1, "tts.voice: fake has no voice"),
    ]:
        out: list[str] = []
        assert lc.cmd_settings(settings_args("set", *argv), settings, out.append) == code
        assert message in out[0]
    assert cfg.read_text() == before


def test_restart_without_socket_uses_launchctl(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    killed: list[int] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append(pid))
    calls: list[list[str]] = []

    def runner(argv: Sequence[str]) -> CommandResult:
        calls.append(list(argv))
        if argv[1] == "print":
            return CommandResult(0, "state = running\n\tpid = 4242\n")
        return CommandResult(0)

    out: list[str] = []
    assert lc.cmd_restart("tts", settings, runner, out.append) == 0
    assert killed == [4242]
    assert ["launchctl", "kickstart", f"gui/{os.getuid()}/{settings.launchd.label('tts')}"] in calls
    assert out[0].startswith("no control socket at")
    assert out[1].startswith("tts: sent SIGKILL to pid 4242")


def test_restart_without_socket_not_loaded(settings: Settings) -> None:
    out: list[str] = []
    runner = lambda argv: CommandResult(113, "", "Could not find service")  # noqa: E731
    assert lc.cmd_restart("all", settings, runner, out.append) == 1
    assert len(out) == 4 and all("not loaded" in line for line in out[1:])
    assert out[3].startswith("watchdog:")


class FakeWatchdogJob:
    """launchctl and os.kill for a watchdog job that KeepAlive restarts with
    a new pid once it is sent SIGTERM; every call goes into ``events``."""

    def __init__(self, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
        self.label = settings.launchd.label("watchdog")
        self.pid = 100
        self.exits = True
        self.kickstart = CommandResult(0)
        self.events: list[tuple[object, ...]] = []
        monkeypatch.setattr(os, "kill", self.kill)
        monkeypatch.setattr(lc, "WATCHDOG_POLL_S", 0.0)

    def kill(self, pid: int, sig: int) -> None:
        self.events.append(("kill", pid, sig))
        if pid == self.pid and self.exits:
            self.pid += 100

    def runner(self, argv: Sequence[str]) -> CommandResult:
        if argv[-1] != f"gui/{os.getuid()}/{self.label}":
            raise AssertionError(f"unexpected launchctl call {argv}")
        if argv[1] == "print":
            return CommandResult(0, f"state = running\n\tpid = {self.pid}\n")
        self.events.append(("kickstart",))
        return self.kickstart


@pytest.fixture
def job(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> FakeWatchdogJob:
    return FakeWatchdogJob(settings, monkeypatch)


@pytest.fixture
def accepting(job: FakeWatchdogJob, monkeypatch: pytest.MonkeyPatch) -> None:
    """A control socket that accepts restarts and greets."""

    def request(path: Path, op: str, args: dict[str, str], timeout_s: float) -> dict[str, object]:
        job.events.append(("request", op, args["service"]))
        names = ["stt", "tts"] if args["service"] == "all" else [args["service"]]
        return {"accepted": True, "detail": {n: "restarting" for n in names}}

    def hello(settings: Settings) -> dict[str, object]:
        job.events.append(("hello",))
        return {"version": "9.9"}

    monkeypatch.setattr(lc, "request", request)
    monkeypatch.setattr(lc, "control_hello", hello)


@pytest.mark.usefixtures("accepting")
def test_restart_all_restarts_the_watchdog_last(settings: Settings, job: FakeWatchdogJob) -> None:
    out: list[str] = []
    assert lc.cmd_restart("all", settings, job.runner, out.append) == 0
    assert job.events == [
        ("request", "service.restart", "all"),
        ("kill", 100, signal.SIGTERM),
        ("kickstart",),
        ("hello",),
    ]
    assert out[:2] == ["stt: restarting", "tts: restarting"]
    assert out[-1] == "watchdog: restarted (pid 100 -> pid 200), voiced 9.9 answering on the socket"


@pytest.mark.usefixtures("accepting")
def test_restart_one_service_leaves_the_watchdog(settings: Settings, job: FakeWatchdogJob) -> None:
    out: list[str] = []
    assert lc.cmd_restart("stt", settings, job.runner, out.append) == 0
    assert job.events == [("request", "service.restart", "stt")]
    assert out == ["stt: restarting", "`voiced status` shows when the model is loaded again."]


@pytest.mark.usefixtures("accepting")
def test_restart_watchdog_alone(settings: Settings, job: FakeWatchdogJob) -> None:
    out: list[str] = []
    assert lc.cmd_restart("watchdog", settings, job.runner, out.append) == 0
    assert [e[0] for e in job.events] == ["kill", "kickstart", "hello"]
    assert out == ["watchdog: restarted (pid 100 -> pid 200), voiced 9.9 answering on the socket"]


@pytest.mark.usefixtures("accepting")
def test_restart_watchdog_failures(
    settings: Settings, job: FakeWatchdogJob, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lc, "WATCHDOG_RESTART_TIMEOUT_S", 0.05)
    out: list[str] = []
    job.exits = False
    assert lc.cmd_restart("all", settings, job.runner, out.append) == 1
    assert out[-1] == "watchdog: pid 100 did not exit within 0s"
    assert ("kickstart",) not in job.events

    job.exits, job.kickstart = True, CommandResult(1, "", "Operation not permitted")
    out.clear()
    assert lc.cmd_restart("watchdog", settings, job.runner, out.append) == 1
    assert out == ["watchdog: launchctl kickstart failed: Operation not permitted"]

    job.kickstart = CommandResult(0)
    monkeypatch.setattr(lc, "control_hello", lambda settings: None)
    out.clear()
    assert lc.cmd_restart("all", settings, job.runner, out.append) == 1
    assert out[-1].startswith("watchdog: restarted, but its control socket did not answer")


def test_restart_all_restarts_the_watchdog_after_a_failed_request(
    settings: Settings, job: FakeWatchdogJob, monkeypatch: pytest.MonkeyPatch
) -> None:
    """New code must reach the watchdog even when it refuses (or an old
    watchdog misunderstands) the service restart."""

    def refused(*args: object) -> dict[str, object]:
        raise ControlError("service_unavailable", "stt: not loaded; tts: not loaded")

    monkeypatch.setattr(lc, "request", refused)
    monkeypatch.setattr(lc, "control_hello", lambda settings: {"version": "9.9"})
    out: list[str] = []
    assert lc.cmd_restart("all", settings, job.runner, out.append) == 1
    assert out[0] == "restart failed: stt: not loaded; tts: not loaded"
    assert out[1].startswith("watchdog: restarted (pid 100 -> pid 200)")


def test_status_without_socket(settings: Settings) -> None:
    out: list[str] = []
    runner = lambda argv: CommandResult(113, "", "Could not find service")  # noqa: E731
    lc.cmd_status(settings, runner, out.append)
    assert "STATE" not in out[0]
    assert out[1].startswith("control socket: not answering")


def test_status_watchdog_running_without_socket(settings: Settings) -> None:
    """The socket failed to start (path too long, someone else's directory):
    say so instead of "watchdog not running"."""

    def runner(argv: Sequence[str]) -> CommandResult:
        if argv[-1].endswith(".watchdog"):
            return CommandResult(0, "state = running\n\tpid = 99\n")
        return CommandResult(113, "", "Could not find service")

    out: list[str] = []
    lc.cmd_status(settings, runner, out.append)
    assert out[1] == f"control socket: {NOT_SERVED}"


def test_watchdog_that_stops_answering(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    def hung(*args: object) -> dict[str, object]:
        raise TimeoutError("timed out")

    monkeypatch.setattr(lc, "request", hung)
    out: list[str] = []
    assert lc.cmd_restart("tts", settings, lambda argv: CommandResult(0), out.append) == 1
    assert out == ["restart failed: watchdog did not answer: timed out"]
    out.clear()
    assert lc.cmd_settings(settings_args(), settings, out.append) == 1
    assert out == ["watchdog did not answer: timed out"]
    with pytest.raises(ControlError, match="did not answer"):
        lc.control_status(settings)


def test_hung_socket_times_out(settings: Settings) -> None:
    """A server that greets and then never answers: a short timeout, no traceback."""
    short = Path(tempfile.mkdtemp(prefix="vc", dir="/tmp"))
    settings.install.venv_dir = short
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path(settings)))
    listener.listen(1)

    def greet_then_hang() -> None:
        conn, _ = listener.accept()
        conn.sendall(b'{"event":"hello","data":{"protocol":1}}\n')
        conn.recv(1024)

    thread = threading.Thread(target=greet_then_hang, daemon=True)
    thread.start()
    try:
        with pytest.raises(ControlError, match="watchdog did not answer"):
            lc._via_control(settings, "status", timeout_s=0.2)
    finally:
        listener.close()
        shutil.rmtree(short, ignore_errors=True)


def test_config_syntax_error_is_one_line(
    settings: Settings, cfg: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg.write_text("[tts\n")
    assert main(["settings"]) == 2
    err = capsys.readouterr().err
    assert err.startswith(f"config error at {cfg}:")
    assert "Traceback" not in err


def test_doctor_socket_missing(tmp_path: Path) -> None:
    check = doctor.check_control_socket(tmp_path / "control.sock")
    assert check.result == "warn"
    running = doctor.check_control_socket(tmp_path / "control.sock", watchdog_pid=99)
    assert running.result == "fail"
    assert NOT_SERVED in running.detail


def test_doctor_socket_not_a_socket(tmp_path: Path) -> None:
    path = tmp_path / "control.sock"
    path.write_text("")
    assert doctor.check_control_socket(path).result == "fail"


# With a socket -----------------------------------------------------------------


@dataclass
class Served:
    settings: Settings
    supervisor: FakeSupervisor
    wait_restarts: Callable[[], None]


@pytest.fixture
def served(settings: Settings, cfg: Path) -> Iterator[Served]:
    """A control server on a background loop, at the settings' socket path."""
    with serving(settings, cfg, FakeSupervisor()) as it:
        yield it


@contextlib.contextmanager
def serving(settings: Settings, cfg: Path, supervisor: Any) -> Iterator[Served]:
    short = Path(tempfile.mkdtemp(prefix="vc", dir="/tmp"))
    settings.install.venv_dir = short / "state"
    loop = asyncio.new_event_loop()
    ops = Operations(supervisor, cfg, engines=engines_list)
    server = ControlServer(socket_path(settings), ops, version="t")
    loop.run_until_complete(server.start())
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()

    def wait_restarts() -> None:
        asyncio.run_coroutine_threadsafe(ops.wait_restarts(), loop).result(5)

    try:
        yield Served(settings, supervisor, wait_restarts)
    finally:
        asyncio.run_coroutine_threadsafe(server.close(), loop).result(5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()
        shutil.rmtree(short, ignore_errors=True)


def test_settings_through_socket(served: Served, cfg: Path) -> None:
    out: list[str] = []
    args = settings_args("set", "tts.speed=1.4")
    assert lc.cmd_settings(args, served.settings, out.append) == 0
    assert tomllib.loads(cfg.read_text())["tts"]["speed"] == 1.4
    assert "tts.speed" in out[0] and "1.4" in out[0]
    assert len(out) == 1, "the service reads the speed for each request: no restart"
    out.clear()
    args = settings_args("set", "tts.voice=bf_emma")
    assert lc.cmd_settings(args, served.settings, out.append) == 0
    assert out[1] == "Restart to apply: voiced restart tts"
    out.clear()
    args = settings_args("set", "tts.voice=nobody")
    assert lc.cmd_settings(args, served.settings, out.append) == 1
    assert out == ["tts.voice: fake has no voice 'nobody'"]


def test_restart_through_socket(served: Served, monkeypatch: pytest.MonkeyPatch) -> None:
    out: list[str] = []
    job = FakeWatchdogJob(served.settings, monkeypatch)
    assert lc.cmd_restart("all", served.settings, job.runner, out.append) == 0
    served.wait_restarts()
    assert sorted(served.supervisor.restarted) == ["stt", "tts"]
    assert out[:2] == ["stt: restarting", "tts: restarting"]
    assert out[-1] == "watchdog: restarted (pid 100 -> pid 200), voiced t answering on the socket"
    served.supervisor.refuse = {"stt": "not_installed"}
    out.clear()
    assert lc.cmd_restart("stt", served.settings, job.runner, out.append) == 1
    assert out == ["restart failed: stt: stt has no launchd plist"]


def test_restart_refused_while_homebrew_holds_the_environment(
    settings: Settings, cfg: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`voiced restart` says why and exits non-zero; it does not print
    "restarting" for a restart the watchdog will not do."""
    from voiced.control.supervisor import WatchdogSupervisor
    from voiced.layout import RunningEnvironment
    from voiced.watchdog import RestartResult, Watchdog

    monkeypatch.setattr(RunningEnvironment, "busy", lambda self: "voiced")
    dog = Watchdog(
        services=lc.watchdog_specs(settings),
        policy=settings.watchdog,
        restart=lambda spec: RestartResult(True, "ok"),
        environment=RunningEnvironment.of((), tmp_path / "voiced"),
    )
    loaded = lambda argv: CommandResult(0, "state = running\npid = 7\n")  # noqa: E731
    sup = WatchdogSupervisor(dog, runner=loaded, installed=lambda label: True)
    with serving(settings, cfg, sup) as it:
        out: list[str] = []
        job = FakeWatchdogJob(it.settings, monkeypatch)
        assert lc.cmd_restart("stt", it.settings, job.runner, out.append) == 1
        assert out == [
            "restart failed: stt: Homebrew is installing voiced; try again when it is done"
        ]


def test_status_through_socket(served: Served) -> None:
    settings = served.settings
    out: list[str] = []
    runner = lambda argv: CommandResult(113, "", "Could not find service")  # noqa: E731
    lc.cmd_status(settings, runner, out.append)
    assert "STATE" in out[0] and "RESTARTS" in out[0] and "running" in out[0]
    assert out[1] == f"control socket: {socket_path(settings)}"
    out.clear()
    lc.cmd_status(settings, runner, out.append, as_json=True)
    rows = json.loads(out[0])
    assert rows[1]["control"]["state"] == "running"


def test_doctor_socket_ok_and_mode(served: Served) -> None:
    path = socket_path(served.settings)
    check = doctor.check_control_socket(path)
    assert check.result == "pass", check.detail
    path.chmod(0o666)
    assert "mode 666" in doctor.check_control_socket(path).detail


def test_doctor_socket_silent(tmp_path: Path) -> None:
    short = Path(tempfile.mkdtemp(prefix="vc", dir="/tmp"))
    try:
        path = short / "control.sock"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(path))
        sock.close()
        path.chmod(0o600)
        assert doctor.check_control_socket(path).result == "fail"
    finally:
        shutil.rmtree(short, ignore_errors=True)
