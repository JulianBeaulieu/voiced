from __future__ import annotations

import os
import time
from pathlib import Path

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


_BOTH_STREAMS = "echo out; echo err >&2; printf 'last'; exit 3"


def test_run_streaming_inherits_the_terminal(capfd: pytest.CaptureFixture[str]) -> None:
    """No capture: the step's output reaches the terminal as it comes."""
    result = control.run_streaming(["/bin/sh", "-c", _BOTH_STREAMS])
    assert result.returncode == 3 and result.output == ""
    out, err = capfd.readouterr()
    assert (out, err) == ("out\nlast", "err\n")


def test_run_streaming_hands_lines_to_on_line(capfd: pytest.CaptureFixture[str]) -> None:
    """--json-progress: stdout and stderr together, line by line, and none
    of it on this process's stdout, which carries the events."""
    lines: list[str] = []
    result = control.run_streaming(["/bin/sh", "-c", _BOTH_STREAMS], on_line=lines.append)
    assert result.returncode == 3
    assert lines == ["out", "err", "last"]
    assert capfd.readouterr() == ("", "")


def test_run_streaming_keeps_the_last_state_of_a_progress_bar() -> None:
    """A bar redraws one line with \\r; each redraw is not a line of its own."""
    lines: list[str] = []
    script = "printf ' 10%%\\r 50%%\\r100%%\\n'; printf 'done\\r\\n'"
    assert control.run_streaming(["/bin/sh", "-c", script], on_line=lines.append).ok
    assert lines == ["100%", "done"]


def test_run_streaming_kills_the_child_when_interrupted(tmp_path: Path) -> None:
    """The app's Cancel is SIGTERM, raised in this process as an exception
    while a step runs: the step's child must not keep installing."""
    pids: list[int] = []

    def on_line(line: str) -> None:
        pids.append(int(line))
        raise KeyboardInterrupt  # stands in for SIGTERM's SystemExit

    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        control.run_streaming(["/bin/sh", "-c", "echo $$; exec sleep 30"], on_line=on_line)
    assert time.monotonic() - started < 10
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)  # killed and reaped


def test_run_streaming_missing_binary() -> None:
    assert control.run_streaming(["/nonexistent/binary-for-voiced-tests"]).returncode == 127
