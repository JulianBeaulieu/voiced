"""Thin ``launchctl`` wrappers.

Everything goes through a :class:`Runner` so tests can inject canned output.
Reading (``launchctl print``) works without privileges on the system domain;
``bootstrap`` and ``bootout`` need root. ``kickstart`` depends on the macOS
version and the caller's privileges, so :func:`kickstart` reports the failure
instead of guessing (see ADR 0005 for the watchdog's fallback).
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


def run_command(argv: Sequence[str], timeout_s: float = 30.0) -> CommandResult:
    """Default :data:`Runner`: ``subprocess.run`` with captured text output."""
    try:
        proc = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except FileNotFoundError as exc:
        return CommandResult(127, "", f"{argv[0]}: not found ({exc})")
    except subprocess.TimeoutExpired:
        return CommandResult(124, "", f"{argv[0]}: timed out after {timeout_s:g}s")
    except OSError as exc:
        return CommandResult(126, "", f"{argv[0]}: {exc}")
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def service_target(label: str) -> str:
    return f"{SYSTEM_DOMAIN}/{label}"


def plist_path(label: str) -> Path:
    return LAUNCH_DAEMONS_DIR / f"{label}.plist"


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
        if self.state:
            return self.state
        return "loaded"


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
    return parse_print_output(label, runner(["launchctl", "print", service_target(label)]))


def is_loaded(label: str, runner: Runner = run_command) -> bool:
    return print_service(label, runner).loaded


def kickstart(label: str, *, kill: bool = False, runner: Runner = run_command) -> CommandResult:
    """``launchctl kickstart [-k] system/<label>``: (re)start a loaded service."""
    argv = ["launchctl", "kickstart"]
    if kill:
        argv.append("-k")
    argv.append(service_target(label))
    return runner(argv)


def bootstrap(label: str, runner: Runner = run_command) -> CommandResult:
    """``launchctl bootstrap system <plist>``. Needs root."""
    return runner(["launchctl", "bootstrap", SYSTEM_DOMAIN, str(plist_path(label))])


def bootout(label: str, runner: Runner = run_command) -> CommandResult:
    """``launchctl bootout system/<label>``. Needs root."""
    return runner(["launchctl", "bootout", service_target(label)])


def bootstrap_command(label: str) -> str:
    return f"launchctl bootstrap {SYSTEM_DOMAIN} {plist_path(label)}"


def bootout_command(label: str) -> str:
    return f"launchctl bootout {service_target(label)}"


def kickstart_command(label: str, *, kill: bool = False) -> str:
    flag = " -k" if kill else ""
    return f"launchctl kickstart{flag} {service_target(label)}"
