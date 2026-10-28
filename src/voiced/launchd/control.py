"""``launchctl`` argv builders and thin wrappers.

Standard library only: the watchdog imports this too. Every verb has one
``*_argv`` builder; the same argv is run through a :data:`Runner` or printed
for the user with ``shlex.join``.
The services are LaunchAgents in the user's own domain, ``gui/<uid>``, with
their plists in ``~/Library/LaunchAgents`` (ADR 0035): every verb works as
the user, none needs root. Earlier releases installed LaunchDaemons in
``/Library/LaunchDaemons``; :func:`legacy_daemons` finds those, and
:func:`legacy_removal_command` is the one line, run with sudo, that removes
them.
"""

from __future__ import annotations

import contextlib
import os
import plistlib
import re
import shlex
import signal
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

LAUNCH_DAEMONS_DIR = Path("/Library/LaunchDaemons")
"""Where releases before ADR 0035 installed the services; read only, to migrate."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        return (self.stdout + self.stderr).strip()


Runner = Callable[[Sequence[str]], CommandResult]
"""Runs a command and returns its result. Must not raise for a missing binary."""


class StreamingRunner(Protocol):
    """:func:`run_streaming`'s signature: a :data:`Runner` that can hand
    each output line to ``on_line``."""

    def __call__(
        self, argv: Sequence[str], on_line: Callable[[str], None] | None = None
    ) -> CommandResult: ...


def run_command(argv: Sequence[str], timeout_s: float | None = 30.0) -> CommandResult:
    """Default :data:`Runner` for short queries: captured output, bounded time."""
    try:
        proc = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except FileNotFoundError as exc:
        return CommandResult(127, "", f"{argv[0]}: not found ({exc})")
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"{argv[0]}: timed out after {timeout_s}s")
    except OSError as exc:
        return CommandResult(126, "", f"{argv[0]}: {exc}")
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def run_streaming(
    argv: Sequence[str], on_line: Callable[[str], None] | None = None
) -> CommandResult:
    """:data:`Runner` for long steps (pip, model downloads).

    No timeout and no capture: output streams to the terminal so the user sees
    progress, and nothing is lost if the step takes ten minutes. With
    ``on_line``, each line of output (stdout and stderr together) goes there
    instead, as it comes (``voiced install --json-progress`` keeps its own
    stdout for events); of a progress bar's ``\r`` refreshes only the last
    state of the line. Any exception here (SIGTERM, Ctrl-C) kills the child
    and everything it started (it runs in a session of its own: uv's build
    backends, say) before it goes on, so no install step keeps running.
    """
    pipe = subprocess.PIPE if on_line is not None else None
    try:
        proc = subprocess.Popen(
            list(argv),
            stdout=pipe,
            stderr=subprocess.STDOUT if pipe else None,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        return CommandResult(127, "", f"{argv[0]}: not found ({exc})")
    except OSError as exc:
        return CommandResult(126, "", f"{argv[0]}: {exc}")
    try:
        if on_line is not None and proc.stdout is not None:
            for raw in proc.stdout:
                last = raw.rstrip(b"\r\n").rsplit(b"\r", 1)[-1]
                on_line(last.decode(errors="replace"))
        proc.wait()
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        raise
    finally:
        if proc.stdout is not None:
            proc.stdout.close()
    return CommandResult(proc.returncode)


def domain() -> str:
    """The user's GUI domain: where the agents load, at login or on bootstrap."""
    return f"gui/{os.getuid()}"


LOG_IN = "Log in at the Mac (Screen Sharing counts)"
"""What to do without a login session (:func:`session_refusal`)."""

_NOT_RUN = (124, 126, 127)  # run_command's timeout, OSError, not found


def session_refusal(user: str, runner: Runner = run_command) -> str | None:
    """Why the agents cannot be loaded, or None: they load only into the
    user's GUI domain, which exists only while they are logged in at the
    Mac (an SSH login alone has none). ``voiced install`` refuses with it
    and ``voiced doctor`` reports it."""
    result = runner(["launchctl", "print", domain()])
    if result.ok:
        return None
    if result.returncode in _NOT_RUN:
        return (
            f"Could not check the login session: {result.output.rstrip('.')}. Run "
            "`voiced install` again; if it keeps failing, run `voiced doctor`."
        )
    return (
        f"Not logged in at this Mac as {user}, and an SSH login is not enough: the services "
        f"are LaunchAgents, which load only while you are logged in. {LOG_IN}."
    )


def service_target(label: str) -> str:
    return f"{domain()}/{label}"


def launch_agents_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents"


def plist_path(label: str, home: Path | None = None) -> Path:
    return launch_agents_dir(home) / f"{label}.plist"


def print_argv(label: str) -> list[str]:
    return ["launchctl", "print", service_target(label)]


def kickstart_argv(label: str) -> list[str]:
    """Start a loaded service now (skips ThrottleInterval)."""
    return ["launchctl", "kickstart", service_target(label)]


def bootstrap_argv(plist: Path) -> list[str]:
    """Load the agent ``plist`` into the user's domain."""
    return ["launchctl", "bootstrap", domain(), str(plist)]


def bootout_argv(label: str) -> list[str]:
    """Unload the service until the next login or bootstrap."""
    return ["launchctl", "bootout", service_target(label)]


DEFAULT_LABEL_PREFIX = "io.github.julianbeaulieu.voiced"
"""The prefix every release installed its LaunchDaemons under (ADR 0003):
the old root step rendered the default labels only."""
LEGACY_LABELS = tuple(f"{DEFAULT_LABEL_PREFIX}.{s}" for s in ("stt", "tts", "watchdog"))


@dataclass(frozen=True)
class LegacyDaemon:
    """A LaunchDaemon an earlier release installed: its plist is in
    ``/Library/LaunchDaemons``, or launchd still has it loaded."""

    label: str
    user: str | None  # its UserName, whose install it is; None without a plist


def legacy_daemons(labels: Sequence[str], runner: Runner) -> list[LegacyDaemon]:
    """The LaunchDaemons of the default labels and of ``labels``: by plist
    (world-readable) and by ``launchctl print system/<label>``, which needs
    no root, so a daemon whose plist was deleted while it stayed loaded is
    found too."""
    found = []
    for label in dict.fromkeys((*LEGACY_LABELS, *labels)):
        plist = LAUNCH_DAEMONS_DIR / f"{label}.plist"
        user: object = None
        if plist.is_file():
            try:
                with plist.open("rb") as fh:
                    user = plistlib.load(fh).get("UserName")
            except (OSError, plistlib.InvalidFileException, ValueError):
                pass
        elif not runner(["launchctl", "print", f"system/{label}"]).ok:
            continue
        found.append(LegacyDaemon(label, user if isinstance(user, str) else None))
    return found


def legacy_removal_command(labels: Sequence[str]) -> str:
    """The one line that boots out and deletes the daemons of ``labels``, then
    waits until launchd has let go of them: fixed commands by absolute path,
    no script and nothing the user's account could have written. The menu
    bar app runs the same commands behind its one administrator prompt
    (``LegacyDaemons.swift``)."""
    steps = [f"/bin/launchctl bootout system/{label} 2>/dev/null" for label in labels]
    plists = [f"/Library/LaunchDaemons/{label}.plist" for label in labels]
    steps.append(shlex.join(["/bin/rm", "-f", *plists]))
    # bootout returns while launchd still tears a job down; wait up to 30 s
    # per label, so that what runs next finds the ports free.
    steps += [
        f"n=0; while /bin/launchctl print system/{label} >/dev/null 2>&1 "
        "&& [ $n -lt 150 ]; do /bin/sleep 0.2; n=$((n+1)); done"
        for label in labels
    ]
    return f"sudo /bin/sh -c {shlex.quote('; '.join(steps))}"


def legacy_message(found: Sequence[LegacyDaemon], user: str, *, one_line: bool = False) -> str:
    """What every command says while LaunchDaemons of 0.4.0 or earlier are
    there (ADR 0035): who they belong to, or the one line that removes them
    and what to run next. ``one_line``: for a table cell (``voiced doctor``)."""
    others = sorted({d.user for d in found if d.user not in (user, None)})
    if others:
        return (
            f"voiced is installed for {', '.join(others)} on this Mac as LaunchDaemons from "
            "0.4.0 or earlier; that account has to remove them before voiced can run as yours."
        )
    intro = (
        "voiced still runs as LaunchDaemons from 0.4.0 or earlier, which only an "
        "administrator can remove. Remove them, the last time voiced needs a password, "
        "then install the LaunchAgents"
    )
    session = (
        "The services are down from the removal until `voiced install` loads the agents, "
        "which needs you logged in at this Mac (Screen Sharing counts); an SSH login is "
        "not enough"
    )
    removal = legacy_removal_command([d.label for d in found])
    if one_line:
        return f"{intro}: `{removal}`; then run `voiced install`. {session}."
    return f"{intro}:\n\n  {removal}\n  voiced install\n\n{session}."


@dataclass(frozen=True)
class ServiceStatus:
    """What ``launchctl print gui/<uid>/<label>`` says about a service."""

    label: str
    loaded: bool
    state: str | None = None  # "running", "waiting", "spawn scheduled", ...
    pid: int | None = None
    last_exit_code: str | None = None  # launchctl prints numbers or "(never exited)"
    raw: str = ""

    @property
    def running(self) -> bool:
        return self.loaded and self.pid is not None

    @property
    def summary(self) -> str:
        if not self.loaded:
            return "not loaded"
        return self.state or "loaded"


_STATE_RE = re.compile(r"^\s*state = (.+?)\s*$", re.MULTILINE)
_PID_RE = re.compile(r"^\s*pid = (\d+)\s*$", re.MULTILINE)
_EXIT_RE = re.compile(r"^\s*last exit code = (.+?)\s*$", re.MULTILINE)


def parse_print_output(label: str, result: CommandResult) -> ServiceStatus:
    """Parse ``launchctl print`` output. Non-zero exit means "not loaded"."""
    if not result.ok:
        return ServiceStatus(label=label, loaded=False, raw=result.output)
    text = result.stdout
    state = _STATE_RE.search(text)
    pid = _PID_RE.search(text)
    exit_code = _EXIT_RE.search(text)
    return ServiceStatus(
        label=label,
        loaded=True,
        state=state.group(1) if state else None,
        pid=int(pid.group(1)) if pid else None,
        last_exit_code=exit_code.group(1) if exit_code else None,
        raw=text,
    )


def print_service(label: str, runner: Runner = run_command) -> ServiceStatus:
    return parse_print_output(label, runner(print_argv(label)))


BOOTOUT_WAIT_S = 30.0
"""How long a bootout may take before the job is taken for gone anyway."""


def wait_unloaded(
    label: str, runner: Runner = run_command, sleep: Callable[[float], None] = time.sleep
) -> bool:
    """Wait until ``label`` is gone from the domain; False if it is still
    there after :data:`BOOTOUT_WAIT_S`. ``bootout`` can return while launchd
    still tears the job down, and a bootstrap or kickstart of it then fails
    or acts on the old job."""
    deadline = time.monotonic() + BOOTOUT_WAIT_S
    while print_service(label, runner).loaded:
        if time.monotonic() >= deadline:
            return False
        sleep(0.2)
    return True
