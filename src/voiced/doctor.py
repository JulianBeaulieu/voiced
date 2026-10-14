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

from voiced.config import Settings
from voiced.launchd.control import Runner, plist_path, print_service, run_command
from voiced.watchdog import Observation, poll_health

Result = Literal["pass", "warn", "fail"]
Which = Callable[[str], str | None]
Exists = Callable[[Path], bool]

MIN_MACOS_MAJOR = 14
MIN_TOTAL_GB = 8
WARN_TOTAL_GB = 16
WARN_AVAILABLE_GB = 4
METAL_PROBE_FILE = "metal-probe.json"
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


def check_macos_version(runner: Runner, system: str | None = None) -> Check:
    system = system or platform.system()
    if system != "Darwin":
        return Check("macOS version", "fail", f"{system}; voiced runs on macOS only")
    version = runner(["sw_vers", "-productVersion"]).stdout.strip() or platform.mac_ver()[0]
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


def check_espeak(which: Which = _which) -> Check:
    found = which("espeak-ng")
    if found:
        return Check("espeak-ng", "pass", found)
    return Check(
        "espeak-ng",
        "warn",
        "not found; Kokoro's phonemizer needs it: brew install espeak-ng",
    )


def check_ffmpeg(which: Which = _which) -> Check:
    found = which("ffmpeg")
    if found:
        return Check("ffmpeg", "pass", found)
    return Check("ffmpeg", "warn", "not found; only WAV uploads will decode: brew install ffmpeg")


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


def check_port(name: str, port: int, runner: Runner, venv_dir: Path | None = None) -> Check:
    """Who listens on ``port``? Uses ``lsof``; absence of lsof is reported, not fatal."""
    result = runner(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fcpn"])
    label = f"Port {port} ({name})"
    if result.returncode == 127:
        return Check(label, "warn", "lsof not available; cannot check")
    if not result.stdout.strip():
        return Check(label, "pass", "free")
    pids = [line[1:] for line in result.stdout.splitlines() if line.startswith("p")]
    commands = sorted({line[1:] for line in result.stdout.splitlines() if line.startswith("c")})
    who = ", ".join(commands) or "unknown"
    pid_text = f" (pid {', '.join(pids)})" if pids else ""
    if venv_dir is not None and pids:
        # If the listener's executable lives in our venv it is a voiced service.
        for pid in pids:
            exe = runner(["ps", "-o", "comm=", "-p", pid]).stdout.strip()
            if exe.startswith(str(venv_dir)):
                return Check(label, "pass", f"in use by voiced{pid_text}")
    return Check(label, "warn", f"in use by {who}{pid_text}")


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
        for service in ("stt", "tts", "watchdog")
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


def check_metal_daemon(
    probe_file: Path,
    health: Iterable[tuple[str, Observation]],
    read_text: Callable[[Path], str | None] | None = None,
) -> Check:
    """Did MLX work from a LaunchDaemon on this machine?

    Evidence, strongest first: a running daemon-managed service that reports
    its model loaded (real engine, not ``fake``); or the JSON written by
    ``voiced doctor --metal-probe``. Without either we can only say "unknown".
    """
    name = "Metal (daemon)"
    for service, obs in health:
        payload = obs.payload or {}
        if payload.get("model_loaded") and payload.get("engine") not in (None, "fake"):
            return Check(name, "pass", f"{service} daemon has {payload.get('engine')} loaded")
    reader = read_text or _read_text
    text = reader(probe_file)
    if text is not None:
        try:
            data = json.loads(text)
        except ValueError:
            return Check(name, "warn", f"{probe_file} is not valid JSON")
        manager = data.get("context", {}).get("launchd_managername", "?")
        if data.get("ok"):
            return Check(
                name,
                "pass",
                f"probe ran under launchd ({manager}): {data.get('default_device')}, "
                f"matmul {data.get('matmul_ms')} ms",
            )
        return Check(name, "fail", f"probe under launchd ({manager}): {data.get('error')}")
    return Check(
        name,
        "warn",
        "unknown; run `voiced doctor --metal-probe` and the printed sudo script",
    )


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


# Everything ---------------------------------------------------------------


def fetch_health(settings: Settings, timeout_s: float = 2.0) -> list[tuple[str, Observation]]:
    host = "127.0.0.1" if settings.server.bind in ("0.0.0.0", "") else settings.server.bind
    out: list[tuple[str, Observation]] = []
    for service, port in (("stt", settings.stt.http_port), ("tts", settings.tts.http_port)):
        url = f"http://{host}:{port}/health"
        out.append(
            (service, poll_health(url, timeout_s=timeout_s, api_key=settings.server.api_key))
        )
    return out


def run_all(settings: Settings, runner: Runner = run_command) -> list[Check]:
    venv_python = settings.install.venv_dir / "bin" / "python"
    checks: list[Check] = [
        check_apple_silicon(runner),
        check_macos_version(runner),
        check_python(),
        check_espeak(),
        check_ffmpeg(),
        check_memory(runner),
        *check_ports(settings, runner),
        check_filevault(runner),
        *check_daemons(settings, runner),
        check_metal_shell(runner, venv_python if venv_python.exists() else None),
        check_metal_daemon(settings.server.log_dir / METAL_PROBE_FILE, fetch_health(settings)),
    ]
    return checks


def worst(checks: Sequence[Check]) -> Result:
    if any(c.result == "fail" for c in checks):
        return "fail"
    if any(c.result == "warn" for c in checks):
        return "warn"
    return "pass"


def render_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Plain-text table: left-aligned columns, two spaces between, no borders."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    # The last column is not padded so long details do not trail whitespace.
    def fmt(cells: Sequence[str]) -> str:
        parts = [c.ljust(widths[i]) for i, c in enumerate(cells[:-1])]
        parts.append(cells[-1])
        return "  ".join(parts).rstrip()

    return "\n".join([fmt(headers), *(fmt(r) for r in rows)])


def render_checks(checks: Sequence[Check]) -> str:
    return render_table(
        ("CHECK", "RESULT", "DETAIL"), [(c.name, c.result, c.detail) for c in checks]
    )
