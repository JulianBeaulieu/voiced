"""``launchctl`` argv builders and thin wrappers.

Standard library only: the watchdog imports this too. Every verb has one
``*_argv(label)`` builder; the same argv is run through a :data:`Runner`,
printed for the user with ``shlex.join``, or written into the sudo scripts.
Reading (``launchctl print``) and plain ``kickstart`` work without root on
the system domain; ``bootstrap``, ``bootout`` and ``kickstart -k`` of a
running job do not (ADR 0004).
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

LAUNCH_DAEMONS_DIR = Path("/Library/LaunchDaemons")
SYSTEM_DOMAIN = "system"


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


def run_streaming(argv: Sequence[str]) -> CommandResult:
    """:data:`Runner` for long steps (pip, model downloads).

    No timeout and no capture: output streams to the terminal so the user sees
    progress, and nothing is lost if the step takes ten minutes.
    """
    try:
        proc = subprocess.run(list(argv), check=False)
    except FileNotFoundError as exc:
        return CommandResult(127, "", f"{argv[0]}: not found ({exc})")
    except OSError as exc:
        return CommandResult(126, "", f"{argv[0]}: {exc}")
    return CommandResult(proc.returncode)


def service_target(label: str) -> str:
    return f"{SYSTEM_DOMAIN}/{label}"


def plist_path(label: str) -> Path:
    return LAUNCH_DAEMONS_DIR / f"{label}.plist"


def print_argv(label: str) -> list[str]:
    return ["launchctl", "print", service_target(label)]


def kickstart_argv(label: str) -> list[str]:
    """Start a loaded service now (skips ThrottleInterval). Works without root."""
    return ["launchctl", "kickstart", service_target(label)]


def bootstrap_argv(label: str) -> list[str]:
    """Load the installed plist. Needs root."""
    return ["launchctl", "bootstrap", SYSTEM_DOMAIN, str(plist_path(label))]


def bootout_argv(label: str) -> list[str]:
    """Unload the service. Needs root."""
    return ["launchctl", "bootout", service_target(label)]


@dataclass(frozen=True)
class ServiceStatus:
    """What ``launchctl print system/<label>`` says about a service."""

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
