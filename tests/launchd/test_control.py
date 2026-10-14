from __future__ import annotations

import subprocess
from typing import Any

import pytest

from voiced.launchd import control
from voiced.launchd.control import CommandResult, parse_print_output

RUNNING = """system/a.b.tts = {
\tactive count = 1
\tpath = /Library/LaunchDaemons/a.b.tts.plist
\ttype = LaunchDaemon
\tstate = running

\tprogram = /Users/someone/.voiced/bin/voiced
\tpid = 4242
\tlast exit code = (never exited)
}
"""

WAITING = """system/x = {
\tstate = waiting
\tlast exit code = 1
}
"""


def test_parse_running() -> None:
    status = parse_print_output("x", CommandResult(0, RUNNING))
    assert status.loaded and status.running
    assert status.state == "running"
    assert status.pid == 4242
    assert status.last_exit_code == "(never exited)"
    assert status.summary == "running"


def test_parse_waiting_without_pid() -> None:
    status = parse_print_output("x", CommandResult(0, WAITING))
    assert status.loaded and not status.running
    assert status.pid is None
    assert status.last_exit_code == "1"


def test_parse_not_loaded() -> None:
    status = parse_print_output("x", CommandResult(113, "", 'Could not find service "x"'))
    assert not status.loaded
    assert status.summary == "not loaded"


def test_argv_builders() -> None:
    assert control.print_argv("a.b") == ["launchctl", "print", "system/a.b"]
    assert control.kickstart_argv("a.b") == ["launchctl", "kickstart", "system/a.b"]
    assert control.bootout_argv("a.b") == ["launchctl", "bootout", "system/a.b"]
    assert control.bootstrap_argv("a.b") == [
        "launchctl",
        "bootstrap",
        "system",
        "/Library/LaunchDaemons/a.b.plist",
    ]


def test_run_command_missing_binary() -> None:
    result = control.run_command(["/nonexistent/binary-for-voiced-tests"])
    assert result.returncode == 127
    assert not result.ok


def test_run_command_times_out() -> None:
    result = control.run_command(["/bin/sleep", "5"], timeout_s=0.1)
    assert result.returncode == 124


def test_run_streaming_has_no_timeout_and_no_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(control.subprocess, "run", fake_run)
    assert control.run_streaming(["pip", "install", "x"]).ok
    assert seen["argv"] == ["pip", "install", "x"]
    assert "timeout" not in seen["kwargs"] or seen["kwargs"]["timeout"] is None
    assert not seen["kwargs"].get("capture_output")
    assert "stdout" not in seen["kwargs"]


def test_run_streaming_missing_binary() -> None:
    assert control.run_streaming(["/nonexistent/binary-for-voiced-tests"]).returncode == 127
