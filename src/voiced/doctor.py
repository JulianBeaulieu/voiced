"""``voiced doctor``: machine checks, each one a small testable function.

Every check takes the external world as arguments (a command :data:`Runner`,
a ``which`` function, a file-exists predicate) and returns a :class:`Check`
with ``pass``, ``warn`` or ``fail``. :func:`run_all` wires in the real
implementations.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from voiced.config import HTTP_SERVICES, SERVICES, ServiceName, Settings
from voiced.engines.kokoro import (
    ENV_ESPEAK_LIBRARY,
    ESPEAK_INSTALL_HINT,
    EspeakNotFound,
    KokoroEngine,
    espeak_candidates,
)
from voiced.launchd.control import Runner, plist_path, print_service, run_command
from voiced.launchd.install import METAL_PROBE_FILE
from voiced.launchd.metal_probe import macos_version as _macos_version
from voiced.table import render_table
from voiced.watchdog import Observation, poll_health

Result = Literal["pass", "warn", "fail"]
Which = Callable[[str], str | None]
Exists = Callable[[Path], bool]

MIN_MACOS_MAJOR = 14
MIN_TOTAL_GB = 8
WARN_TOTAL_GB = 16
WARN_AVAILABLE_GB = 4
HOMEBREW_BINS = ("/opt/homebrew/bin", "/usr/local/bin")


@dataclass(frozen=True)
class Check:
    name: str
    result: Result
    detail: str = ""


def _which(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for prefix in HOMEBREW_BINS:
        candidate = Path(prefix) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


# Hardware and OS -----------------------------------------------------------


def check_apple_silicon(runner: Runner, machine: str | None = None) -> Check:
    machine = machine or platform.machine()
    brand = runner(["sysctl", "-n", "machdep.cpu.brand_string"]).stdout.strip() or "unknown CPU"
    if machine == "arm64":
        return Check("Apple Silicon", "pass", brand)
    translated = runner(["sysctl", "-n", "sysctl.proc_translated"]).stdout.strip()
    if translated == "1":
        return Check(
            "Apple Silicon",
            "fail",
            f"Python runs under Rosetta ({brand}); install an arm64 Python",
        )
    return Check("Apple Silicon", "fail", f"{machine} ({brand}); MLX needs Apple Silicon")


def macos_version(runner: Runner) -> str:
    return _macos_version(runner(["sw_vers", "-productVersion"]).stdout)


def check_macos_version(runner: Runner, system: str | None = None) -> Check:
    system = system or platform.system()
    if system != "Darwin":
        return Check("macOS version", "fail", f"{system}; voiced runs on macOS only")
    version = macos_version(runner)
    match = re.match(r"(\d+)", version)
    major = int(match.group(1)) if match else 0
    if major >= MIN_MACOS_MAJOR:
        return Check("macOS version", "pass", version)
    if major >= MIN_MACOS_MAJOR - 1:
        return Check("macOS version", "warn", f"{version}; tested on {MIN_MACOS_MAJOR}+")
    return Check("macOS version", "fail", f"{version}; needs macOS {MIN_MACOS_MAJOR} or newer")


def check_python(version: tuple[int, int, int] | None = None) -> Check:
    v = version or sys.version_info[:3]
    text = ".".join(str(part) for part in v)
    if (3, 11) <= v[:2] <= (3, 13):
        return Check("Python", "pass", f"{text} ({sys.executable})")
    return Check("Python", "fail", f"{text}; voiced needs Python 3.11 to 3.13")


# External programs ---------------------------------------------------------


def check_espeak(required: bool = True) -> Check:
    """Does the Kokoro engine find an espeak-ng library?

    Uses the engine's own lookup (:func:`voiced.engines.kokoro.espeak_candidates`):
    ``VOICED_ESPEAK_LIBRARY`` when set, else the Homebrew keg, which is found
    whether or not it is linked. An ``espeak-ng`` binary on ``PATH`` is
    irrelevant: the engine loads the library. ``required`` is whether the
    configured TTS engine needs it; if not, any problem is only a warning.
    The library is found, not loaded: the engine's phonemizer self-test runs
    only in the service.
    """
    name = "espeak-ng"
    result: Result = "fail" if required else "warn"
    try:
        found = espeak_candidates()
    except EspeakNotFound as exc:
        return Check(name, result, str(exc))
    if found:
        library, data = found[0]
        override = library == os.environ.get(ENV_ESPEAK_LIBRARY)
        detail = f"found {library} ({ENV_ESPEAK_LIBRARY if override else 'Homebrew'})"
        if data:
            detail += f", data {data}"
        return Check(name, "pass", detail)
    return Check(
        name,
        result,
        f"library not found; Kokoro needs it: {ESPEAK_INSTALL_HINT} "
        f"(or set {ENV_ESPEAK_LIBRARY} to libespeak-ng.dylib)",
    )


def check_ffmpeg(which: Which = _which) -> Check:
    found = which("ffmpeg")
    if found:
        return Check("ffmpeg", "pass", found)
    return Check(
        "ffmpeg",
        "fail",
        "not found; required to decode uploads and encode mp3/opus: brew install ffmpeg",
    )


# Memory -------------------------------------------------------------------

_PAGE_RE = re.compile(r"page size of (\d+) bytes")
_VMSTAT_RE = re.compile(r"^(Pages (?:free|inactive|speculative)):\s+(\d+)\.", re.MULTILINE)


def parse_vm_stat(text: str) -> int | None:
    """Available bytes from ``vm_stat`` output (free + inactive + speculative)."""
    page = _PAGE_RE.search(text)
    if not page:
        return None
    pages = sum(int(m.group(2)) for m in _VMSTAT_RE.finditer(text))
    return pages * int(page.group(1))


def check_memory(runner: Runner) -> Check:
    total_text = runner(["sysctl", "-n", "hw.memsize"]).stdout.strip()
    if not total_text.isdigit():
        return Check("Memory", "warn", "could not read hw.memsize")
    total_gb = int(total_text) / 2**30
    available = parse_vm_stat(runner(["vm_stat"]).stdout)
    avail_text = f"{available / 2**30:.1f} GB available" if available is not None else ""
    detail = f"{total_gb:.0f} GB total" + (f", {avail_text}" if avail_text else "")
    if total_gb < MIN_TOTAL_GB:
        return Check("Memory", "fail", f"{detail}; need at least {MIN_TOTAL_GB} GB")
    if total_gb < WARN_TOTAL_GB:
        return Check("Memory", "warn", f"{detail}; {WARN_TOTAL_GB} GB recommended")
    if available is not None and available / 2**30 < WARN_AVAILABLE_GB:
        return Check("Memory", "warn", f"{detail}; models need about 3 GB free")
    return Check("Memory", "pass", detail)


# Ports --------------------------------------------------------------------


def listener_pids(port: int, runner: Runner) -> list[int] | None:
    """Pids listening on ``port`` via lsof; None if lsof is unavailable."""
    result = runner(["lsof", "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"])
    if result.returncode == 127:
        return None
    return [int(line) for line in result.stdout.split() if line.isdigit()]


def check_port(name: str, port: int, runner: Runner, venv_dir: Path | None = None) -> Check:
    """Who listens on ``port``? Uses ``lsof``; absence of lsof is reported, not fatal."""
    label = f"Port {port} ({name})"
    pids = listener_pids(port, runner)
    if pids is None:
        return Check(label, "warn", "lsof not available; cannot check")
    if not pids:
        return Check(label, "pass", "free")
    commands: list[str] = []
    ours = False
    for pid in pids:
        exe = runner(["ps", "-o", "comm=", "-p", str(pid)]).stdout.strip() or "unknown"
        commands.append(exe)
        if venv_dir is not None and Path(exe).is_relative_to(venv_dir):
            ours = True
    pid_text = ", ".join(str(p) for p in pids)
    if ours:
        return Check(label, "pass", f"in use by voiced (pid {pid_text})")
    return Check(label, "warn", f"in use by {', '.join(commands)} (pid {pid_text})")


def check_ports(settings: Settings, runner: Runner) -> list[Check]:
    ports = [
        ("stt http", settings.stt.http_port),
        ("stt wyoming", settings.stt.wyoming_port),
        ("tts http", settings.tts.http_port),
        ("tts wyoming", settings.tts.wyoming_port),
    ]
    return [check_port(name, port, runner, settings.install.venv_dir) for name, port in ports]


# Disk and launchd ---------------------------------------------------------


def check_filevault(runner: Runner) -> Check:
    out = runner(["fdesetup", "status"]).stdout.strip()
    if "FileVault is On" in out:
        return Check(
            "FileVault",
            "warn",
            "on: after a reboot the daemons start only once someone unlocks the disk "
            "at the login screen (no GUI login needed after that)",
        )
    if "FileVault is Off" in out:
        return Check("FileVault", "pass", "off; unattended reboots work")
    return Check("FileVault", "warn", out or "could not read fdesetup status")


def check_daemon(service: str, label: str, runner: Runner, exists: Exists = Path.is_file) -> Check:
    name = f"Daemon {service}"
    installed = exists(plist_path(label))
    status = print_service(label, runner)
    if installed and status.loaded:
        detail = status.summary + (f", pid {status.pid}" if status.pid else "")
        if status.last_exit_code and status.last_exit_code not in ("0", "(never exited)"):
            detail += f", last exit {status.last_exit_code}"
        return Check(name, "pass", detail)
    if installed:
        return Check(name, "fail", f"{plist_path(label)} exists but is not loaded")
    if status.loaded:
        return Check(name, "warn", "loaded from somewhere other than /Library/LaunchDaemons")
    return Check(name, "warn", "not installed; run voiced install and the printed sudo script")


def check_daemons(settings: Settings, runner: Runner, exists: Exists = Path.is_file) -> list[Check]:
    return [
        check_daemon(service, settings.launchd.label(service), runner, exists)
        for service in SERVICES
    ]


# Metal --------------------------------------------------------------------

_METAL_SNIPPET = "import mlx.core as mx; print(mx.default_device())"


def check_metal_shell(runner: Runner, python: Path | None) -> Check:
    """Can MLX see the GPU from this shell, using the install venv's Python?"""
    name = "Metal (shell)"
    if python is None:
        return Check(name, "warn", "install venv not found; run voiced install")
    result = runner([str(python), "-c", _METAL_SNIPPET])
    if result.ok and "gpu" in result.stdout.lower():
        return Check(name, "pass", result.stdout.strip())
    if "No module named" in result.output:
        return Check(name, "warn", "mlx not installed in the venv (stt/tts extras missing)")
    return Check(name, "fail", result.output.splitlines()[-1] if result.output else "no output")


@dataclass(frozen=True)
class DaemonEvidence:
    """What a running service tells us, with proof it is the launchd-managed one."""

    service: str
    health: Observation
    launchd_pid: int | None  # from launchctl print
    listener_pids: list[int] | None  # from lsof on the HTTP port


def check_metal_daemon(
    probe_file: Path,
    evidence: Iterable[DaemonEvidence],
    read_text: Callable[[Path], str | None] | None = None,
    running_macos: str | None = None,
) -> Check:
    """Did MLX work from a LaunchDaemon on this machine?

    Accepted evidence, strongest first:

    1. A service whose ``/health`` says a real engine (not ``fake``) has its
       model loaded, where the pid listening on its HTTP port is the pid
       launchd reports for the daemon. That rules out a ``voiced serve`` run by
       hand on the same port.
    2. The JSON written by ``voiced doctor --metal-probe``, if it says ``ok``,
       was produced under launchd's system manager with pid 1 as parent, and
       on the macOS version running now.

    Anything else is "unknown", never a pass.
    """
    name = "Metal (daemon)"
    for item in evidence:
        payload = item.health.payload or {}
        if not payload.get("model_loaded") or payload.get("engine") in (None, "fake"):
            continue
        if item.launchd_pid is None or not item.listener_pids:
            continue
        if item.launchd_pid in item.listener_pids:
            return Check(
                name,
                "pass",
                f"{item.service} daemon (pid {item.launchd_pid}) has {payload['engine']} loaded",
            )
    reader = read_text or _read_text
    text = reader(probe_file)
    if text is None:
        return Check(
            name, "warn", "unknown; run `voiced doctor --metal-probe` and the printed sudo script"
        )
    try:
        data = json.loads(text)
    except ValueError:
        return Check(name, "warn", f"{probe_file} is not valid JSON")
    ctx = data.get("context") or {}
    manager = ctx.get("launchd_managername")
    if manager != "System" or ctx.get("ppid") != 1:
        return Check(
            name,
            "warn",
            f"{probe_file} was not produced by a LaunchDaemon "
            f"(managername={manager!r}, ppid={ctx.get('ppid')!r})",
        )
    if running_macos and ctx.get("macos") != running_macos:
        return Check(
            name,
            "warn",
            f"{probe_file} is from macOS {ctx.get('macos')}, now running {running_macos}; "
            "re-run `voiced doctor --metal-probe`",
        )
    if data.get("ok"):
        return Check(
            name,
            "pass",
            f"probe ran under launchd: {data.get('default_device')}, "
            f"matmul {data.get('matmul_ms')} ms",
        )
    return Check(name, "fail", f"probe under launchd: {data.get('error')}")


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


# Everything ---------------------------------------------------------------


def fetch_health(
    settings: Settings, timeout_s: float = 2.0
) -> list[tuple[ServiceName, Observation]]:
    out: list[tuple[ServiceName, Observation]] = []
    for service in HTTP_SERVICES:
        url = f"{settings.http_url(service)}/health"
        obs = poll_health(url, timeout_s=timeout_s, api_key=settings.server.api_key)
        out.append((service, obs))
    return out


def daemon_evidence(settings: Settings, runner: Runner) -> list[DaemonEvidence]:
    items: list[DaemonEvidence] = []
    for service, obs in fetch_health(settings):
        status = print_service(settings.launchd.label(service), runner)
        pids = listener_pids(settings.http_port(service), runner)
        items.append(DaemonEvidence(service, obs, status.pid, pids))
    return items


def run_all(settings: Settings, runner: Runner = run_command) -> list[Check]:
    venv_python = settings.install.venv_dir / "bin" / "python"
    return [
        check_apple_silicon(runner),
        check_macos_version(runner),
        check_python(),
        check_espeak(required=settings.tts.engine == KokoroEngine.name),
        check_ffmpeg(),
        check_memory(runner),
        *check_ports(settings, runner),
        check_filevault(runner),
        *check_daemons(settings, runner),
        check_metal_shell(runner, venv_python if venv_python.exists() else None),
        check_metal_daemon(
            settings.server.log_dir / METAL_PROBE_FILE,
            daemon_evidence(settings, runner),
            running_macos=macos_version(runner),
        ),
    ]


def worst(checks: Sequence[Check]) -> Result:
    if any(c.result == "fail" for c in checks):
        return "fail"
    if any(c.result == "warn" for c in checks):
        return "warn"
    return "pass"


def render_checks(checks: Sequence[Check]) -> str:
    rows = [(c.name, c.result, c.detail) for c in checks]
    return render_table(("CHECK", "RESULT", "DETAIL"), rows)
