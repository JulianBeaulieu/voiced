"""Lifecycle commands against a fake launchctl and a fake health poller."""

from __future__ import annotations

import argparse
import os
import pwd
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from tests import hf_cache_support as hf
from voiced import cli_lifecycle as lc
from voiced.config import Settings
from voiced.launchd.control import CommandResult
from voiced.models import locked
from voiced.watchdog import Observation

RUNNING = "system/x = {\n\tstate = running\n\tpid = 42\n\tlast exit code = 0\n}"
NOT_LOADED = CommandResult(113, "", "Could not find service")


class FakeLaunchctl:
    """The user's GUI domain; nothing in the system domain. A job booted
    out stays listed for ``linger`` more prints, as launchd tears it down."""

    def __init__(self, loaded: dict[str, bool], linger: int = 0) -> None:
        self.loaded = loaded
        self.linger = linger
        self.unloading: dict[str, int] = {}
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> CommandResult:
        self.calls.append(list(argv))
        target = argv[-1]
        label = target.rsplit("/", 1)[-1]
        if argv[1] == "print":
            if target.startswith("system/"):
                return NOT_LOADED
            if self.unloading.get(label, 0) > 0:
                self.unloading[label] -= 1
                return CommandResult(0, "gui/x = {\n\tstate = exiting\n}")
            return CommandResult(0, RUNNING) if self.loaded.get(label) else NOT_LOADED
        if argv[1] == "bootout":
            self.loaded.pop(label, None)
            self.unloading[label] = self.linger
        return CommandResult(0)


GUI = f"gui/{os.getuid()}"


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Never read the real ~/Library/LaunchAgents or run as root in tests."""
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    monkeypatch.setattr(lc, "plist_path", lambda label: agents / f"{label}.plist")
    monkeypatch.setattr(lc, "is_root", lambda: False)
    return agents


def test_start_kickstarts_loaded_services(settings: Settings) -> None:
    stt = settings.launchd.label("stt")
    fake = FakeLaunchctl({stt: True})
    out: list[str] = []
    assert lc.cmd_start("stt", settings, fake, out.append) == 0
    assert ["launchctl", "kickstart", f"{GUI}/{stt}"] in fake.calls
    assert out == [f"stt: kickstarted {stt}"]


def test_start_loads_an_installed_agent_without_root(settings: Settings, isolated: Path) -> None:
    tts = settings.launchd.label("tts")
    plist = isolated / f"{tts}.plist"
    plist.write_text("x")
    fake = FakeLaunchctl({})
    out: list[str] = []
    assert lc.cmd_start("tts", settings, fake, out.append) == 0
    assert ["launchctl", "bootstrap", GUI, str(plist)] in fake.calls
    assert out == [f"tts: loaded {plist}"]


def test_start_reports_missing_install(settings: Settings) -> None:
    out: list[str] = []
    assert lc.cmd_start("all", settings, FakeLaunchctl({}), out.append) == 1
    assert all("not installed" in line for line in out)


def test_stop_boots_out_without_root(settings: Settings) -> None:
    stt = settings.launchd.label("stt")
    fake = FakeLaunchctl({stt: True})
    out: list[str] = []
    assert lc.cmd_stop("all", settings, fake, out.append) == 0
    assert ["launchctl", "bootout", f"{GUI}/{stt}"] in fake.calls
    assert "stt: stopped until the next login or `voiced start`" in out
    assert stt not in fake.loaded
    assert "tts: not loaded" in out
    assert not any("sudo" in line for line in out)


def test_stop_returns_once_launchd_has_let_go_of_the_job(settings: Settings) -> None:
    """bootout returns while launchd still tears the job down; a `voiced
    start` straight after would kickstart the dying job."""
    stt = settings.launchd.label("stt")
    fake = FakeLaunchctl({stt: True}, linger=3)
    out: list[str] = []
    assert lc.cmd_stop("stt", settings, fake, out.append) == 0
    prints_after = [
        c for c in fake.calls[fake.calls.index(["launchctl", "bootout", f"{GUI}/{stt}"]) :]
    ]
    assert [c[1] for c in prints_after] == ["bootout", "print", "print", "print", "print"]
    assert (
        lc.cmd_start("stt", settings, fake, out.append) == 1
    )  # not installed: nothing to bootstrap
    assert not any(c[1] == "kickstart" for c in fake.calls)


def test_stop_says_a_job_still_unloading(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from voiced.launchd import control

    monkeypatch.setattr(control, "BOOTOUT_WAIT_S", 0.0)
    stt = settings.launchd.label("stt")
    out: list[str] = []
    assert lc.cmd_stop("stt", settings, FakeLaunchctl({stt: True}, linger=10**6), out.append) == 1
    assert out == ["stt: still unloading; try again in a moment"]


@pytest.mark.parametrize("command", ["start", "stop", "restart", "status", "uninstall", "doctor"])
def test_every_launchd_command_refuses_root(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """As root launchctl addresses gui/0: `sudo voiced stop` would find
    nothing loaded and leave the user's services running."""
    from voiced.cli import build_parser

    monkeypatch.setattr(lc, "is_root", lambda: True)
    fake = FakeLaunchctl({settings.launchd.label("stt"): True})
    out: list[str] = []
    args = build_parser().parse_args([command, *(["--yes"] if command == "uninstall" else [])])
    assert lc.run_lifecycle(args, settings, fake, out.append) == 2
    assert out == [
        f"Run `voiced {command}` as the user the services run as, not with sudo: "
        "they are that user's LaunchAgents."
    ]
    assert fake.calls == []


def test_start_and_stop_point_old_launchdaemons_to_their_removal(
    settings: Settings, no_launchd: Path
) -> None:
    """These commands manage the agents only; until the old daemons are
    removed they say how, and touch nothing."""
    import plistlib

    label = settings.launchd.label("stt")
    plist = {"Label": label, "UserName": pwd.getpwuid(os.getuid()).pw_name}
    (no_launchd / f"{label}.plist").write_bytes(plistlib.dumps(plist))
    for command in (lc.cmd_start, lc.cmd_stop):
        fake = FakeLaunchctl({label: True})
        out: list[str] = []
        assert command("all", settings, fake, out.append) == 1
        assert all(c[1] == "print" and c[2].startswith("system/") for c in fake.calls)
        (message,) = out
        assert message.startswith("voiced still runs as LaunchDaemons from 0.4.0 or earlier")
        assert f"\n  sudo /bin/sh -c '/bin/launchctl bootout system/{label} " in message
        assert "\n  voiced install\n\nThe services are down" in message
        assert message.endswith("an SSH login is not enough.")


def test_status_table(settings: Settings) -> None:
    fake = FakeLaunchctl({settings.launchd.label(s): True for s in ("stt", "tts", "watchdog")})

    def poll(url: str) -> Observation:
        if url.startswith(settings.http_url("stt")):
            return Observation(
                "ok",
                "",
                10,
                {
                    "model": "m/stt",
                    "queue_depth": 1,
                    "queue_capacity": 8,
                    "last_inference_ms": 12.3,
                    "last_inference_at": None,
                },
            )
        return Observation("error", "HTTP 404, status=None", None, {"detail": "Not Found"})

    rows = lc.collect_status(settings, fake, poll)
    assert rows[0]["health"] == "ok"
    assert rows[0]["health_detail"] == "m/stt, queue 1/8, last 12 ms never"
    assert rows[1]["health_detail"] == "HTTP 404, status=None"
    assert rows[2]["pid"] == 42 and "health" not in rows[2]

    out: list[str] = []
    assert lc.cmd_status(settings, fake, out.append) == 1  # nothing listens on the test ports
    lines = out[0].splitlines()
    assert lines[0].split() == ["SERVICE", "LAUNCHD", "HEALTH", "DETAIL"]
    assert lines[1].startswith("stt       running (pid 42)")


def test_status_says_to_install_before_the_agents_are_there(settings: Settings) -> None:
    out: list[str] = []
    assert lc.cmd_status(settings, FakeLaunchctl({}), out.append, control=lambda s: None) == 1
    assert out[-1] == "The services are not installed. Run `voiced install`."


def test_logs_tail_and_missing(settings: Settings) -> None:
    settings.server.log_dir.mkdir()
    (settings.server.log_dir / "stt.log").write_text("\n".join(f"line{i}" for i in range(100)))
    args = argparse.Namespace(service="stt", lines=3, follow=False, stderr=False)
    out: list[str] = []
    assert lc.cmd_logs(args, settings, out.append) == 0
    assert out == ["line97", "line98", "line99"]
    args = argparse.Namespace(service="tts", lines=3, follow=False, stderr=True)
    out.clear()
    assert lc.cmd_logs(args, settings, out.append) == 1
    assert "no log files yet" in out[0]
    assert lc.log_files(settings, "all", stderr=True) == [
        settings.server.log_dir / name
        for name in (
            "stt.log",
            "stt.stderr.log",
            "tts.log",
            "tts.stderr.log",
            "watchdog.log",
            "watchdog.stderr.log",
        )
    ]


def test_logs_stderr_names_a_missing_stderr_file(settings: Settings) -> None:
    """After a log_dir change the stderr file stays where the plist says
    until `voiced install`; the empty answer says so."""
    settings.server.log_dir.mkdir()
    (settings.server.log_dir / "stt.log").write_text("line\n")
    args = argparse.Namespace(service="stt", lines=3, follow=False, stderr=True)
    out: list[str] = []
    assert lc.cmd_logs(args, settings, out.append) == 0
    assert out[0] == "line"
    assert "no stt.stderr.log" in out[-1] and "`voiced install`" in out[-1]
    (settings.server.log_dir / "stt.stderr.log").write_text("")
    out.clear()
    lc.cmd_logs(args, settings, out.append)
    assert not any("no stt.stderr.log" in line for line in out)


def _uninstall_args(**overrides: bool) -> argparse.Namespace:
    base = {
        "state_dir": None,
        "yes": False,
        "remove_config": False,
        "remove_logs": False,
        "remove_models": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def _default_layout(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("VOICED_CONFIG")
    settings.install.venv_dir = home / ".voiced"
    return home


def test_uninstall_stops_loaded_agents_first(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _default_layout(settings, tmp_path, monkeypatch)
    settings.install.venv_dir.mkdir(parents=True)
    stt = settings.launchd.label("stt")
    fake = FakeLaunchctl({stt: True})
    out: list[str] = []
    assert lc.cmd_uninstall(_uninstall_args(yes=True), settings, fake, out.append) == 0
    assert ["launchctl", "bootout", f"{GUI}/{stt}"] in fake.calls
    assert f"stopped {stt}" in out
    assert not any("sudo" in line for line in out)
    assert home.exists()


def test_uninstall_dry_then_real(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VOICED_CONFIG", str(tmp_path / "cfg" / "config.toml"))
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg" / "config.toml").write_text("")
    settings.install.venv_dir = tmp_path / "venv"
    settings.install.venv_dir.mkdir()
    (settings.install.venv_dir / "pyvenv.cfg").write_text("home = /x\n")
    (settings.install.venv_dir / "lib/python3.12/site-packages/voiced-0.0.1.dist-info").mkdir(
        parents=True
    )
    settings.server.log_dir.mkdir()
    settings.install.hf_home = tmp_path / "hf"
    kokoro = locked("mlx-community/Kokoro-82M-bf16")
    assert kokoro is not None
    weights = b"w" * 2000
    hub = tmp_path / "hf" / "hub"
    hf.add_revision(hub, kokoro.repo, kokoro.revision, {"kokoro.safetensors": weights})
    fake = FakeLaunchctl({})
    out: list[str] = []

    assert lc.cmd_uninstall(_uninstall_args(remove_models=True), settings, fake, out.append) == 0
    assert settings.install.venv_dir.exists() and hf.store_path(hub, weights).exists()
    assert any("Would remove" in line for line in out)
    assert f"  {kokoro.repo} revision {kokoro.revision}" in out
    assert "Removing those models frees 2.0 KB (2,000 bytes)." in out

    args = _uninstall_args(yes=True, remove_models=True, remove_logs=True)
    assert lc.cmd_uninstall(args, settings, fake, out.append) == 0
    assert not settings.install.venv_dir.exists()
    assert not hf.repo_dir(hub, kokoro.repo).exists()
    assert not hf.store_path(hub, weights).exists()
    assert f"freed 2.0 KB (2,000 bytes) of models in {hub}" in out
    assert not settings.server.log_dir.exists()
    assert (tmp_path / "cfg" / "config.toml").exists()  # --remove-config not given


def test_watchdog_specs(settings: Settings) -> None:
    settings.server.bind = "0.0.0.0"
    specs = lc.watchdog_specs(settings)
    assert [s.name for s in specs] == ["stt", "tts"]
    # A wildcard bind is polled on loopback.
    assert specs[0].base_url == settings.http_url("stt")
    assert specs[0].base_url == f"http://127.0.0.1:{settings.stt.http_port}"
    assert specs[1].health_url == f"{settings.http_url('tts')}/health"
    assert specs[0].probe is not None and specs[0].probe.path == "/v1/audio/transcriptions"
    assert specs[1].probe is not None and b'"voice": "af_heart"' in specs[1].probe.body
    assert settings.watchdog.stall_after_s == 60 and settings.watchdog.probe is False


def test_settings_http_helpers_and_label_validation() -> None:
    s = Settings()
    assert s.http_port("stt") == 8881 and s.http_port("tts") == 8880
    assert s.http_url("tts") == "http://127.0.0.1:8880"
    s.server.bind = "0.0.0.0"
    assert s.http_url("stt") == "http://127.0.0.1:8881"
    s.server.bind = "10.0.0.5"
    assert s.http_url("stt") == "http://10.0.0.5:8881"
    s.server.bind = "::"
    assert s.http_url("tts") == "http://127.0.0.1:8880"
    s.server.bind = "::1"
    assert s.http_url("tts") == "http://[::1]:8880"
    import pydantic

    for bad in ("a;b", "x y", "ok\n", "", ".lead"):
        with pytest.raises(pydantic.ValidationError):
            Settings(launchd={"label_prefix": bad})  # type: ignore[arg-type]
    assert (
        Settings(launchd={"label_prefix": "org.example-1.voiced"}).launchd.label(  # type: ignore[arg-type]
            "metalprobe"
        )
        == "org.example-1.voiced.metalprobe"
    )


def test_doctor_metal_probe_runs_it_as_an_agent(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VOICED_CONFIG", str(tmp_path / "cfg" / "config.toml"))
    fake = FakeLaunchctl({})
    result = settings.server.log_dir / "metal-probe.json"

    def launchctl(argv: Sequence[str]) -> CommandResult:
        if argv[1] == "bootstrap":
            result.write_text('{"ok": true}')
        return fake(argv)

    args = argparse.Namespace(json=False, metal_probe=True)
    out: list[str] = []
    assert lc.cmd_doctor(args, settings, launchctl, out.append) == 0
    assert '{"ok": true}' in out
    assert [c[1] for c in fake.calls] == ["bootout", "print", "bootstrap", "bootout"]
    assert not (tmp_path / "cfg").exists(), "no script to write any more"


def test_uninstall_waits_for_an_install(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`uninstall --yes` takes the same lock as install: nothing goes while
    the app or another terminal is installing."""
    import threading

    from voiced.layout import Manifest, install_lock, write_manifest

    monkeypatch.setenv("VOICED_CONFIG", str(tmp_path / "config.toml"))
    state = settings.install.venv_dir = tmp_path / "state"
    write_manifest(state, Manifest(source="cli", environment=None))
    out: list[str] = []
    with install_lock(state):
        thread = threading.Thread(
            target=lc.cmd_uninstall,
            args=(_uninstall_args(yes=True), settings, FakeLaunchctl({}), out.append),
        )
        thread.start()
        for _ in range(100):
            if any("waiting" in line for line in out):
                break
            time.sleep(0.05)
        assert any("waiting for an install" in line for line in out)
        assert (state / "install.json").exists()
    thread.join(10)
    assert not state.exists()


# A service switched off (ADR 0033) ------------------------------------------


def test_start_of_a_service_switched_off_starts_it_and_says_so(settings: Settings) -> None:
    settings.stt.enabled = False
    stt, tts = settings.launchd.label("stt"), settings.launchd.label("tts")
    fake = FakeLaunchctl({stt: True, tts: True})
    out: list[str] = []
    assert lc.cmd_start("stt", settings, fake, out.append) == 0
    assert out == [
        "stt: off ([stt] enabled = false); turn it on with "
        "`voiced settings set stt.enabled=true` and `voiced restart stt`",
        f"stt: kickstarted {stt}",
    ]
    # Started as any other service: its process parks, and turning it on
    # later is a restart, which needs the agent loaded.
    assert ["launchctl", "kickstart", f"{GUI}/{stt}"] in fake.calls


def test_status_says_off_for_the_parked_process(settings: Settings) -> None:
    from voiced.control import state_dir
    from voiced.layout import off_marker

    settings.stt.enabled = False
    fake = FakeLaunchctl({settings.launchd.label(s): True for s in ("stt", "tts", "watchdog")})
    marker = off_marker(state_dir(settings), "stt")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("42\n")  # the pid launchd runs for stt (RUNNING)

    def poll(url: str) -> Observation:
        assert not url.startswith(settings.http_url("stt")), "a parked service is not polled"
        return Observation("idle", "", 10, {"model": "m/tts", "queue_depth": 0})

    rows = lc.collect_status(settings, fake, poll)
    assert rows[0]["health"] == "off"
    assert "voiced settings set stt.enabled=true" in rows[0]["health_detail"]
    assert rows[1]["health"] == "idle"
    # Turned on in the file, not restarted yet.
    settings.stt.enabled = True
    rows = lc.collect_status(settings, fake, poll)
    assert rows[0]["health"] == "off"
    assert rows[0]["health_detail"].startswith("off until restarted (`voiced restart stt`)")
    settings.stt.enabled = False

    out: list[str] = []
    assert lc.cmd_status(settings, fake, out.append, control=lambda s: None) == 1
    assert "off ([stt] enabled = false)" in out[0]


def test_status_is_fine_with_idle_and_off(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [
        {"service": "stt", "health": "off", "pid": 1, "launchd": "running"},
        {"service": "tts", "health": "idle", "pid": 2, "launchd": "running"},
        {"service": "watchdog", "pid": 3, "launchd": "running"},
    ]
    monkeypatch.setattr(lc, "collect_status", lambda *a, **k: rows)
    out: list[str] = []
    assert lc.cmd_status(settings, FakeLaunchctl({}), out.append, control=lambda s: None) == 0


def test_no_probe_for_a_service_that_unloads_when_idle(settings: Settings) -> None:
    """Each probe is a request: it would keep the model loaded (ADR 0033)."""
    settings.tts.idle_unload_s = 300
    stt, tts = lc.watchdog_specs(settings)
    assert stt.probe is not None
    assert tts.probe is None


def test_start_after_stop_bootstraps_a_service_switched_off(
    settings: Settings, isolated: Path
) -> None:
    """``voiced stop all`` then ``voiced start all``: the off service's agent
    is loaded again (it parks), so turning it on stays a restart."""
    settings.stt.enabled = False
    for name in ("stt", "tts", "watchdog"):
        (isolated / f"{settings.launchd.label(name)}.plist").write_text("x")
    out: list[str] = []
    lc.cmd_start("all", settings, FakeLaunchctl({}), out.append)
    stt = settings.launchd.label("stt")
    assert out[0].startswith("stt: off ([stt] enabled = false)")
    assert f"stt: loaded {isolated / f'{stt}.plist'}" in out
