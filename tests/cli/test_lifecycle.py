"""Lifecycle commands against a fake launchctl and a fake health poller."""

from __future__ import annotations

import argparse
import os
import pwd
import shlex
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
    def __init__(self, loaded: dict[str, bool]) -> None:
        self.loaded = loaded
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str]) -> CommandResult:
        self.calls.append(list(argv))
        if argv[1] == "print":
            label = argv[2].split("/", 1)[1]
            return CommandResult(0, RUNNING) if self.loaded.get(label) else NOT_LOADED
        return CommandResult(0)


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Never read the real /Library/LaunchDaemons or run as root in tests."""
    daemons = tmp_path / "LaunchDaemons"
    daemons.mkdir()
    monkeypatch.setattr(lc, "plist_path", lambda label: daemons / f"{label}.plist")
    monkeypatch.setattr(lc, "is_root", lambda: False)
    return daemons


def test_start_kickstarts_loaded_services(settings: Settings) -> None:
    stt = settings.launchd.label("stt")
    fake = FakeLaunchctl({stt: True})
    out: list[str] = []
    assert lc.cmd_start("stt", settings, fake, out.append) == 0
    assert ["launchctl", "kickstart", f"system/{stt}"] in fake.calls
    assert out == [f"stt: kickstarted {stt}"]


def test_start_prints_bootstrap_when_installed_but_not_loaded(
    settings: Settings, isolated: Path
) -> None:
    tts = settings.launchd.label("tts")
    (isolated / f"{tts}.plist").write_text("x")
    fake = FakeLaunchctl({})
    out: list[str] = []
    assert lc.cmd_start("tts", settings, fake, out.append) == 1
    assert f"  sudo launchctl bootstrap system /Library/LaunchDaemons/{tts}.plist" in out
    assert not any(c[1] == "bootstrap" for c in fake.calls)


def test_start_runs_bootstrap_as_root(
    settings: Settings, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lc, "is_root", lambda: True)
    tts = settings.launchd.label("tts")
    (isolated / f"{tts}.plist").write_text("x")
    fake = FakeLaunchctl({})
    out: list[str] = []
    assert lc.cmd_start("tts", settings, fake, out.append) == 0
    assert any(c[1] == "bootstrap" for c in fake.calls)


def test_start_reports_missing_install(settings: Settings) -> None:
    out: list[str] = []
    assert lc.cmd_start("all", settings, FakeLaunchctl({}), out.append) == 1
    assert all("not installed" in line for line in out)


def test_stop_prints_sudo_bootout(settings: Settings) -> None:
    stt = settings.launchd.label("stt")
    fake = FakeLaunchctl({stt: True})
    out: list[str] = []
    assert lc.cmd_stop("all", settings, fake, out.append) == 1
    assert f"  sudo launchctl bootout system/{stt}" in out
    assert "tts: not loaded" in out
    assert not any(c[1] == "bootout" for c in fake.calls)


def test_stop_runs_bootout_as_root(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lc, "is_root", lambda: True)
    tts = settings.launchd.label("tts")
    fake = FakeLaunchctl({tts: True})
    out: list[str] = []
    assert lc.cmd_stop("tts", settings, fake, out.append) == 0
    assert ["launchctl", "bootout", f"system/{tts}"] in fake.calls


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


def test_status_says_what_to_do_before_the_daemons_are_installed(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After `voiced install` in the default layout, the root step; before
    it, or for a layout the root step does not install, `voiced install`."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("VOICED_CONFIG")
    settings.install.venv_dir = home / ".voiced"
    out: list[str] = []
    assert lc.cmd_status(settings, FakeLaunchctl({}), out.append, control=lambda s: None) == 1
    assert out[-1].endswith("Run `voiced install` first.")
    entry = home / ".voiced/bin/voiced"
    entry.parent.mkdir(parents=True)
    entry.write_text("")
    out.clear()
    lc.cmd_status(settings, FakeLaunchctl({}), out.append, control=lambda s: None)
    lead, _, command = out[-1].partition(": ")
    assert lead == "The daemons are not installed. Run"
    script = Path(lc.__file__).parent / "launchd/voiced-privileged.sh"
    user = pwd.getpwuid(os.getuid()).pw_name
    assert shlex.split(command) == ["sudo", "/bin/bash", str(script), "install", user]
    settings.launchd.label_prefix = "org.example.voiced"
    out.clear()
    lc.cmd_status(settings, FakeLaunchctl({}), out.append, control=lambda s: None)
    assert out[-1].endswith("Run `voiced install` first.")


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


def test_uninstall_refuses_while_loaded(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _default_layout(settings, tmp_path, monkeypatch)
    settings.install.venv_dir.mkdir(parents=True)
    stt = settings.launchd.label("stt")
    fake = FakeLaunchctl({stt: True})
    out: list[str] = []
    assert lc.cmd_uninstall(_uninstall_args(yes=True), settings, fake, out.append) == 1
    assert settings.install.venv_dir.exists()
    assert any("still loaded: stt" in line for line in out)
    user = pwd.getpwuid(os.getuid()).pw_name
    lead, _, command = out[0].partition(": ")
    assert lead == "Root step (stop and remove the daemons)"
    script = Path(lc.__file__).parent / "launchd/voiced-privileged.sh"
    assert shlex.split(command) == ["sudo", "/bin/bash", str(script), "uninstall", user]


@pytest.mark.parametrize("layout", ["state_dir", "label_prefix"])
def test_uninstall_prints_no_root_step_for_another_layout(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str
) -> None:
    """`uninstall --state-dir /tmp/x` from a harness must not print the line
    that removes the real daemons; nor must another label prefix, whose
    daemons the script would not remove."""
    _default_layout(settings, tmp_path, monkeypatch)
    args = _uninstall_args()
    if layout == "state_dir":
        args.state_dir = tmp_path / "x"
    else:
        settings.launchd.label_prefix = "org.example.voiced"
    out: list[str] = []
    lc.cmd_uninstall(args, settings, FakeLaunchctl({}), out.append)
    assert out[0].startswith("No root step: the daemons run ~/.voiced/bin/voiced")
    assert not any("sudo" in line for line in out)


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


def test_doctor_metal_probe_writes_one_script(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VOICED_CONFIG", str(tmp_path / "cfg" / "config.toml"))
    args = argparse.Namespace(json=False, metal_probe=True)
    out: list[str] = []
    assert lc.cmd_doctor(args, settings, FakeLaunchctl({}), out.append) == 0
    script = tmp_path / "cfg" / "metal-probe.sh"
    assert sorted(x.name for x in script.parent.iterdir()) == ["metal-probe.sh"]
    assert f"Review, then run:  sudo /bin/bash {script}" in out


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


def test_start_of_a_service_switched_off_says_how_to_turn_it_on(settings: Settings) -> None:
    settings.stt.enabled = False
    stt, tts = settings.launchd.label("stt"), settings.launchd.label("tts")
    fake = FakeLaunchctl({stt: True, tts: True})
    out: list[str] = []
    assert lc.cmd_start("stt", settings, fake, out.append) == 0
    assert out == [
        "stt: off ([stt] enabled = false); turn it on with "
        "`voiced settings set stt.enabled=true` and `voiced restart stt`"
    ]
    assert fake.calls == [], "not kickstarted, not bootstrapped"
    out.clear()
    lc.cmd_start("tts", settings, fake, out.append)
    assert ["launchctl", "kickstart", f"system/{tts}"] in fake.calls


def test_status_says_off_for_the_parked_process(settings: Settings) -> None:
    from voiced.control import state_dir
    from voiced.launchd import parked

    fake = FakeLaunchctl({settings.launchd.label(s): True for s in ("stt", "tts", "watchdog")})
    marker = parked.marker(state_dir(settings), "stt")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("42\n")  # the pid launchd runs for stt (RUNNING)

    def poll(url: str) -> Observation:
        assert not url.startswith(settings.http_url("stt")), "a parked service is not polled"
        return Observation("idle", "", 10, {"model": "m/tts", "queue_depth": 0})

    rows = lc.collect_status(settings, fake, poll)
    assert rows[0]["health"] == "off"
    assert "voiced settings set stt.enabled=true" in rows[0]["health_detail"]
    assert rows[1]["health"] == "idle"

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
