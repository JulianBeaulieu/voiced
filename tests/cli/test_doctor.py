"""Each doctor check with an injected command runner."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from voiced import doctor
from voiced.config import SERVICES, Settings
from voiced.engines import kokoro
from voiced.engines.kokoro import ENV_ESPEAK_LIBRARY
from voiced.launchd import control
from voiced.launchd.control import CommandResult
from voiced.launchd.install import plan_install
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


def _keg(root: Path) -> Path:
    """A Homebrew espeak-ng keg under ``root``: the library and its data directory."""
    (root / "lib").mkdir(parents=True)
    (root / "lib" / "libespeak-ng.dylib").write_bytes(b"")
    (root / "share" / "espeak-ng-data").mkdir(parents=True)
    return root


@pytest.fixture
def no_wheel(monkeypatch: pytest.MonkeyPatch) -> None:
    """This environment has no espeakng-loader wheel."""
    monkeypatch.setattr(kokoro, "_bundled_espeak", lambda: None)


def _wheel(monkeypatch: pytest.MonkeyPatch, root: Path, data: Path) -> Path:
    """An espeakng-loader wheel with its library under ``root`` and its data at ``data``."""
    library = root / "espeakng_loader" / "libespeak-ng.dylib"
    library.parent.mkdir(parents=True)
    library.write_bytes(b"")
    wheel = kokoro.EspeakCandidate(str(library), str(data), "wheel", "0.2.4")
    monkeypatch.setattr(kokoro, "_bundled_espeak", lambda: wheel)
    return library


def test_espeak_wheel_passes_without_homebrew(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The wheel's library is what Kokoro tries first, so it passes with no
    keg, and the data named is the short clone load would use (not made here)."""
    library = _wheel(monkeypatch, tmp_path, tmp_path / "data")
    monkeypatch.setattr(kokoro, "_user_cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(kokoro, "HOMEBREW_PREFIXES", (str(tmp_path / "absent"),))
    check = doctor.check_espeak()
    clone = tmp_path / "cache" / "voiced" / "espeak-ng-data-0.2.4"
    assert check == doctor.Check(
        "espeak-ng", "pass", f"found {library} (espeakng-loader wheel), data {clone}"
    )
    assert not clone.exists()


def test_espeak_wheel_load_would_refuse_is_not_a_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No user cache directory and a wheel at a path longer than espeak-ng
    accepts: load skips it, so doctor reports the keg, or fails without one."""
    long = tmp_path / ("d" * 80) / ("d" * 80) / "espeak-ng-data"
    library = _wheel(monkeypatch, tmp_path, long)
    monkeypatch.setattr(kokoro, "_user_cache_dir", lambda: None)
    monkeypatch.setattr(kokoro, "HOMEBREW_PREFIXES", (str(tmp_path / "absent"),))
    check = doctor.check_espeak()
    assert check.result == "fail"
    assert f"{library}: espeak-ng data path" in check.detail
    assert check.detail.endswith(kokoro.ESPEAK_REMEDY)
    keg = _keg(tmp_path / "opt" / "espeak-ng")
    monkeypatch.setattr(kokoro, "HOMEBREW_PREFIXES", (str(keg),))
    check = doctor.check_espeak()
    assert check.result == "pass"
    assert check.detail.startswith(f"found {keg}/lib/libespeak-ng.dylib (Homebrew)")
    assert f"; skipped {library}: espeak-ng data path" in check.detail


@pytest.mark.usefixtures("no_wheel")
def test_espeak_unlinked_keg_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Without the wheel the engine loads the keg under opt/, linked or not, so
    that is what passes. Whether an ``espeak-ng`` binary is on PATH is not looked at."""
    keg = _keg(tmp_path / "opt" / "espeak-ng")
    monkeypatch.setattr(kokoro, "HOMEBREW_PREFIXES", (str(tmp_path / "absent"), str(keg)))
    check = doctor.check_espeak()
    assert check == doctor.Check(
        "espeak-ng",
        "pass",
        f"found {keg}/lib/libespeak-ng.dylib (Homebrew), data {keg}/share/espeak-ng-data",
    )


@pytest.mark.usefixtures("no_wheel")
def test_espeak_missing_library(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(kokoro, "HOMEBREW_PREFIXES", (str(tmp_path / "absent"),))
    check = doctor.check_espeak()
    assert check.result == "fail"
    assert "brew install espeak-ng" in check.detail and "[tts] espeak_library" in check.detail
    assert doctor.check_espeak(required=False).result == "warn"


@pytest.mark.usefixtures("no_wheel")
def test_espeak_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    lib = tmp_path / "libespeak-ng.dylib"
    lib.write_bytes(b"")
    monkeypatch.setenv(ENV_ESPEAK_LIBRARY, str(lib))
    check = doctor.check_espeak()
    assert check == doctor.Check("espeak-ng", "pass", f"found {lib} ([tts] espeak_library)")


def test_espeak_stale_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A stale override breaks Kokoro, but only Kokoro: warn when it is not the engine."""
    monkeypatch.setenv(ENV_ESPEAK_LIBRARY, str(tmp_path / "missing.dylib"))
    broken = doctor.check_espeak()
    assert broken.result == "fail" and "missing.dylib" in broken.detail
    relaxed = doctor.check_espeak(required=False)
    assert relaxed.result == "warn" and relaxed.detail == broken.detail


def test_espeak_required_follows_tts_engine(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bool] = []

    def fake(required: bool = True) -> doctor.Check:
        seen.append(required)
        return doctor.Check("espeak-ng", "pass")

    monkeypatch.setattr(doctor, "check_espeak", fake)
    monkeypatch.setattr(doctor, "agent_evidence", lambda s, r: [])
    runner = make_runner({})
    settings.tts.engine = "kokoro"
    doctor.run_all(settings, runner)
    settings.tts.engine = "soprano"
    doctor.run_all(settings, runner)
    assert seen == [True, False]


PROTOCOLS = "Supported file protocols:\nInput:\n  {inputs}\nOutput:\n  fd\n  file\n  pipe\n"


def test_external_programs() -> None:
    no_ffmpeg = doctor.check_ffmpeg(lambda: None)
    assert no_ffmpeg.result == "fail" and "reinstall voiced" in no_ffmpeg.detail
    assert "brew" not in no_ffmpeg.detail

    def protocols(inputs: str) -> Callable[[Sequence[str]], CommandResult]:
        return lambda argv: CommandResult(0, PROTOCOLS.format(inputs=inputs))

    found = lambda: "/usr/local/bin/ffmpeg"  # noqa: E731
    ok = doctor.check_ffmpeg(found, protocols("fd\n  file\n  pipe"))
    assert ok.result == "pass" and ok.detail == "/usr/local/bin/ffmpeg (PATH)"
    old = doctor.check_ffmpeg(found, protocols("file\n  pipe"))  # fd only as output
    assert old.result == "fail" and "imageio-ffmpeg" in old.detail and "brew" not in old.detail


def _environment(tmp_path: Path) -> tuple[Path, Path]:
    """A state dir whose bin/voiced links into an environment, as installed."""
    env_bin = tmp_path / "env" / "bin"
    env_bin.mkdir(parents=True)
    (env_bin / "voiced").write_text("#!/bin/sh\n")
    entry = tmp_path / "state" / "bin" / "voiced"
    entry.parent.mkdir(parents=True)
    entry.symlink_to(env_bin / "voiced")
    return entry, env_bin / "python"


def test_service_python_is_the_environment_interpreter(tmp_path: Path) -> None:
    missing = doctor.check_service_python(tmp_path / "state" / "bin" / "voiced")
    assert missing.result == "warn" and "voiced install" in missing.detail
    entry, python = _environment(tmp_path)
    python.write_text("")
    seen: list[str] = []

    def runner(argv: Sequence[str]) -> CommandResult:
        seen.append(argv[0])
        return CommandResult(0, "3.12.15\n")

    ok = doctor.check_service_python(entry, runner)
    assert ok.result == "pass" and "3.12.15" in ok.detail and str(python) in ok.detail
    assert seen == [str(python)]  # next to the resolved console script, not resolved
    old = doctor.check_service_python(entry, lambda argv: CommandResult(0, "3.10.18\n"))
    assert old.result == "fail"


def test_service_python_reports_a_dangling_entry_point(tmp_path: Path) -> None:
    """`brew uninstall voiced` with the daemons installed leaves the entry
    point pointing nowhere: a failure, not "not installed yet"."""
    entry, _ = _environment(tmp_path)
    (tmp_path / "env" / "bin" / "voiced").unlink()
    check = doctor.check_service_python(entry)
    assert check.result == "fail" and "no longer exists" in check.detail


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


FRAMEWORK_PYTHON = (
    "/opt/homebrew/Cellar/python@3.12/3.12.15/Frameworks/Python.framework/Versions/3.12"
    "/Resources/Python.app/Contents/MacOS/Python"
)


def test_ports() -> None:
    free = make_runner({"lsof": CommandResult(1, "")})
    assert doctor.check_port("tts http", 8880, free).result == "pass"
    entry = Path("/Users/u/.voiced/bin/voiced")

    def check(*argv: str) -> doctor.Check:
        runner = make_runner({"lsof -nP -t -iTCP:8880": CommandResult(0, "77\n")})
        name = Path(argv[0]).name
        return doctor.check_port("tts http", 8880, runner, entry, lambda pid: (name, list(argv)))

    busy = check("/usr/local/bin/node", "server.js")
    assert busy.result == "warn" and "node" in busy.detail and "pid 77" in busy.detail
    # A source build: argv[0] is the environment's python, argv[1] the entry point.
    assert check("/Users/u/.voiced/envs/x/bin/python", str(entry), "serve").result == "pass"
    # Homebrew's Python is a framework build: the executable is in Python.app.
    assert check(FRAMEWORK_PYTHON, str(entry), "serve", "tts").result == "pass"
    # The entry point anywhere but argv[1] is not voiced's daemon.
    assert check("/usr/bin/python3", "tool.py", str(entry), "serve").result == "warn"
    assert check("/usr/bin/python3", f"/tmp{entry}", "serve").result == "warn"
    assert doctor.check_port("x", 1, make_runner({})).result == "warn"  # no lsof


def test_port_listener_on_another_environment(tmp_path: Path) -> None:
    """A daemon not restarted after a switch still runs the old
    environment's files: doctor says so."""
    new, old = tmp_path / "envs" / "new", tmp_path / "envs" / "old"
    (new / "bin").mkdir(parents=True)
    (new / "bin" / "voiced").write_text("")
    entry = tmp_path / "bin" / "voiced"
    entry.parent.mkdir()
    entry.symlink_to(new / "bin" / "voiced")
    argv = ["/x/Python", str(entry), "serve", "tts"]

    def check(mapped: Path) -> doctor.Check:
        site = mapped.resolve() / "lib/python3.12/site-packages/x.so"
        runner = make_runner(
            {
                "lsof -nP -t -iTCP:8880": CommandResult(0, "77\n"),
                "lsof -p 77 -Fn": CommandResult(0, f"p77\nfcwd\nn/\nftxt\nn{site}\n"),
            }
        )
        return doctor.check_port("tts http", 8880, runner, entry, lambda pid: ("Python", argv))

    assert check(new).result == "pass"
    stale = check(old)
    assert stale.result == "warn" and "voiced restart all" in stale.detail


def test_install_row(tmp_path: Path) -> None:
    from voiced.layout import Manifest, link_entry_point, sha256_file, write_manifest

    state = tmp_path / "state"
    assert doctor.check_install(state).result == "warn"  # nothing installed
    env = tmp_path / "env"
    lock = env / "lib/python3.12/site-packages/voiced/requirements.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("a==1\n")
    (env / "bin").mkdir()
    (env / "bin" / "voiced").write_text("")
    link_entry_point(state, env)
    manifest = Manifest("app", env, "0.1.0", sha256_file(lock))
    write_manifest(state, manifest)
    check = doctor.check_install(state)
    assert check.result == "pass" and "app" in check.detail and "0.1.0" in check.detail
    lock.write_text("a==2\n")
    assert "no longer matches" in doctor.check_install(state).detail
    link_entry_point(state, tmp_path / "other")
    assert "runs" in doctor.check_install(state).detail


def test_install_row_for_homebrew(tmp_path: Path) -> None:
    """Homebrew's opt/voiced: `brew upgrade` changes the lock and the version
    under the manifest by design, which needs no `voiced install`."""
    from voiced.layout import Manifest, link_entry_point, write_manifest

    tmp_path = tmp_path.resolve()
    keg = tmp_path / "Cellar" / "voiced" / "0.2.0" / "libexec"
    lock = keg / "lib/python3.12/site-packages/voiced/requirements.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("a==2\n")
    (keg / "lib/python3.12/site-packages/voiced-0.2.0.dist-info").mkdir()
    (keg / "bin").mkdir()
    (keg / "bin" / "voiced").write_text("")
    (tmp_path / "opt").mkdir()
    (tmp_path / "opt" / "voiced").symlink_to(keg.parent)
    env = tmp_path / "opt" / "voiced" / "libexec"
    state = tmp_path / "state"
    link_entry_point(state, env)
    write_manifest(state, Manifest("homebrew", env, "0.1.0", "0" * 64))
    check = doctor.check_install(state)
    assert check.result == "pass"
    assert "voiced 0.2.0" in check.detail and "upgraded in place" in check.detail
    # The same environment from a source install: a changed lock is news.
    write_manifest(state, Manifest("cli", env, "0.1.0", "0" * 64))
    assert doctor.check_install(state).result == "warn"


def test_check_ports_uses_all_four(settings: Settings) -> None:
    seen: list[str] = []

    def runner(argv: Sequence[str]) -> CommandResult:
        seen.append(argv[3])
        return CommandResult(1, "")

    checks = doctor.check_ports(settings, runner)
    assert [c.result for c in checks] == ["pass"] * 4
    # The shared fixture moves every port to a free one; compare with it.
    stt, tts = settings.stt, settings.tts
    ports = [stt.http_port, stt.wyoming_port, tts.http_port, tts.wyoming_port]
    assert seen == [f"-iTCP:{port}" for port in ports]
    assert [c.name for c in checks] == [
        f"Port {port} ({key})" for key, port in settings.ports().items()
    ]


def test_the_login_session_is_reported_as_install_refuses_it() -> None:
    gui = f"launchctl print {control.domain()}"
    check = doctor.check_session(make_runner({gui: CommandResult(0)}), "someone")
    assert (check.result, check.detail) == ("pass", "someone is logged in at this Mac")
    logged_out = make_runner({gui: CommandResult(113)})
    check = doctor.check_session(logged_out, "someone")
    assert (check.result, check.detail) == ("fail", control.session_refusal("someone", logged_out))


def test_restart_after_a_reboot() -> None:
    """The agents start when the user logs in: FileVault's unlock at boot is
    that login, otherwise automatic login is (ADR 0035)."""
    on = make_runner({"fdesetup status": CommandResult(0, "FileVault is On.\n")})
    check = doctor.check_restart(on, "someone")
    assert check.result == "pass" and "when someone unlocks the disk" in check.detail
    off = {"fdesetup status": CommandResult(0, "FileVault is Off.\n")}
    auto = make_runner({**off, "defaults read": CommandResult(0, "someone\n")})
    check = doctor.check_restart(auto, "someone")
    assert check.result == "pass" and "automatic login" in check.detail
    other = make_runner({**off, "defaults read": CommandResult(0, "else\n")})
    check = doctor.check_restart(other, "someone")
    assert check.result == "warn" and "once you log in" in check.detail
    assert doctor.check_restart(make_runner({**off}), "someone").result == "warn"


def _check_agent(
    settings: Settings, runner: doctor.Runner, installed: bytes | None = b"plist"
) -> doctor.Check:
    """The ``stt`` agent, its plist on disk ``installed`` and as expected ``plist``."""
    label = settings.launchd.label("stt")
    return doctor.check_agent("stt", label, runner, installed, b"plist", settings.server.log_dir)


def test_agent_states(settings: Settings) -> None:
    loaded = make_runner(
        {"launchctl print": CommandResult(0, "gui/501/x = {\n\tstate = running\n\tpid = 5\n}")}
    )
    not_loaded = make_runner({"launchctl print": CommandResult(113, "", "Could not find")})
    assert _check_agent(settings, loaded) == doctor.Check("Agent stt", "pass", "running, pid 5")
    check = _check_agent(settings, not_loaded)
    assert check.result == "fail" and "voiced start stt" in check.detail
    assert _check_agent(settings, not_loaded, None).result == "warn"
    crashed = make_runner(
        {
            "launchctl print": CommandResult(
                0, "gui/501/x = {\n\tstate = waiting\n\tlast exit code = 1\n}"
            )
        }
    )
    check = _check_agent(settings, crashed)
    assert check.result == "warn" and "last exit 1" in check.detail
    assert "not running" in check.detail and "`voiced start stt` reload it" in check.detail
    assert [c.name for c in doctor.check_agents(settings, not_loaded, lambda p: None)] == [
        "Agent stt",
        "Agent tts",
        "Agent watchdog",
    ]


def test_an_agent_with_an_out_of_date_plist_says_to_install(settings: Settings) -> None:
    """A formula or uv tool upgrade keeps the earlier release's plist, and a
    log_dir changed after install is not in it yet."""
    running = make_runner(
        {"launchctl print": CommandResult(0, "gui/501/x = {\n\tstate = running\n\tpid = 5\n}")}
    )
    check = _check_agent(settings, running, b"old plist")
    assert check.result == "warn"
    assert "out of date" in check.detail and "run `voiced install`" in check.detail


def test_check_agents_compares_with_what_install_writes(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    running = make_runner(
        {"launchctl print": CommandResult(0, "gui/501/x = {\n\tstate = running\n\tpid = 5\n}")}
    )
    plan = plan_install(settings, home=tmp_path, uv=None)
    on_disk = {plan.agent_path(s): plan.agent_plist(s) for s in SERVICES}
    current = doctor.check_agents(settings, running, on_disk.get)
    assert [c.result for c in current] == ["pass"] * 3, current
    settings.server.log_dir = tmp_path / "moved"
    moved = doctor.check_agents(settings, running, on_disk.get)
    assert [c.result for c in moved] == ["warn"] * 3


def test_an_agent_launchd_cannot_open_the_logs_for_fails(settings: Settings) -> None:
    """launchd does not start a job whose StandardOutPath or StandardErrorPath
    it cannot open, and says so with exit code 78."""
    refused = make_runner(
        {
            "launchctl print": CommandResult(
                0, "gui/501/x = {\n\tstate = not running\n\tlast exit code = 78: EX_CONFIG\n}"
            )
        }
    )
    check = _check_agent(settings, refused)
    assert check.result == "fail"
    assert str(settings.server.log_dir / "stt.stderr.log") in check.detail
    assert "[server] log_dir" in check.detail and "`voiced start stt`" in check.detail


def test_old_launchdaemons_fail_with_their_removal(settings: Settings, no_launchd: Path) -> None:
    """By plist, under the default prefix whatever the config says (the old
    root step installed no other), and by `launchctl print system/<label>`
    for one whose plist is gone while it stays loaded."""
    import plistlib

    settings.launchd.label_prefix = "org.example.voicedtest"
    nothing = make_runner({})
    assert doctor.check_legacy_daemons(settings, nothing, "someone") == doctor.Check(
        "Old LaunchDaemons", "pass", "none"
    )
    watchdog = "io.github.julianbeaulieu.voiced.watchdog"
    (no_launchd / f"{watchdog}.plist").write_bytes(plistlib.dumps({"UserName": "someone"}))
    check = doctor.check_legacy_daemons(settings, nothing, "someone")
    assert check.result == "fail"
    assert check.detail.startswith(f"{watchdog}: voiced still runs as LaunchDaemons")
    assert f"/bin/launchctl bootout system/{watchdog}" in check.detail
    assert "`; then run `voiced install`. " in check.detail
    assert check.detail.endswith("an SSH login is not enough.")
    (no_launchd / f"{watchdog}.plist").unlink()
    stt = "io.github.julianbeaulieu.voiced.stt"
    loaded = make_runner({f"launchctl print system/{stt}": CommandResult(0, "system/x = {}")})
    check = doctor.check_legacy_daemons(settings, loaded, "someone")
    assert check.result == "fail" and check.detail.startswith(f"{stt}:")


def test_firewall_on_the_network() -> None:
    """Checked only with bind = lan. voiced adds no exception: macOS asks when
    a service starts listening, and again after the Python changes."""
    fw = "/usr/libexec/ApplicationFirewall/socketfilterfw"
    on = make_runner(
        {f"{fw} --getglobalstate": CommandResult(0, "Firewall is enabled. (State = 1)\n")}
    )
    off = make_runner(
        {f"{fw} --getglobalstate": CommandResult(0, "Firewall is disabled. (State = 0)")}
    )
    assert doctor.check_firewall(on, False) is None
    assert doctor.check_firewall(off, True) == doctor.Check("Firewall", "pass", "off")
    check = doctor.check_firewall(on, True)
    assert check is not None and check.result == "warn"
    assert "asks again after an update that changes that Python" in check.detail
    assert "System Settings > Network > Firewall > Options" in check.detail


def test_discovery_needs_the_network_not_only_this_mac(settings: Settings) -> None:
    """Bonjour from a LaunchAgent reached only loopback when measured (ADR
    0035): seen there only, Home Assistant cannot discover voiced."""
    assert doctor.check_discovery(settings, lambda: {}) is None  # loopback bind
    settings.server.bind = "0.0.0.0"
    both = {"voiced-stt": {1, 14}, "voiced-tts": {14}}
    assert doctor.check_discovery(settings, lambda: both) == doctor.Check(
        "Discovery", "pass", "voiced-stt, voiced-tts advertised on the network"
    )
    check = doctor.check_discovery(settings, lambda: {"voiced-stt": {1}, "voiced-tts": {1}})
    assert check is not None and check.result == "warn"
    assert "Local Network" in check.detail and "address and port" in check.detail
    settings.tts.enabled = False
    assert doctor.check_discovery(settings, lambda: {"voiced-stt": {14}}).result == "pass"  # type: ignore[union-attr]
    check = doctor.check_discovery(settings, lambda: None)
    assert check is not None and check.result == "warn"
    settings.server.zeroconf = False
    assert doctor.check_discovery(settings, lambda: {}) is None


def test_browse_reads_dns_sd(monkeypatch: pytest.MonkeyPatch) -> None:
    """dns-sd's columns: the interface index is the fourth."""
    text = (
        "Timestamp     A/R    Flags  if Domain               Service Type         Instance Name\n"
        "17:44:38.263  Add        3   1 local.               _wyoming._tcp.       voiced-stt\n"
        "17:44:38.263  Add        2  14 local.               _wyoming._tcp.       voiced-stt\n"
        "17:44:39.000  Rmv        0  14 local.               _wyoming._tcp.       old one\n"
    )
    import os as os_module

    reads = [text.encode()]
    monkeypatch.setattr("pty.fork", lambda: (4242, 99))
    monkeypatch.setattr("select.select", lambda r, w, x, t: (r, [], []))
    monkeypatch.setattr(
        os_module, "read", lambda fd, n: reads.pop() if reads else (_ for _ in ()).throw(OSError())
    )
    monkeypatch.setattr(os_module, "kill", lambda pid, sig: None)
    monkeypatch.setattr(os_module, "waitpid", lambda pid, flags: (pid, 0))
    monkeypatch.setattr(os_module, "close", lambda fd: None)
    assert doctor.browse_wyoming(1.0) == {"voiced-stt": {1, 14}}


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


def test_metal_agent_from_running_service_requires_matching_pid() -> None:
    good = [doctor.AgentEvidence("tts", _loaded(), launchd_pid=42, listener_pids=[42])]
    check = doctor.check_metal_agent(Path("/none"), good, _no_file)
    assert check.result == "pass" and "kokoro" in check.detail and "pid 42" in check.detail

    rejected = [
        # a `voiced serve` started by hand owns the port, not the agent
        doctor.AgentEvidence("tts", _loaded(), launchd_pid=42, listener_pids=[777]),
        # agent not running at all
        doctor.AgentEvidence("tts", _loaded(), launchd_pid=None, listener_pids=[777]),
        # lsof unavailable: cannot prove who listens
        doctor.AgentEvidence("tts", _loaded(), launchd_pid=42, listener_pids=None),
        # fake engine never proves Metal
        doctor.AgentEvidence("tts", _loaded("fake"), launchd_pid=42, listener_pids=[42]),
        # model not loaded yet
        doctor.AgentEvidence(
            "tts", Observation("starting", "", 1, {"model_loaded": False}), 42, [42]
        ),
    ]
    for item in rejected:
        assert doctor.check_metal_agent(Path("/none"), [item], _no_file).result == "warn", item


def _probe(**overrides: object) -> str:
    data: dict[str, object] = {
        "ok": True,
        "default_device": "Device(gpu, 0)",
        "matmul_ms": 12.0,
        "context": {"launchd_managername": "Aqua", "ppid": 1, "macos": "15.7.4"},
    }
    data.update(overrides)
    return json.dumps(data)


def test_metal_agent_from_probe_file_validates_context() -> None:
    def run(text: str, macos: str | None = "15.7.4") -> doctor.Check:
        return doctor.check_metal_agent(Path("/p.json"), [], lambda p: text, macos)

    good = run(_probe())
    assert good.result == "pass" and "12.0 ms" in good.detail

    shell = run(_probe(context={"launchd_managername": "Background", "ppid": 500}))
    assert shell.result == "warn" and "not produced by a LaunchAgent" in shell.detail

    not_pid1 = run(_probe(context={"launchd_managername": "Aqua", "ppid": 300}))
    assert not_pid1.result == "warn"

    # A LaunchDaemon's, from before ADR 0035: not how the services run now.
    daemon = run(_probe(context={"launchd_managername": "System", "ppid": 1, "macos": "15.7.4"}))
    assert daemon.result == "warn"

    other_os = run(_probe(), macos="26.0.1")
    assert other_os.result == "warn" and "re-run" in other_os.detail

    failed = run(_probe(ok=False, error="no device"))
    assert failed.result == "fail" and "no device" in failed.detail

    assert run("{").result == "warn"
    assert doctor.check_metal_agent(Path("/p.json"), [], _no_file).result == "warn"


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


def test_service_python_reports_a_broken_link(tmp_path: Path) -> None:
    """`uv python uninstall` leaves the venv's python pointing nowhere and the
    services down: a failure, not "not installed yet"."""
    entry, python = _environment(tmp_path)
    python.symlink_to(tmp_path / "gone" / "python3.12")
    check = doctor.check_service_python(entry)
    assert check.result == "fail" and "broken link" in check.detail


def test_config_file_mode(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    assert doctor.check_config_file(path).result == "pass"
    path.write_text("")
    path.chmod(0o644)
    readable = doctor.check_config_file(path)
    assert readable.result == "warn" and "chmod 600" in readable.detail
    path.chmod(0o600)
    assert doctor.check_config_file(path).result == "pass"


def test_each_services_model_is_downloaded_or_says_what_fetches_it(
    settings: Settings, model_cache: Path
) -> None:
    from tests import hf_cache_support as hf
    from voiced import models

    entry = models.lock()[settings.stt.model]
    hf.add_locked(model_cache, entry)
    settings.tts.model = "someone/own-voice"
    stt, tts = doctor.check_models(settings)
    assert (stt.name, stt.result) == ("STT model", "pass")
    assert stt.detail == f"{entry.repo} downloaded at {entry.revision[:12]}"
    assert (tts.result, tts.detail) == (
        "pass",
        "someone/own-voice (not in voiced's catalogue; not checked)",
    )
    settings.tts.model = "gradium/phonon-31m-en"
    _, tts = doctor.check_models(settings)
    assert tts.result == "fail"
    assert tts.detail == (
        "gradium/phonon-31m-en, gradium/phonon-voices not downloaded; "
        "run voiced models download gradium/phonon-31m-en"
    )


def test_the_model_of_a_service_switched_off_is_not_checked(settings: Settings) -> None:
    settings.stt.enabled = False
    stt, _ = doctor.check_models(settings)
    assert (stt.result, stt.detail) == ("pass", "stt is off; not checked")
