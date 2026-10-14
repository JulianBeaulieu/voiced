"""Lifecycle commands against a fake launchctl and a fake health poller."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

import pytest

from voiced import cli_lifecycle as lc
from voiced.config import Settings
from voiced.launchd.control import CommandResult
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
        "venv": None,
        "yes": False,
        "remove_config": False,
        "remove_logs": False,
        "remove_models": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_uninstall_refuses_while_loaded(settings: Settings, tmp_path: Path) -> None:
    settings.install.venv_dir = tmp_path / "venv"
    settings.install.venv_dir.mkdir()
    stt = settings.launchd.label("stt")
    fake = FakeLaunchctl({stt: True})
    out: list[str] = []
    assert lc.cmd_uninstall(_uninstall_args(yes=True), settings, fake, out.append) == 1
    assert settings.install.venv_dir.exists()
    assert any("still loaded: stt" in line for line in out)
    assert any(f"launchctl bootout system/{stt}" in line for line in out)


def test_uninstall_dry_then_real(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VOICED_CONFIG", str(tmp_path / "cfg" / "config.toml"))
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg" / "config.toml").write_text("")
    settings.install.venv_dir = tmp_path / "venv"
    settings.install.venv_dir.mkdir()
    (settings.install.venv_dir / "pyvenv.cfg").write_text("home = /x\n")
    settings.server.log_dir.mkdir()
    settings.install.hf_home = tmp_path / "hf"
    model_dir = tmp_path / "hf" / "hub" / "models--mlx-community--Kokoro-82M-bf16"
    model_dir.mkdir(parents=True)
    fake = FakeLaunchctl({})
    out: list[str] = []

    assert lc.cmd_uninstall(_uninstall_args(remove_models=True), settings, fake, out.append) == 0
    assert settings.install.venv_dir.exists() and model_dir.exists()
    assert any("Would remove" in line for line in out)

    args = _uninstall_args(yes=True, remove_models=True, remove_logs=True)
    assert lc.cmd_uninstall(args, settings, fake, out.append) == 0
    assert not settings.install.venv_dir.exists()
    assert not model_dir.exists()
    assert not settings.server.log_dir.exists()
    assert (tmp_path / "cfg" / "config.toml").exists()  # --remove-config not given


def test_watchdog_specs(settings: Settings) -> None:
    settings.server.bind = "0.0.0.0"
    specs = lc.watchdog_specs(settings)
    assert [s.name for s in specs] == ["stt", "tts"]
    assert specs[0].base_url == "http://127.0.0.1:8881"
    assert specs[1].health_url == "http://127.0.0.1:8880/health"
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


def test_doctor_metal_probe_stages_script(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VOICED_CONFIG", str(tmp_path / "cfg" / "config.toml"))
    args = argparse.Namespace(json=False, metal_probe=True)
    out: list[str] = []
    assert lc.cmd_doctor(args, settings, FakeLaunchctl({}), out.append) == 0
    staging = tmp_path / "cfg" / "launchd"
    assert (staging / "metal-probe.sh").exists()
    assert (staging / f"{settings.launchd.label('metalprobe')}.plist").exists()
    assert any(line.startswith("Run:  sudo bash ") for line in out)
