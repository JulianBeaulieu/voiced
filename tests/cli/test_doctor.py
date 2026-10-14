"""Each doctor check with an injected command runner."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from voiced import doctor
from voiced.config import Settings
from voiced.engines.kokoro import ENV_ESPEAK_LIBRARY
from voiced.launchd.control import CommandResult
from voiced.watchdog import Observation


def make_runner(table: dict[str, CommandResult]) -> doctor.Runner:
    """Match on a command prefix, longest key first."""

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
    assert doctor.check_apple_silicon(runner, machine="x86_64").result == "fail"
    rosetta = make_runner(
        {
            "sysctl -n machdep.cpu.brand_string": CommandResult(0, "Apple M1 Max\n"),
            "sysctl -n sysctl.proc_translated": CommandResult(0, "1\n"),
        }
    )
    assert "Rosetta" in doctor.check_apple_silicon(rosetta, machine="x86_64").detail


def test_macos_version() -> None:
    def check(version: str) -> str:
        runner = make_runner({"sw_vers": CommandResult(0, version + "\n")})
        return doctor.check_macos_version(runner, system="Darwin").result

    assert check("15.7.4") == "pass"
    assert check("13.6") == "warn"
    assert check("12.1") == "fail"
    assert doctor.check_macos_version(make_runner({}), system="Linux").result == "fail"


def test_python_version() -> None:
    assert doctor.check_python((3, 12, 4)).result == "pass"
    assert doctor.check_python((3, 10, 0)).result == "fail"
    assert doctor.check_python((3, 14, 0)).result == "fail"


KEG = "/opt/homebrew/opt/espeak-ng"


def test_espeak_unlinked_keg_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The keg is what the engine loads, linked or not; no binary on PATH needed."""
    monkeypatch.delenv(ENV_ESPEAK_LIBRARY, raising=False)
    keg_lib = f"{KEG}/lib/libespeak-ng.dylib"
    monkeypatch.setattr(Path, "is_file", lambda self: str(self) == keg_lib)
    monkeypatch.setattr("shutil.which", lambda name: None)
    check = doctor.check_espeak()
    assert check.result == "pass"
    assert check.detail == f"{keg_lib} (Homebrew), data {KEG}/share/espeak-ng-data"


def test_espeak_binary_on_path_without_library_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """An espeak-ng binary the engine cannot load is not enough."""
    monkeypatch.delenv(ENV_ESPEAK_LIBRARY, raising=False)
    monkeypatch.setattr(Path, "is_file", lambda self: False)
    monkeypatch.setattr("shutil.which", lambda name: f"/opt/homebrew/bin/{name}")
    check = doctor.check_espeak()
    assert check.result == "fail"
    assert "brew install espeak-ng" in check.detail and ENV_ESPEAK_LIBRARY in check.detail
    assert doctor.check_espeak(required=False).result == "warn"


def test_espeak_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lib = tmp_path / "libespeak-ng.dylib"
    lib.write_bytes(b"")
    monkeypatch.setenv(ENV_ESPEAK_LIBRARY, str(lib))
    check = doctor.check_espeak()
    assert check == doctor.Check("espeak-ng", "pass", f"{lib} ({ENV_ESPEAK_LIBRARY})")
    monkeypatch.setenv(ENV_ESPEAK_LIBRARY, str(tmp_path / "missing.dylib"))
    broken = doctor.check_espeak()
    assert broken.result == "fail" and "missing.dylib" in broken.detail


def test_espeak_required_follows_tts_engine(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bool] = []

    def fake(required: bool = True) -> doctor.Check:
        seen.append(required)
        return doctor.Check("espeak-ng", "pass")

    monkeypatch.setattr(doctor, "check_espeak", fake)
    monkeypatch.setattr(doctor, "daemon_evidence", lambda s, r: [])
    runner = make_runner({})
    settings.tts.engine = "kokoro"
    doctor.run_all(settings, runner)
    settings.tts.engine = "soprano"
    doctor.run_all(settings, runner)
    assert seen == [True, False]


def test_external_programs() -> None:
    no_ffmpeg = doctor.check_ffmpeg(lambda n: None)
    assert no_ffmpeg.result == "fail" and "brew install ffmpeg" in no_ffmpeg.detail
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
            "lsof -nP -t -iTCP:8880": CommandResult(0, "123\n"),
            "ps -o comm= -p 123": CommandResult(0, "/usr/local/bin/node\n"),
        }
    )
    check = doctor.check_port("tts http", 8880, busy, Path("/Users/u/.voiced"))
    assert check.result == "warn" and "node" in check.detail and "pid 123" in check.detail
    ours = make_runner(
        {
            "lsof -nP -t -iTCP:8880": CommandResult(0, "99\n"),
            "ps -o comm= -p 99": CommandResult(0, "/Users/u/.voiced/bin/python3.12\n"),
        }
    )
    assert doctor.check_port("tts http", 8880, ours, Path("/Users/u/.voiced")).result == "pass"
    assert doctor.check_port("x", 1, make_runner({})).result == "warn"  # no lsof


def test_check_ports_uses_all_four(settings: Settings) -> None:
    seen: list[str] = []

    def runner(argv: Sequence[str]) -> CommandResult:
        seen.append(argv[3])
        return CommandResult(1, "")

    checks = doctor.check_ports(settings, runner)
    assert [c.result for c in checks] == ["pass"] * 4
    # The shared fixture moves the Wyoming ports to free ones; compare with it.
    stt_wyoming, tts_wyoming = settings.stt.wyoming_port, settings.tts.wyoming_port
    assert seen == ["-iTCP:8881", f"-iTCP:{stt_wyoming}", "-iTCP:8880", f"-iTCP:{tts_wyoming}"]


def test_filevault() -> None:
    on = make_runner({"fdesetup status": CommandResult(0, "FileVault is On.\n")})
    check = doctor.check_filevault(on)
    assert check.result == "warn" and "unlock" in check.detail
    off = make_runner({"fdesetup status": CommandResult(0, "FileVault is Off.\n")})
    assert doctor.check_filevault(off).result == "pass"


def test_daemon_states(settings: Settings) -> None:
    label = settings.launchd.label("stt")
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
    assert [c.name for c in doctor.check_daemons(settings, not_loaded, lambda p: False)] == [
        "Daemon stt",
        "Daemon tts",
        "Daemon watchdog",
    ]


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


def _loaded(engine: str = "kokoro") -> Observation:
    return Observation("ok", "", 1, {"model_loaded": True, "engine": engine})


def _no_file(path: Path) -> str | None:
    return None


def test_metal_daemon_from_running_service_requires_matching_pid() -> None:
    good = [doctor.DaemonEvidence("tts", _loaded(), launchd_pid=42, listener_pids=[42])]
    check = doctor.check_metal_daemon(Path("/none"), good, _no_file)
    assert check.result == "pass" and "kokoro" in check.detail and "pid 42" in check.detail

    rejected = [
        # a `voiced serve` started by hand owns the port, not the daemon
        doctor.DaemonEvidence("tts", _loaded(), launchd_pid=42, listener_pids=[777]),
        # daemon not running at all
        doctor.DaemonEvidence("tts", _loaded(), launchd_pid=None, listener_pids=[777]),
        # lsof unavailable: cannot prove who listens
        doctor.DaemonEvidence("tts", _loaded(), launchd_pid=42, listener_pids=None),
        # fake engine never proves Metal
        doctor.DaemonEvidence("tts", _loaded("fake"), launchd_pid=42, listener_pids=[42]),
        # model not loaded yet
        doctor.DaemonEvidence(
            "tts", Observation("starting", "", 1, {"model_loaded": False}), 42, [42]
        ),
    ]
    for item in rejected:
        assert doctor.check_metal_daemon(Path("/none"), [item], _no_file).result == "warn", item


def _probe(**overrides: object) -> str:
    data: dict[str, object] = {
        "ok": True,
        "default_device": "Device(gpu, 0)",
        "matmul_ms": 12.0,
        "context": {"launchd_managername": "System", "ppid": 1, "macos": "15.7.4"},
    }
    data.update(overrides)
    return json.dumps(data)


def test_metal_daemon_from_probe_file_validates_context() -> None:
    def run(text: str, macos: str | None = "15.7.4") -> doctor.Check:
        return doctor.check_metal_daemon(Path("/p.json"), [], lambda p: text, macos)

    good = run(_probe())
    assert good.result == "pass" and "12.0 ms" in good.detail

    shell = run(_probe(context={"launchd_managername": "Background", "ppid": 500}))
    assert shell.result == "warn" and "not produced by a LaunchDaemon" in shell.detail

    not_pid1 = run(_probe(context={"launchd_managername": "System", "ppid": 300}))
    assert not_pid1.result == "warn"

    other_os = run(_probe(), macos="26.0.1")
    assert other_os.result == "warn" and "re-run" in other_os.detail

    failed = run(_probe(ok=False, error="no device"))
    assert failed.result == "fail" and "no device" in failed.detail

    assert run("{").result == "warn"
    assert doctor.check_metal_daemon(Path("/p.json"), [], _no_file).result == "warn"


def test_render_and_worst() -> None:
    checks = [
        doctor.Check("A", "pass", "fine"),
        doctor.Check("Long name", "warn", "hmm"),
    ]
    lines = doctor.render_checks(checks).splitlines()
    assert lines[0].startswith("CHECK      RESULT  DETAIL")
    assert lines[1] == "A          pass    fine"
    assert lines[2] == "Long name  warn    hmm"
    assert doctor.worst(checks) == "warn"
    assert doctor.worst([*checks, doctor.Check("B", "fail")]) == "fail"
    assert doctor.worst([checks[0]]) == "pass"
