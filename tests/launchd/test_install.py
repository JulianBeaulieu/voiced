"""Installer plan, golden plists and scripts, dry-run, and uninstall safety."""

from __future__ import annotations

import inspect
import os
import plistlib
import pwd
import shlex
import stat
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from voiced.cli import main
from voiced.config import SERVICES, Settings
from voiced.launchd import install as install_module
from voiced.launchd.control import CommandResult, run_streaming
from voiced.launchd.install import (
    InstallError,
    InstallPlan,
    daemon_install_lines,
    plan_install,
    plan_summary,
    removal_refusal,
    remove_targets,
    requirement_with_extras,
    run_install,
    uninstall_targets,
    write_default_config,
    write_staging,
)

GOLDEN = Path(__file__).parent / "golden"
HOME = Path("/Users/someone")


def golden_plan(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> InstallPlan:
    """Fixed user, home, label prefix and paths so the artifacts are reproducible."""
    monkeypatch.setattr(install_module, "installing_user", lambda: "someone")
    settings.launchd.label_prefix = "org.example.voiced"
    settings.install.hf_home = None
    return plan_install(
        settings,
        venv_dir=HOME / ".voiced",
        home=HOME,
        staging_dir=HOME / ".config/voiced/launchd",
        config_file=HOME / ".config/voiced/config.toml",
        source="voiced==0.0.1",
        uv="/opt/homebrew/bin/uv",
    )


@pytest.fixture
def plan(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> InstallPlan:
    monkeypatch.delenv("HF_HOME", raising=False)
    settings.server.log_dir = HOME / "Library/Logs/voiced"
    return golden_plan(settings, monkeypatch)


def _compare(actual: str, name: str) -> None:
    path = GOLDEN / name
    if not path.exists():  # first run: write the golden file, then fail loudly
        path.write_text(actual)
        pytest.fail(f"golden file {name} created; re-run")
    assert actual == path.read_text(), f"{name} differs from golden file"


@pytest.mark.parametrize("service", SERVICES)
def test_plists_match_golden(plan: InstallPlan, service: str) -> None:
    rendered = plan.render(service).decode()
    _compare(rendered, f"{service}.plist")
    parsed = plistlib.loads(plan.render(service))
    assert parsed["Label"] == plan.labels[service]
    assert parsed["UserName"] == "someone"
    assert parsed["KeepAlive"] is True and parsed["RunAtLoad"] is True
    assert parsed["EnvironmentVariables"]["VOICED_CONFIG"] == str(plan.config_file)


def test_install_script_matches_golden(plan: InstallPlan) -> None:
    _compare(plan.sudo_install_script(), "install-daemons.sh")


def test_uninstall_script_matches_golden(plan: InstallPlan) -> None:
    _compare(plan.sudo_uninstall_script(), "uninstall-daemons.sh")


def test_metal_probe_matches_golden(plan: InstallPlan) -> None:
    probe = plan.metal_probe(HOME / ".voiced/bin/python")
    _compare(probe.script, "metal-probe.sh")
    _compare(probe.plist.decode(), "metalprobe.plist")
    assert plistlib.loads(probe.plist)["KeepAlive"] is False
    assert probe.label == "org.example.voiced.metalprobe"
    assert "trap " in probe.script and "EXIT" in probe.script


def _stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!/bin/bash\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_metal_probe_script_cleans_up_when_bootstrap_fails(
    plan: InstallPlan, tmp_path: Path
) -> None:
    """Run the generated script with stubbed tools: a failed bootstrap must still rm the plist."""
    probe = plan.metal_probe(HOME / ".voiced/bin/python")
    script = tmp_path / "metal-probe.sh"
    script.write_text(probe.script)
    stubs = tmp_path / "bin"
    stubs.mkdir()
    log = tmp_path / "calls.log"
    _stub(
        stubs,
        "launchctl",
        f'echo "launchctl $*" >> {log}\ncase "$1" in bootstrap) exit 5;; esac\nexit 113',
    )
    for tool in ("cp", "chown", "chmod"):
        _stub(stubs, tool, f'echo "{tool} $*" >> {log}')
    _stub(stubs, "rm", f'echo "rm $*" >> {log}')
    env = {"PATH": f"{stubs}:/usr/bin:/bin", "HOME": str(tmp_path)}
    proc = subprocess.run(["/bin/bash", str(script)], env=env, capture_output=True, text=True)
    assert proc.returncode != 0  # bootstrap failed under set -e
    calls = log.read_text().splitlines()
    dst = f"/Library/LaunchDaemons/{probe.label}.plist"
    assert f"launchctl bootstrap system {dst}" in calls
    assert calls[-2] == f"launchctl bootout system/{probe.label}"  # the trap's bootout
    assert calls[-1] == f"rm -f {dst}"  # ran even though that bootout failed


def test_scripts_never_escalate(plan: InstallPlan) -> None:
    for script in (plan.sudo_install_script(), plan.sudo_uninstall_script()):
        assert not any(line.startswith("sudo") for line in script.splitlines())


def test_paths_with_spaces_are_quoted(settings: Settings, tmp_path: Path) -> None:
    weird = tmp_path / "dir with spaces $HOME"
    settings.server.log_dir = weird / "logs"
    p = plan_install(settings, home=weird, staging_dir=weird / "st", uv=None)
    script = p.sudo_install_script()
    assert shlex.quote(str(p.staged_plist("stt"))) in script
    lines = daemon_install_lines("a.b", weird / "a.b.plist")
    # The copy line round-trips through the shell lexer with the path intact.
    assert shlex.split(lines[2]) == [
        "cp",
        str(weird / "a.b.plist"),
        "/Library/LaunchDaemons/a.b.plist",
    ]


def test_long_venv_path_is_not_flagged(settings: Settings, tmp_path: Path) -> None:
    """uv and pip both write a /bin/sh trampoline instead of a ``#!`` line when the
    interpreter path is long or has spaces, and the engine reads espeak-ng data
    from Homebrew, not from the venv, so no venv path length is too long."""
    venv = tmp_path / "Application Support" / ("x" * 200) / ".voiced"
    p = plan_install(settings, venv_dir=venv, home=tmp_path, uv=None)
    summary = plan_summary(p)
    assert f"venv           {venv}" in summary
    assert "warning" not in summary


def test_default_install_runner_streams() -> None:
    assert inspect.signature(run_install).parameters["runner"].default is run_streaming


def test_reinstall_waits_for_bootout() -> None:
    lines = daemon_install_lines("a.b", Path("/s/a.b.plist"))
    assert lines[0] == "launchctl bootout system/a.b 2>/dev/null || true"
    assert "launchctl print system/a.b >/dev/null 2>&1 || break" in lines[1]
    assert lines.index(next(line for line in lines if "bootstrap" in line)) > 1


def test_hf_home_resolved_once_for_plist_and_download(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def make() -> InstallPlan:
        return plan_install(settings, home=tmp_path, staging_dir=tmp_path, uv=None)

    monkeypatch.delenv("HF_HOME", raising=False)
    p = make()
    assert "HF_HOME" not in p.environment()
    assert not any(a.startswith("HF_HOME=") for a in p.model_download_command())

    monkeypatch.setenv("HF_HOME", str(tmp_path / "from-env"))
    p = make()  # the shell's HF_HOME reaches the daemons and the download alike
    assert p.environment()["HF_HOME"] == str(tmp_path / "from-env")
    assert f"HF_HOME={tmp_path / 'from-env'}" in p.model_download_command()

    settings.install.hf_home = tmp_path / "from-config"
    p = make()  # the config key wins over the environment
    assert p.environment()["HF_HOME"] == str(tmp_path / "from-config")
    assert f"HF_HOME={tmp_path / 'from-config'}" in p.model_download_command()

    settings.install.hf_home = None
    monkeypatch.setenv("HF_HOME", "relative/cache")
    monkeypatch.chdir(tmp_path)
    p = make()  # a relative HF_HOME is made absolute: the daemon's cwd differs
    assert p.environment()["HF_HOME"] == str(tmp_path / "relative/cache")


def test_espeak_override_reaches_daemons_and_download(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The override `voiced doctor` suggests must be in the daemons' environment too."""

    def make() -> InstallPlan:
        return plan_install(settings, home=tmp_path, staging_dir=tmp_path, uv=None)

    p = make()
    assert not {"VOICED_ESPEAK_LIBRARY", "VOICED_ESPEAK_DATA"} & set(p.environment())

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VOICED_ESPEAK_LIBRARY", "lib/libespeak-ng.dylib")
    monkeypatch.setenv("VOICED_ESPEAK_DATA", str(tmp_path / "data"))
    p = make()
    expected = {
        "VOICED_ESPEAK_LIBRARY": str(tmp_path / "lib/libespeak-ng.dylib"),  # made absolute
        "VOICED_ESPEAK_DATA": str(tmp_path / "data"),
    }
    for service in ("stt", "tts", "watchdog"):
        env = plistlib.loads(p.render(service))["EnvironmentVariables"]
        assert {k: env[k] for k in expected} == expected
    command = p.model_download_command()
    assert all(f"{k}={v}" in command for k, v in expected.items())
    assert f"VOICED_ESPEAK_LIBRARY {expected['VOICED_ESPEAK_LIBRARY']}" in plan_summary(p)


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
    # Extras go before the version, not after: `voiced==0.0.1[stt,tts]` is
    # invalid PEP 508 and pip rejects it.
    assert cmds[1][-1] == "voiced[stt,tts]==0.0.1"


def test_requirement_places_extras_before_a_version_specifier(settings: Settings) -> None:
    """A pinned release source (`voiced==X`, the default after the first PyPI
    release) must keep the extras before the version: `voiced==X[stt,tts]` is
    invalid PEP 508 and a bare `voiced install` would fail on it."""
    pinned = plan_install(settings, source="voiced==0.0.1")
    assert pinned.requirement == "voiced[stt,tts]==0.0.1"
    # A dev checkout source is a path; its extras go at the end.
    path = plan_install(settings, source="/Users/someone/voiced")
    assert path.requirement == "/Users/someone/voiced[stt,tts]"


def test_requirement_with_extras_variants() -> None:
    e = ("stt", "tts")
    assert requirement_with_extras("voiced==0.0.1", e) == "voiced[stt,tts]==0.0.1"
    assert requirement_with_extras("voiced>=1,<2", e) == "voiced[stt,tts]>=1,<2"
    assert requirement_with_extras("/home/me/voiced", e) == "/home/me/voiced[stt,tts]"
    assert requirement_with_extras(".", e) == ".[stt,tts]"
    assert requirement_with_extras("voiced", e) == "voiced[stt,tts]"
    assert requirement_with_extras("voiced==0.0.1", ()) == "voiced==0.0.1"


def test_venv_commands_with_pip(settings: Settings, tmp_path: Path) -> None:
    p = plan_install(
        settings,
        venv_dir=Path("/v"),
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
    assert cmd[:2] == ["/usr/bin/env", f"VOICED_CONFIG={plan.config_file}"]
    assert cmd[-3] == str(plan.python_bin)
    assert "engine.load()" in cmd[-1]


def test_write_staging(settings: Settings, tmp_path: Path) -> None:
    p = plan_install(settings, home=tmp_path, staging_dir=tmp_path / "s", uv=None)
    written = write_staging(p)
    assert sorted(x.name for x in written) == sorted(
        [
            "install-daemons.sh",
            "uninstall-daemons.sh",
            *(f"{p.labels[s]}.plist" for s in SERVICES),
        ]
    )
    plistlib.loads(p.staged_plist("tts").read_bytes())


def test_write_default_config(tmp_path: Path) -> None:
    target = tmp_path / "cfg" / "config.toml"
    assert write_default_config(target) is True
    text = target.read_text()
    assert "[server]" in text and "[watchdog]" in text and "[install]" in text
    # api_key goes in this file, and other local users can read 0644 files in
    # a macOS home directory through the staff group.
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    target.write_text("# mine")
    assert write_default_config(target) is False
    assert target.read_text() == "# mine"


def _tmp_plan(settings: Settings, tmp_path: Path) -> InstallPlan:
    settings.server.log_dir = tmp_path / "logs"
    return plan_install(
        settings,
        venv_dir=tmp_path / "venv",
        home=tmp_path,
        staging_dir=tmp_path / "stage",
        config_file=tmp_path / "config.toml",
        source="voiced==0.0.1",
        uv="/fake/uv",
    )


def test_run_install_executes_steps_in_order(settings: Settings, tmp_path: Path) -> None:
    p = _tmp_plan(settings, tmp_path)
    calls: list[list[str]] = []
    said: list[str] = []

    def runner(argv: Sequence[str]) -> CommandResult:
        calls.append(list(argv))
        if argv[1] == "venv":  # the venv step produces the entry point
            p.voiced_bin.parent.mkdir(parents=True)
            p.voiced_bin.write_text("#!/bin/sh\n")
        return CommandResult(0)

    run_install(p, runner=runner, say=said.append)
    assert [c[:2] for c in calls] == [
        ["/fake/uv", "venv"],
        ["/fake/uv", "pip"],
        ["/usr/bin/env", f"VOICED_CONFIG={p.config_file}"],
    ]
    assert p.config_file.exists()
    assert (tmp_path / "logs").is_dir()
    assert p.install_script_path.exists()
    assert any("created from config.example.toml" in line for line in said)


def test_run_install_dry_run_touches_nothing(settings: Settings, tmp_path: Path) -> None:
    p = _tmp_plan(settings, tmp_path)
    said: list[str] = []

    def runner(argv: Sequence[str]) -> CommandResult:
        pytest.fail("dry run must not execute anything")

    run_install(p, runner=runner, dry_run=True, say=said.append)
    assert not p.config_file.exists()
    assert not p.staging_dir.exists()
    assert not (tmp_path / "logs").exists()
    text = "\n".join(said)
    assert "would be created" in text
    assert "$ /fake/uv venv" in text
    assert f"would write {p.install_script_path}" in text


def test_run_install_fails_if_entry_point_missing(settings: Settings, tmp_path: Path) -> None:
    p = _tmp_plan(settings, tmp_path)
    with pytest.raises(InstallError, match="missing after install"):
        run_install(p, runner=lambda argv: CommandResult(0), say=lambda _: None)


def test_run_install_stops_on_failure(settings: Settings, tmp_path: Path) -> None:
    p = _tmp_plan(settings, tmp_path)
    with pytest.raises(InstallError, match="boom"):
        run_install(p, runner=lambda argv: CommandResult(1, "", "boom"), say=lambda _: None)
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
            "--staging-dir",
            str(staging),
            "--source",
            "voiced==0.0.1",
            "--no-uv",
        ]
    )
    out = capsys.readouterr().out
    labels = Settings().launchd
    assert code == 0
    assert not staging.exists()
    assert not (tmp_path / "logs").exists()
    assert "dry run" in out
    assert f"<string>{labels.label('watchdog')}</string>" in out
    assert f"<string>{pwd.getpwuid(os.getuid()).pw_name}</string>" in out
    assert f"{staging / 'install-daemons.sh'} (would be written)" in out
    assert "-m venv" in out and "voiced[stt,tts]==0.0.1" in out


# Uninstall safety --------------------------------------------------------------


def test_removal_refusals(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    assert removal_refusal(Path("/"), home) == "is the filesystem root"
    assert removal_refusal(home, home) == "is the home directory"
    assert removal_refusal(tmp_path, home) == "contains the home directory"
    link = tmp_path / "link"
    link.symlink_to(home)
    assert removal_refusal(link, home) == "is a symlink"
    ok = home / ".voiced"
    ok.mkdir()
    assert removal_refusal(ok, home) is None


def test_remove_targets_requires_pyvenv_cfg(settings: Settings, tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".voiced").mkdir(parents=True)
    p = plan_install(settings, venv_dir=home / ".voiced", home=home, uv=None)
    targets = uninstall_targets(p, settings, logs=False, models=False, config=False)
    said: list[str] = []
    assert remove_targets(targets, p, said.append) == 0
    assert (home / ".voiced").exists()
    assert "no pyvenv.cfg" in said[0]
    (home / ".voiced" / "pyvenv.cfg").write_text("home = /x\n")
    assert remove_targets(targets, p, said.append) == 1
    assert not (home / ".voiced").exists()


def test_remove_targets_never_deletes_home(settings: Settings, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "keep.txt").write_text("x")
    (home / "pyvenv.cfg").write_text("x")  # even if it looks like a venv
    p = plan_install(settings, venv_dir=home, home=home, config_file=home / "config.toml", uv=None)
    said: list[str] = []
    targets = uninstall_targets(p, settings, logs=False, models=False, config=True)
    assert targets.trees == [home] and p.staging_dir == home / "launchd"
    remove_targets(targets, p, said.append)
    assert (home / "keep.txt").exists()
    assert any("is the home directory" in line for line in said)


def test_remove_logs_only_touches_voiced_files(settings: Settings, tmp_path: Path) -> None:
    """``log_dir = ~/Library/Logs`` is legal; other apps' logs must survive."""
    home = tmp_path / "home"
    logs = home / "Library" / "Logs"
    logs.mkdir(parents=True)
    (logs / "OtherApp.log").write_text("keep")
    for name in ("stt.log", "stt.log.1", "tts.stderr.log", "watchdog.log", "metal-probe.json"):
        (logs / name).write_text("x")
    settings.server.log_dir = logs
    p = plan_install(settings, venv_dir=home / ".voiced", home=home, uv=None)
    targets = uninstall_targets(p, settings, logs=True, models=False, config=False)
    assert logs not in targets.trees and logs in targets.prune_dirs
    remove_targets(targets, p, lambda _: None)
    assert sorted(x.name for x in logs.iterdir()) == ["OtherApp.log"]


def test_remove_logs_prunes_empty_dir(settings: Settings, tmp_path: Path) -> None:
    home = tmp_path / "home"
    logs = home / "Library" / "Logs" / "voiced"
    logs.mkdir(parents=True)
    (logs / "stt.log").write_text("x")
    settings.server.log_dir = logs
    p = plan_install(settings, venv_dir=home / ".voiced", home=home, uv=None)
    remove_targets(
        uninstall_targets(p, settings, logs=True, models=False, config=False), p, lambda _: None
    )
    assert not logs.exists()


def test_remove_config_only_touches_generated_files(settings: Settings, tmp_path: Path) -> None:
    """``VOICED_CONFIG=~/config.toml`` makes the staging dir ``~/launchd``; keep the rest."""
    home = tmp_path / "home"
    staging = home / "launchd"
    staging.mkdir(parents=True)
    (home / "config.toml").write_text("x")
    (staging / "mine.plist").write_text("keep")
    p = plan_install(
        settings, venv_dir=home / ".voiced", home=home, config_file=home / "config.toml"
    )
    for name in p.labels:
        p.staged_plist(name).write_text("x")
    p.install_script_path.write_text("x")
    targets = uninstall_targets(p, settings, logs=False, models=False, config=True)
    assert home not in targets.trees and staging not in targets.trees
    remove_targets(targets, p, lambda _: None)
    assert not (home / "config.toml").exists()
    assert sorted(x.name for x in staging.iterdir()) == ["mine.plist"]
    assert home.exists()


def test_uninstall_targets_models(settings: Settings, tmp_path: Path) -> None:
    settings.install.hf_home = tmp_path / "hf"
    p = plan_install(settings, home=tmp_path)
    targets = uninstall_targets(p, settings, logs=False, models=True, config=False)
    assert tmp_path / "hf" / "hub" / "models--mlx-community--Kokoro-82M-bf16" in targets.trees
    assert tmp_path / "hf" / "hub" / "models--mlx-community--parakeet-tdt-0.6b-v3" in targets.trees


@pytest.mark.parametrize(
    ("argv", "root", "message"),
    [
        (["install", "--source", "."], True, "not with sudo"),
        (["install"], False, "--source ~/voiced"),
    ],
)
def test_cli_install_refuses_unsafe_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    root: bool,
    message: str,
) -> None:
    """Root daemons running a user-writable venv would hand root to anything
    that can write to it; and an unpublished name from PyPI is whatever
    someone else registers under it. Both stop before anything is written."""
    from voiced import cli_lifecycle
    from voiced.launchd import install

    monkeypatch.setenv("VOICED_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setattr(cli_lifecycle, "is_root", lambda: root)
    monkeypatch.setattr(install, "repo_root", lambda: None)  # as after `uv tool install`
    assert main([*argv, "--dry-run"]) == 2
    assert message in capsys.readouterr().out
    assert not (tmp_path / "config.toml").exists()


def test_run_install_tightens_an_existing_readable_config(
    settings: Settings, tmp_path: Path
) -> None:
    """Older installs created the config 0644; re-installing makes it 0600."""
    p = _tmp_plan(settings, tmp_path)
    p.config_file.parent.mkdir(parents=True, exist_ok=True)
    p.config_file.write_text('[server]\napi_key = "k"\n')
    p.config_file.chmod(0o644)
    said: list[str] = []
    run_install(p, runner=lambda argv: CommandResult(0), dry_run=True, say=said.append)
    assert stat.S_IMODE(p.config_file.stat().st_mode) == 0o644, "dry run changes nothing"
    assert any("would tighten" in line for line in said)
    p.voiced_bin.parent.mkdir(parents=True)
    p.voiced_bin.write_text("")
    run_install(p, runner=lambda argv: CommandResult(0), skip_models=True, say=said.append)
    assert stat.S_IMODE(p.config_file.stat().st_mode) == 0o600


def test_plan_uses_the_real_account_not_logname(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LOGNAME and USER are whatever the environment says; "System
    Administrator" is uid 0, so believing it would write a root UserName."""
    monkeypatch.setenv("LOGNAME", "System Administrator")
    monkeypatch.setenv("USER", "System Administrator")
    p = plan_install(settings, home=tmp_path, staging_dir=tmp_path / "s", uv=None)
    assert p.user == pwd.getpwuid(os.getuid()).pw_name != "System Administrator"
    assert f"<string>{p.user}</string>" in p.render("stt").decode()
