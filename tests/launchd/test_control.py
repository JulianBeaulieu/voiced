from __future__ import annotations

import os
import signal
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
    """The user's own domain: nothing here needs root (ADR 0035)."""
    gui = f"gui/{os.getuid()}"
    assert control.print_argv("a.b") == ["launchctl", "print", f"{gui}/a.b"]
    assert control.kickstart_argv("a.b") == ["launchctl", "kickstart", f"{gui}/a.b"]
    assert control.bootout_argv("a.b") == ["launchctl", "bootout", f"{gui}/a.b"]
    plist = control.plist_path("a.b")
    assert plist == Path.home() / "Library/LaunchAgents/a.b.plist"
    assert control.bootstrap_argv(plist) == ["launchctl", "bootstrap", gui, str(plist)]


def test_legacy_daemons_name_their_owner(no_launchd: Path) -> None:
    import plistlib

    (no_launchd / "a.b.stt.plist").write_bytes(plistlib.dumps({"UserName": "someone"}))
    (no_launchd / "a.b.tts.plist").write_bytes(b"not a plist")
    nothing_loaded = lambda argv: CommandResult(113)  # noqa: E731
    found = control.legacy_daemons(["a.b.stt", "a.b.tts", "a.b.watchdog"], nothing_loaded)
    assert [(d.label, d.user) for d in found] == [("a.b.stt", "someone"), ("a.b.tts", None)]


def test_the_login_session_wording() -> None:
    """The one place the wording is pinned: the refusal ``voiced install``
    and ``voiced doctor`` give, and the migration message's last line."""
    assert control.session_refusal("someone", lambda argv: CommandResult(0)) is None
    assert control.session_refusal("someone", lambda argv: CommandResult(113)) == (
        "Not logged in at this Mac as someone, and an SSH login is not enough: the services "
        "are LaunchAgents, which load only while you are logged in. Log in at the Mac "
        "(Screen Sharing counts)."
    )
    missing = control.session_refusal("someone", lambda argv: CommandResult(127, "", "gone"))
    assert missing == (
        "Could not check the login session: gone. Run `voiced install` again; if it keeps "
        "failing, run `voiced doctor`."
    )
    found = [control.LegacyDaemon("a.b.stt", "someone")]
    assert control.legacy_message(found, "someone").endswith(
        "\n  voiced install\n\nThe services are down from the removal until `voiced install` "
        "loads the agents, which needs you logged in at this Mac (Screen Sharing counts); "
        "an SSH login is not enough."
    )


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


def test_run_streaming_kills_what_the_child_started(tmp_path: Path) -> None:
    """uv runs build backends of its own; they go with it."""
    pids: list[int] = []

    def on_line(line: str) -> None:
        pids.append(int(line))
        raise KeyboardInterrupt

    script = "sleep 30 & echo $!; wait"
    with pytest.raises(KeyboardInterrupt):
        control.run_streaming(["/bin/sh", "-c", script], on_line=on_line)
    deadline = time.monotonic() + 5  # the orphan is reaped by launchd, not by us
    while time.monotonic() < deadline:
        try:
            os.kill(pids[0], 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    os.kill(pids[0], signal.SIGKILL)
    pytest.fail("the grandchild outlived the interrupted step")


def test_run_streaming_missing_binary() -> None:
    assert control.run_streaming(["/nonexistent/binary-for-voiced-tests"]).returncode == 127
