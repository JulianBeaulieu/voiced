from __future__ import annotations

from collections.abc import Sequence

from voiced.launchd import control
from voiced.launchd.control import CommandResult, parse_print_output

RUNNING = """system/io.github.julianbeaulieu.voiced.tts = {
\tactive count = 1
\tpath = /Library/LaunchDaemons/io.github.julianbeaulieu.voiced.tts.plist
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
    status = parse_print_output(
        "x", CommandResult(113, "", 'Could not find service "x" in domain for system')
    )
    assert not status.loaded
    assert status.summary == "not loaded"


def test_wrappers_build_expected_argv() -> None:
    calls: list[list[str]] = []

    def runner(argv: Sequence[str]) -> CommandResult:
        calls.append(list(argv))
        return CommandResult(0, RUNNING)

    control.kickstart("a.b", kill=True, runner=runner)
    control.bootout("a.b", runner=runner)
    control.bootstrap("a.b", runner=runner)
    assert control.print_service("a.b", runner=runner).pid == 4242
    assert calls == [
        ["launchctl", "kickstart", "-k", "system/a.b"],
        ["launchctl", "bootout", "system/a.b"],
        ["launchctl", "bootstrap", "system", "/Library/LaunchDaemons/a.b.plist"],
        ["launchctl", "print", "system/a.b"],
    ]


def test_run_command_missing_binary() -> None:
    result = control.run_command(["/nonexistent/binary-for-voiced-tests"])
    assert result.returncode == 127
    assert not result.ok
