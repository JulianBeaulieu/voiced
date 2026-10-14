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


@pytest.fixture
def no_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lc, "is_root", lambda: False)


def test_start_kickstarts_loaded_services(settings: Settings, no_root: None) -> None:
    fake = FakeLaunchctl({settings.launchd.label("stt"): True, settings.launchd.label("tts"): True})
    out: list[str] = []
    code = lc.cmd_start("stt", settings, fake, out.append)
    assert code == 0
    assert ["launchctl", "kickstart", "system/io.github.julianbeaulieu.voiced.stt"] in fake.calls
    assert out == ["stt: kickstarted io.github.julianbeaulieu.voiced.stt"]


def test_start_prints_bootstrap_when_installed_but_not_loaded(
    settings: Settings, no_root: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lc, "plist_path", lambda label: Path(__file__))  # "exists"
    fake = FakeLaunchctl({})
    out: list[str] = []
    assert lc.cmd_start("tts", settings, fake, out.append) == 1
    assert out[-1].strip().startswith("sudo launchctl bootstrap system ")
    assert not any(c[1] == "bootstrap" for c in fake.calls)


def test_start_reports_missing_install(settings: Settings, no_root: None) -> None:
    out: list[str] = []
    assert lc.cmd_start("all", settings, FakeLaunchctl({}), out.append) == 1
    assert all("not installed" in line for line in out)


def test_stop_prints_sudo_bootout(settings: Settings, no_root: None) -> None:
    fake = FakeLaunchctl({settings.launchd.label("stt"): True})
    out: list[str] = []
    assert lc.cmd_stop("all", settings, fake, out.append) == 1
    assert "  sudo launchctl bootout system/io.github.julianbeaulieu.voiced.stt" in out
    assert "tts: not loaded" in out
    assert not any(c[1] == "bootout" for c in fake.calls)


def test_stop_runs_bootout_as_root(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lc, "is_root", lambda: True)
    fake = FakeLaunchctl({settings.launchd.label("tts"): True})
    out: list[str] = []
    assert lc.cmd_stop("tts", settings, fake, out.append) == 0
    assert ["launchctl", "bootout", "system/io.github.julianbeaulieu.voiced.tts"] in fake.calls


def test_status_table(settings: Settings) -> None:
    fake = FakeLaunchctl({settings.launchd.label(s): True for s in ("stt", "tts", "watchdog")})

    def poll(url: str) -> Observation:
        if ":8881" in url:
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
    code = lc.cmd_status(settings, fake, out.append)
    assert code == 1  # nothing is listening on the test ports
    lines = out[0].splitlines()
    assert lines[0].split() == ["SERVICE", "LAUNCHD", "HEALTH", "DETAIL"]
    assert lines[1].startswith("stt       running (pid 42)")


def test_logs_tail_and_missing(settings: Settings, tmp_path: Path) -> None:
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
        "force": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_uninstall_refuses_while_loaded(settings: Settings, tmp_path: Path) -> None:
    settings.install.venv_dir = tmp_path / "venv"
    settings.install.venv_dir.mkdir()
    fake = FakeLaunchctl({settings.launchd.label("stt"): True})
    out: list[str] = []
    code = lc.cmd_uninstall(_uninstall_args(yes=True), settings, fake, out.append)
    assert code == 1
    assert settings.install.venv_dir.exists()
    assert any("still loaded: stt" in line for line in out)
    assert any(
        "launchctl bootout system/io.github.julianbeaulieu.voiced.stt" in line for line in out
    )


def test_uninstall_dry_then_real(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VOICED_CONFIG", str(tmp_path / "cfg" / "config.toml"))
    (tmp_path / "cfg").mkdir()
    settings.install.venv_dir = tmp_path / "venv"
    settings.install.venv_dir.mkdir()
    settings.server.log_dir.mkdir()
    settings.install.hf_home = tmp_path / "hf"
    model_dir = tmp_path / "hf" / "hub" / "models--mlx-community--Kokoro-82M-bf16"
    model_dir.mkdir(parents=True)
    fake = FakeLaunchctl({})
    out: list[str] = []

    code = lc.cmd_uninstall(_uninstall_args(remove_models=True), settings, fake, out.append)
    assert code == 0
    assert settings.install.venv_dir.exists() and model_dir.exists()
    assert any("Would remove" in line for line in out)

    args = _uninstall_args(yes=True, remove_models=True, remove_logs=True)
    assert lc.cmd_uninstall(args, settings, fake, out.append) == 0
    assert not settings.install.venv_dir.exists()
    assert not model_dir.exists()
    assert not settings.server.log_dir.exists()
    assert (tmp_path / "cfg").exists()  # --remove-config not given


def test_watchdog_specs_and_policy(settings: Settings) -> None:
    settings.server.bind = "0.0.0.0"
    specs = lc.watchdog_specs(settings)
    assert [s.name for s in specs] == ["stt", "tts"]
    assert specs[0].base_url == "http://127.0.0.1:8881"
    assert specs[1].health_url == "http://127.0.0.1:8880/health"
    assert specs[0].probe is not None and specs[0].probe.path == "/v1/audio/transcriptions"
    assert specs[1].probe is not None and b'"voice": "af_heart"' in specs[1].probe.body
    policy = lc.watchdog_policy(settings)
    assert policy.interval_s == 15 and policy.failures_before_restart == 3
    assert policy.probe is False


def test_metal_probe_script(settings: Settings, tmp_path: Path) -> None:
    plan = lc.plan_install(settings, user="u", home=tmp_path, staging_dir=tmp_path, uv=None)
    staged, script, plist = lc.metal_probe_script(settings, plan, Path("/v/bin/python"))
    assert staged.name == "io.github.julianbeaulieu.voiced.metalprobe.plist"
    assert b"<false/>" in plist  # KeepAlive off: one-shot
    assert b"voiced.launchd.metal_probe" in plist
    assert "launchctl bootstrap system" in script
    assert 'rm -f "$DST"' in script  # self-cleaning
