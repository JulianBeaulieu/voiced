"""``voiced install --dry-run`` renders the expected plists and sudo script."""

from __future__ import annotations

import plistlib
from collections.abc import Sequence
from pathlib import Path

import pytest

from voiced.cli import main
from voiced.config import Settings
from voiced.launchd.control import CommandResult
from voiced.launchd.install import (
    InstallError,
    InstallPlan,
    plan_install,
    run_install,
    write_default_config,
    write_staging,
)


@pytest.fixture
def plan(settings: Settings, tmp_path: Path) -> InstallPlan:
    settings.install.hf_home = Path("/Users/someone/hf")
    return plan_install(
        settings,
        venv_dir=Path("/Users/someone/.voiced"),
        user="someone",
        home=Path("/Users/someone"),
        staging_dir=tmp_path / "stage",
        config_file=Path("/Users/someone/.config/voiced/config.toml"),
        source="voiced==0.0.1",
        uv="/opt/homebrew/bin/uv",
    )


def test_plists(plan: InstallPlan) -> None:
    plists = {service: plistlib.loads(data) for service, data in plan.plists().items()}
    assert set(plists) == {"stt", "tts", "watchdog"}

    stt = plists["stt"]
    assert stt["Label"] == "io.github.julianbeaulieu.voiced.stt"
    assert stt["ProgramArguments"] == ["/Users/someone/.voiced/bin/voiced", "serve", "stt"]
    assert stt["UserName"] == "someone"
    assert stt["RunAtLoad"] is True
    assert stt["KeepAlive"] is True
    assert stt["ThrottleInterval"] == 10
    assert stt["ProcessType"] == "Interactive"
    assert stt["WorkingDirectory"] == "/Users/someone"
    env = stt["EnvironmentVariables"]
    assert env["HOME"] == "/Users/someone"
    assert env["PATH"].startswith("/Users/someone/.voiced/bin:/opt/homebrew/bin:")
    assert env["VOICED_CONFIG"] == "/Users/someone/.config/voiced/config.toml"
    assert env["HF_HOME"] == "/Users/someone/hf"
    assert stt["StandardOutPath"].endswith("/stt.stdout.log")

    assert plists["tts"]["ProgramArguments"][-2:] == ["serve", "tts"]
    assert plists["watchdog"]["ProgramArguments"] == [
        "/Users/someone/.voiced/bin/voiced",
        "watchdog",
    ]
    assert plists["watchdog"]["Label"] == "io.github.julianbeaulieu.voiced.watchdog"


def test_no_hf_home_means_no_env_key(settings: Settings, tmp_path: Path) -> None:
    p = plan_install(settings, user="u", home=tmp_path, staging_dir=tmp_path, uv=None)
    assert "HF_HOME" not in p.environment()


def test_sudo_install_script(plan: InstallPlan) -> None:
    script = plan.sudo_install_script()
    assert script.startswith("#!/bin/bash")
    assert "set -euo pipefail" in script
    # The user adds sudo; the script never escalates itself.
    assert not any(line.startswith("sudo") for line in script.splitlines())
    for label in plan.labels.values():
        assert f'cp "$STAGING/{label}.plist" "$DAEMONS/{label}.plist"' in script
        assert f'chown root:wheel "$DAEMONS/{label}.plist"' in script
        assert f'chmod 644 "$DAEMONS/{label}.plist"' in script
        assert f"launchctl bootstrap system /Library/LaunchDaemons/{label}.plist" in script
        assert f"launchctl bootout system/{label} 2>/dev/null || true" in script
    assert "DAEMONS=/Library/LaunchDaemons" in script


def test_sudo_uninstall_script(plan: InstallPlan) -> None:
    script = plan.sudo_uninstall_script()
    for label in plan.labels.values():
        assert f"launchctl bootout system/{label}" in script
        assert f"rm -f /Library/LaunchDaemons/{label}.plist" in script


def test_venv_commands_with_uv(plan: InstallPlan) -> None:
    cmds = plan.venv_commands()
    assert cmds[0] == [
        "/opt/homebrew/bin/uv",
        "venv",
        "--allow-existing",
        "--python",
        "3.12",
        "/Users/someone/.voiced",
    ]
    assert cmds[1][:3] == ["/opt/homebrew/bin/uv", "pip", "install"]
    assert cmds[1][-1] == "voiced==0.0.1[stt,tts]"


def test_venv_commands_with_pip(settings: Settings, tmp_path: Path) -> None:
    p = plan_install(
        settings,
        venv_dir=Path("/v"),
        user="u",
        home=tmp_path,
        staging_dir=tmp_path,
        source=".",
        uv=None,
        python="/usr/bin/python3",
    )
    cmds = p.venv_commands()
    assert cmds[0] == ["/usr/bin/python3", "-m", "venv", "/v"]
    assert cmds[-1] == ["/v/bin/python", "-m", "pip", "install", "--upgrade", ".[stt,tts]"]


def test_model_download_runs_in_venv_with_config(plan: InstallPlan) -> None:
    cmd = plan.model_download_command()
    assert cmd[:2] == ["/usr/bin/env", "VOICED_CONFIG=/Users/someone/.config/voiced/config.toml"]
    assert "HF_HOME=/Users/someone/hf" in cmd
    assert cmd[-3] == "/Users/someone/.voiced/bin/python"
    assert "engine.load()" in cmd[-1]


def test_write_staging(plan: InstallPlan) -> None:
    written = write_staging(plan)
    names = sorted(p.name for p in written)
    assert names == sorted(
        [
            "install-daemons.sh",
            "uninstall-daemons.sh",
            "io.github.julianbeaulieu.voiced.stt.plist",
            "io.github.julianbeaulieu.voiced.tts.plist",
            "io.github.julianbeaulieu.voiced.watchdog.plist",
        ]
    )
    for path in written:
        assert path.exists()
    plistlib.loads(plan.staged_plist("tts").read_bytes())


def test_write_default_config(tmp_path: Path) -> None:
    target = tmp_path / "cfg" / "config.toml"
    assert write_default_config(target) is True
    text = target.read_text()
    assert "[server]" in text and "[watchdog]" in text and "[install]" in text
    target.write_text("# mine")
    assert write_default_config(target) is False
    assert target.read_text() == "# mine"


def test_run_install_executes_steps_in_order(settings: Settings, tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    settings.server.log_dir = tmp_path / "logs"
    p = plan_install(
        settings,
        venv_dir=tmp_path / "venv",
        user="u",
        home=tmp_path,
        staging_dir=tmp_path / "stage",
        config_file=config_file,
        source="voiced==0.0.1",
        uv="/fake/uv",
    )
    calls: list[list[str]] = []
    said: list[str] = []

    def runner(argv: Sequence[str]) -> CommandResult:
        calls.append(list(argv))
        return CommandResult(0)

    run_install(p, runner=runner, say=said.append)
    assert calls[0][:2] == ["/fake/uv", "venv"]
    assert calls[1][:3] == ["/fake/uv", "pip", "install"]
    assert calls[2][0] == "/usr/bin/env"  # model download
    assert config_file.exists()
    assert (tmp_path / "logs").is_dir()
    assert p.install_script_path.exists()
    assert any("wrote default config" in line for line in said)


def test_run_install_stops_on_failure(settings: Settings, tmp_path: Path) -> None:
    p = plan_install(
        settings,
        venv_dir=tmp_path / "venv",
        user="u",
        home=tmp_path,
        staging_dir=tmp_path / "stage",
        config_file=tmp_path / "config.toml",
        source="voiced==0.0.1",
        uv="/fake/uv",
    )

    def runner(argv: Sequence[str]) -> CommandResult:
        return CommandResult(1, "", "boom")

    with pytest.raises(InstallError, match="boom"):
        run_install(p, runner=runner, say=lambda _: None)
    assert not p.install_script_path.exists()


def test_cli_dry_run_prints_everything_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("VOICED_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.setenv("VOICED_SERVER__LOG_DIR", str(tmp_path / "logs"))
    staging = tmp_path / "stage"
    code = main(
        [
            "install",
            "--dry-run",
            "--venv",
            str(tmp_path / "venv"),
            "--user",
            "someone",
            "--staging-dir",
            str(staging),
            "--source",
            "voiced==0.0.1",
            "--no-uv",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert not staging.exists()
    assert not (tmp_path / "logs").exists()
    assert "dry run" in out
    assert "<key>Label</key>" in out
    assert "io.github.julianbeaulieu.voiced.watchdog</string>" in out
    assert "<string>someone</string>" in out
    stt_plist = "/Library/LaunchDaemons/io.github.julianbeaulieu.voiced.stt.plist"
    assert f"launchctl bootstrap system {stt_plist}" in out
    assert str(staging / "install-daemons.sh") in out
    assert "-m venv" in out and "voiced==0.0.1[stt,tts]" in out
