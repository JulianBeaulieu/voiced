"""Each doctor check with an injected command runner."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from voiced import doctor
from voiced.config import Settings
from voiced.launchd.control import CommandResult
from voiced.watchdog import Observation


def make_runner(table: dict[str, CommandResult]) -> doctor.Runner:
    """Match on the first word plus up to three args, longest key first."""

    def runner(argv: Sequence[str]) -> CommandResult:
        joined = " ".join(argv)
        for key in sorted(table, key=len, reverse=True):
            if joined.startswith(key):
                return table[key]
        return CommandResult(127, "", f"{argv[0]}: not found")

    return runner


def test_apple_silicon() -> None:
    runner = make_runner({"sysctl -n machdep.cpu.brand_string": CommandResult(0, "Apple M1 Max\n")})
    assert doctor.check_apple_silicon(runner, machine="arm64") == doctor.Check(
        "Apple Silicon", "pass", "Apple M1 Max"
    )
    intel = doctor.check_apple_silicon(runner, machine="x86_64")
    assert intel.result == "fail"
    rosetta = make_runner(
        {
            "sysctl -n machdep.cpu.brand_string": CommandResult(0, "Apple M1 Max\n"),
            "sysctl -n sysctl.proc_translated": CommandResult(0, "1\n"),
        }
    )
    assert "Rosetta" in doctor.check_apple_silicon(rosetta, machine="x86_64").detail


def test_macos_version() -> None:
    assert (
        doctor.check_macos_version(
            make_runner({"sw_vers": CommandResult(0, "15.7.4\n")}), system="Darwin"
        ).result
        == "pass"
    )
    assert (
        doctor.check_macos_version(
            make_runner({"sw_vers": CommandResult(0, "13.6\n")}), system="Darwin"
        ).result
        == "warn"
    )
    assert (
        doctor.check_macos_version(
            make_runner({"sw_vers": CommandResult(0, "12.1\n")}), system="Darwin"
        ).result
        == "fail"
    )
    assert doctor.check_macos_version(make_runner({}), system="Linux").result == "fail"


def test_python_version() -> None:
    assert doctor.check_python((3, 12, 4)).result == "pass"
    assert doctor.check_python((3, 10, 0)).result == "fail"
    assert doctor.check_python((3, 14, 0)).result == "fail"


def test_external_programs() -> None:
    assert doctor.check_espeak(lambda n: "/opt/homebrew/bin/espeak-ng").result == "pass"
    missing = doctor.check_espeak(lambda n: None)
    assert missing.result == "warn" and "brew install espeak-ng" in missing.detail
    assert doctor.check_ffmpeg(lambda n: None).result == "warn"
    assert doctor.check_ffmpeg(lambda n: "/usr/local/bin/ffmpeg").result == "pass"


VM_STAT = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               400000.
Pages active:                             500000.
Pages inactive:                           100000.
Pages speculative:                         50000.
Pages wired down:                         200000.
"""


def test_memory() -> None:
    assert doctor.parse_vm_stat(VM_STAT) == 550000 * 16384
    big = make_runner(
        {
            "sysctl -n hw.memsize": CommandResult(0, str(64 * 2**30)),
            "vm_stat": CommandResult(0, VM_STAT),
        }
    )
    check = doctor.check_memory(big)
    assert check.result == "pass" and "64 GB total" in check.detail
    small = make_runner({"sysctl -n hw.memsize": CommandResult(0, str(8 * 2**30))})
    assert doctor.check_memory(small).result == "warn"
    tiny = make_runner({"sysctl -n hw.memsize": CommandResult(0, str(4 * 2**30))})
    assert doctor.check_memory(tiny).result == "fail"
    assert doctor.check_memory(make_runner({})).result == "warn"


def test_ports() -> None:
    free = make_runner({"lsof": CommandResult(1, "")})
    assert doctor.check_port("tts http", 8880, free).result == "pass"
    busy = make_runner(
        {
            "lsof -nP -iTCP:8880": CommandResult(0, "p123\ncnode\nn*:8880\n"),
            "ps -o comm= -p 123": CommandResult(0, "/usr/local/bin/node\n"),
        }
    )
    check = doctor.check_port("tts http", 8880, busy, Path("/Users/u/.voiced"))
    assert check.result == "warn" and "node" in check.detail and "pid 123" in check.detail
    ours = make_runner(
        {
            "lsof -nP -iTCP:8880": CommandResult(0, "p99\ncpython\n"),
            "ps -o comm= -p 99": CommandResult(0, "/Users/u/.voiced/bin/python3.12\n"),
        }
    )
    assert doctor.check_port("tts http", 8880, ours, Path("/Users/u/.voiced")).result == "pass"
    assert doctor.check_port("x", 1, make_runner({})).result == "warn"


def test_check_ports_uses_all_four(settings: Settings) -> None:
    seen: list[str] = []

    def runner(argv: Sequence[str]) -> CommandResult:
        seen.append(argv[2])
        return CommandResult(1, "")

    checks = doctor.check_ports(settings, runner)
    assert [c.result for c in checks] == ["pass"] * 4
    assert seen == ["-iTCP:8881", "-iTCP:10300", "-iTCP:8880", "-iTCP:10200"]


def test_filevault() -> None:
    on = make_runner({"fdesetup status": CommandResult(0, "FileVault is On.\n")})
    check = doctor.check_filevault(on)
    assert check.result == "warn" and "unlock" in check.detail
    off = make_runner({"fdesetup status": CommandResult(0, "FileVault is Off.\n")})
    assert doctor.check_filevault(off).result == "pass"


def test_daemon_states() -> None:
    label = "io.github.julianbeaulieu.voiced.stt"
    loaded = make_runner(
        {"launchctl print": CommandResult(0, "system/x = {\n\tstate = running\n\tpid = 5\n}")}
    )
    not_loaded = make_runner({"launchctl print": CommandResult(113, "", "Could not find")})
    assert doctor.check_daemon("stt", label, loaded, lambda p: True) == doctor.Check(
        "Daemon stt", "pass", "running, pid 5"
    )
    assert doctor.check_daemon("stt", label, not_loaded, lambda p: True).result == "fail"
    assert doctor.check_daemon("stt", label, not_loaded, lambda p: False).result == "warn"
    crashed = make_runner(
        {
            "launchctl print": CommandResult(
                0, "system/x = {\n\tstate = waiting\n\tlast exit code = 1\n}"
            )
        }
    )
    check = doctor.check_daemon("stt", label, crashed, lambda p: True)
    assert check.result == "pass" and "last exit 1" in check.detail


def test_metal_shell() -> None:
    gpu = make_runner({"/v/bin/python": CommandResult(0, "Device(gpu, 0)\n")})
    assert doctor.check_metal_shell(gpu, Path("/v/bin/python")).result == "pass"
    no_mlx = make_runner(
        {"/v/bin/python": CommandResult(1, "", "ModuleNotFoundError: No module named 'mlx'")}
    )
    assert doctor.check_metal_shell(no_mlx, Path("/v/bin/python")).result == "warn"
    cpu = make_runner({"/v/bin/python": CommandResult(0, "Device(cpu, 0)\n")})
    assert doctor.check_metal_shell(cpu, Path("/v/bin/python")).result == "fail"
    assert doctor.check_metal_shell(gpu, None).result == "warn"


def test_metal_daemon_from_running_service() -> None:
    health = [("tts", Observation("ok", "", 1, {"model_loaded": True, "engine": "kokoro"}))]
    check = doctor.check_metal_daemon(Path("/nonexistent"), health, lambda p: None)
    assert check.result == "pass" and "kokoro" in check.detail
    fake = [("tts", Observation("ok", "", 1, {"model_loaded": True, "engine": "fake"}))]
    assert doctor.check_metal_daemon(Path("/nonexistent"), fake, lambda p: None).result == "warn"


def test_metal_daemon_from_probe_file() -> None:
    good = json.dumps(
        {
            "ok": True,
            "default_device": "Device(gpu, 0)",
            "matmul_ms": 12.0,
            "context": {"launchd_managername": "System"},
        }
    )
    check = doctor.check_metal_daemon(Path("/p.json"), [], lambda p: good)
    assert check.result == "pass" and "System" in check.detail
    bad = json.dumps({"ok": False, "error": "no device", "context": {}})
    assert doctor.check_metal_daemon(Path("/p.json"), [], lambda p: bad).result == "fail"
    assert doctor.check_metal_daemon(Path("/p.json"), [], lambda p: "{").result == "warn"


def test_render_and_worst() -> None:
    checks = [
        doctor.Check("A", "pass", "fine"),
        doctor.Check("Long name", "warn", "hmm"),
    ]
    text = doctor.render_checks(checks)
    lines = text.splitlines()
    assert lines[0].startswith("CHECK      RESULT  DETAIL")
    assert lines[1] == "A          pass    fine"
    assert lines[2] == "Long name  warn    hmm"
    assert doctor.worst(checks) == "warn"
    assert doctor.worst([*checks, doctor.Check("B", "fail")]) == "fail"
    assert doctor.worst([checks[0]]) == "pass"
