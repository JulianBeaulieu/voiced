"""Installer plan, golden plists and scripts, dry-run, and uninstall safety."""

from __future__ import annotations

import dataclasses
import inspect
import json
import os
import plistlib
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

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
    run_install,
    uninstall_targets,
    write_default_config,
    write_staging,
)
from voiced.layout import Manifest, install_lock, read_manifest, sha256_file, write_manifest

GOLDEN = Path(__file__).parent / "golden"
HOME = Path("/Users/someone")


def golden_plan(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> InstallPlan:
    """Fixed user, home, label prefix and paths so the artifacts are reproducible."""
    monkeypatch.setattr(install_module, "installing_user", lambda: "someone")
    settings.launchd.label_prefix = "org.example.voiced"
    settings.install.hf_home = None
    return plan_install(
        settings,
        state_dir=HOME / ".voiced",
        home=HOME,
        staging_dir=HOME / ".config/voiced/launchd",
        config_file=HOME / ".config/voiced/config.toml",
        prefix="/opt/homebrew/Cellar/voiced/0.1.0/libexec",
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
    assert parsed["RunAtLoad"] is True
    # Respawned only while the entry point resolves (not after `brew uninstall`).
    assert parsed["KeepAlive"] == {"PathState": {str(HOME / ".voiced/bin/voiced"): True}}
    assert parsed["EnvironmentVariables"]["VOICED_CONFIG"] == str(plan.config_file)
    assert "StandardOutPath" not in parsed and "StandardErrorPath" not in parsed
    stdio = parsed["EnvironmentVariables"]["VOICED_STDIO_LOG"]
    assert stdio == str(plan.log_dir / service)


def test_install_script_matches_golden(plan: InstallPlan) -> None:
    _compare(plan.sudo_install_script(), "install-daemons.sh")


def test_uninstall_script_matches_golden(plan: InstallPlan) -> None:
    _compare(plan.sudo_uninstall_script(), "uninstall-daemons.sh")


def test_metal_probe_matches_golden(plan: InstallPlan) -> None:
    probe = plan.metal_probe(HOME / ".voiced/bin/python")
    _compare(probe.script, "metal-probe.sh")
    _compare(probe.plist.decode(), "metalprobe.plist")
    assert plistlib.loads(probe.plist)["KeepAlive"] is False
    assert "StandardErrorPath" not in plistlib.loads(probe.plist)
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
    p = plan_install(settings, state_dir=venv, home=tmp_path, uv=None)
    summary = plan_summary(p)
    assert f"state dir      {venv}" in summary
    assert "warning" not in summary


def test_default_install_runner_streams() -> None:
    assert inspect.signature(run_install).parameters["runner"].default is run_streaming


def test_reinstall_waits_for_bootout() -> None:
    lines = daemon_install_lines("a.b", Path("/s/a.b.plist"))
    assert lines[0] == "launchctl bootout system/a.b 2>/dev/null || true"
    assert "launchctl print system/a.b >/dev/null 2>&1 || break" in lines[1]
    assert lines.index(next(line for line in lines if "bootstrap" in line)) > 1


def _download_env(p: InstallPlan) -> dict[str, str]:
    env: dict[str, str] = json.loads(p.model_download_command()[-1])
    return env


def test_hf_home_resolved_once_for_plist_and_download(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def make() -> InstallPlan:
        return plan_install(settings, home=tmp_path, staging_dir=tmp_path, uv=None)

    monkeypatch.delenv("HF_HOME", raising=False)
    p = make()
    assert "HF_HOME" not in p.environment_variables()
    assert "HF_HOME" not in _download_env(p)

    monkeypatch.setenv("HF_HOME", str(tmp_path / "from-env"))
    p = make()  # the shell's HF_HOME reaches the daemons and the download alike
    assert p.environment_variables()["HF_HOME"] == str(tmp_path / "from-env")
    assert _download_env(p)["HF_HOME"] == str(tmp_path / "from-env")

    settings.install.hf_home = tmp_path / "from-config"
    p = make()  # the config key wins over the environment
    assert p.environment_variables()["HF_HOME"] == str(tmp_path / "from-config")
    assert _download_env(p)["HF_HOME"] == str(tmp_path / "from-config")

    settings.install.hf_home = None
    monkeypatch.setenv("HF_HOME", "relative/cache")
    monkeypatch.chdir(tmp_path)
    p = make()  # a relative HF_HOME is made absolute: the daemon's cwd differs
    assert p.environment_variables()["HF_HOME"] == str(tmp_path / "relative/cache")


def test_espeak_override_reaches_daemons_and_download(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The override `voiced doctor` suggests must be in the daemons' environment too."""

    def make() -> InstallPlan:
        return plan_install(settings, home=tmp_path, staging_dir=tmp_path, uv=None)

    p = make()
    assert not {"VOICED_ESPEAK_LIBRARY", "VOICED_ESPEAK_DATA"} & set(p.environment_variables())

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
    assert {k: _download_env(p)[k] for k in expected} == expected
    assert f"VOICED_ESPEAK_LIBRARY {expected['VOICED_ESPEAK_LIBRARY']}" in plan_summary(p)


def test_build_commands_use_the_locks_and_a_managed_python(
    settings: Settings, tmp_path: Path
) -> None:
    """--source: a fresh environment under envs/ on a uv-managed Python in the
    state dir, every dependency with its hash from the lock, then voiced
    without dependencies and with a hashed build backend."""
    checkout = tmp_path / "checkout"
    p = plan_install(
        settings, state_dir=tmp_path / "state", home=tmp_path, source=checkout, uv="/fake/uv"
    )
    assert p.environment.parent == tmp_path / "state" / "envs"
    assert p.origin == "cli"
    python = str(p.environment / "bin" / "python")
    assert p.build_commands() == [
        [
            "/usr/bin/env",
            f"UV_PYTHON_INSTALL_DIR={tmp_path / 'state' / 'python'}",
            "/fake/uv",
            "venv",
            "--managed-python",
            "--python",
            "3.12",
            str(p.environment),
        ],
        [
            *("/fake/uv", "pip", "install", "--python", python, "--no-deps"),
            *("--require-hashes", "-r", str(checkout / "src/voiced/requirements.lock")),
        ],
        [
            *("/fake/uv", "pip", "install", "--python", python, "--no-deps"),
            *("--build-constraints", str(checkout / "src/voiced/build-requirements.lock")),
            str(checkout),
        ],
    ]


def test_without_source_the_environment_is_the_running_one(settings: Settings) -> None:
    keg = "/opt/homebrew/Cellar/voiced/HEAD-afcb37e/libexec"
    brewed = plan_install(settings, prefix=keg)
    assert brewed.environment == Path("/opt/homebrew/opt/voiced/libexec")
    assert brewed.origin == "homebrew" and brewed.build_commands() == []
    assert plan_install(settings).environment == Path(sys.prefix)  # this test venv
    assert plan_install(settings).origin == "cli"


def test_stable_environment_maps_only_a_voiced_keg() -> None:
    from voiced.layout import stable_environment

    assert stable_environment("/usr/local/Cellar/voiced/0.1.0/libexec") == (
        Path("/usr/local/opt/voiced/libexec"),
        "homebrew",
    )
    for other in (
        "/Users/u/.voiced",
        "/opt/homebrew/Cellar/other/1.0/libexec",
        "/opt/homebrew/opt/voiced/libexec",
    ):
        assert stable_environment(other) == (Path(other), "cli")


def test_plists_are_the_same_for_every_environment(settings: Settings, tmp_path: Path) -> None:
    """Switching between Homebrew, a source build and the app is repointing
    bin/voiced; the plists, and so the root step, never change."""
    common = {"state_dir": tmp_path / "state", "home": tmp_path, "uv": "/fake/uv"}
    brewed = plan_install(settings, prefix="/opt/homebrew/Cellar/voiced/1/libexec", **common)
    built = plan_install(settings, source=tmp_path, **common)
    assert brewed.plists() == built.plists()


def test_model_download_runs_in_the_new_environment(plan: InstallPlan) -> None:
    """The environment travels as JSON, so a state path with ``=`` in it is
    not taken for an assignment the way ``/usr/bin/env`` would."""
    plan = dataclasses.replace(plan, environment=Path("/tmp/a=b/env"))
    cmd = plan.model_download_command()
    assert cmd[0] == "/tmp/a=b/env/bin/python"
    assert "engine.load()" in cmd[2]
    assert json.loads(cmd[3])["VOICED_CONFIG"] == str(plan.config_file)


def test_shipped_lock_matches_uv_lock() -> None:
    """Every install path installs from these files; they must say what
    uv.lock says. Regenerate with the commands in CONTRIBUTING.md."""
    uv = shutil.which("uv")
    root = Path(__file__).resolve().parents[2]
    if uv is None or not (root / "uv.lock").is_file():
        pytest.skip("needs uv and a checkout")
    exports = {
        "src/voiced/requirements.lock": ["--no-dev", "--extra", "stt", "--extra", "tts"],
        "src/voiced/build-requirements.lock": ["--only-group", "build"],
    }
    for path, flags in exports.items():
        argv = [uv, "export", "--frozen", *flags, "--no-emit-project", "--no-header", "-q"]
        out = subprocess.run(argv, cwd=root, capture_output=True, text=True, check=True).stdout
        assert out == (root / path).read_text(), f"{path} is stale"
    assert install_module.packaged_lock().is_file()


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


def test_remove_logs_only_touches_voiced_files(settings: Settings, tmp_path: Path) -> None:
    """``log_dir = ~/Library/Logs`` is legal; other apps' logs must survive."""
    home = tmp_path / "home"
    logs = home / "Library" / "Logs"
    logs.mkdir(parents=True)
    (logs / "OtherApp.log").write_text("keep")
    for name in ("stt.log", "stt.log.1", "tts.stderr.log", "watchdog.log", "metal-probe.json"):
        (logs / name).write_text("x")
    settings.server.log_dir = logs
    p = plan_install(settings, state_dir=home / ".voiced", home=home, uv=None)
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
    p = plan_install(settings, state_dir=home / ".voiced", home=home, uv=None)
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
        settings, state_dir=home / ".voiced", home=home, config_file=home / "config.toml"
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


# The state directory (ADR 0017) -----------------------------------------------


def _environment(path: Path, version: str = "0.0.1") -> Path:
    """A minimal environment: bin/voiced, bin/python, a dist-info."""
    (path / "bin").mkdir(parents=True)
    for name in ("voiced", "python"):
        (path / "bin" / name).write_text("#!/bin/sh\n")
        (path / "bin" / name).chmod(0o755)
    (path / "lib/python3.12/site-packages" / f"voiced-{version}.dist-info").mkdir(parents=True)
    (path / "pyvenv.cfg").write_text("home = /x\n")
    return path


def _plan(settings: Settings, tmp_path: Path, **kwargs: Any) -> InstallPlan:
    settings.server.log_dir = tmp_path / "logs"
    kwargs.setdefault("prefix", tmp_path / "env")
    return plan_install(
        settings,
        state_dir=tmp_path / "state",
        home=tmp_path,
        staging_dir=tmp_path / "stage",
        config_file=tmp_path / "config.toml",
        uv="/fake/uv",
        **kwargs,
    )


def _check_ok(argv: Sequence[str]) -> CommandResult:
    """The engine check of an environment that has them."""
    return CommandResult(0, "voiced 0.0.1\n")


def _install(p: InstallPlan, said: list[str] | None = None) -> list[list[str]]:
    """Runs ``p``; returns every command, the engine check included."""
    calls: list[list[str]] = []

    def runner(argv: Sequence[str]) -> CommandResult:
        calls.append(list(argv))
        if argv[-1:] == [str(p.environment)]:  # uv venv
            _environment(p.environment)
        return CommandResult(0)

    def query(argv: Sequence[str]) -> CommandResult:
        calls.append(list(argv))
        return _check_ok(argv)

    run_install(p, runner=runner, query=query, say=(said if said is not None else []).append)
    return calls


def test_install_links_the_running_environment(settings: Settings, tmp_path: Path) -> None:
    env = _environment(tmp_path / "env")
    p = _plan(settings, tmp_path)
    calls = _install(p)
    assert [c[0] for c in calls] == [str(env / "bin/python")] * 2  # check, then models
    assert "import mlx.core" in calls[0][-1]
    assert "engine.load()" in calls[1][2]
    assert p.entry.is_symlink() and os.readlink(p.entry) == str(env / "bin" / "voiced")
    manifest = read_manifest(p.state_dir)
    assert manifest is not None
    assert (manifest.source, manifest.environment) == ("cli", env)
    assert manifest.lock_sha256 == sha256_file(install_module.packaged_lock())
    assert manifest.python == os.path.realpath(env / "bin" / "python")
    assert p.install_script_path.exists()
    assert sorted(x.name for x in p.state_dir.iterdir()) == ["bin", "install.json"]


def test_install_builds_a_source_environment(settings: Settings, tmp_path: Path) -> None:
    p = _plan(settings, tmp_path, source=tmp_path / "checkout")
    calls = _install(p)
    assert [c[3] if c[0] == "/usr/bin/env" else c[1] for c in calls[:3]] == [
        "venv",
        "pip",
        "pip",
    ]
    manifest = read_manifest(p.state_dir)
    assert manifest is not None and manifest.environment == p.environment
    assert manifest.version == "0.0.1"  # what the engine check printed
    assert p.entry.resolve() == (p.environment / "bin" / "voiced").resolve()


def test_switching_environments_repoints_and_prunes(settings: Settings, tmp_path: Path) -> None:
    """Homebrew -> source -> source: no refusal, no rm -rf of the state dir;
    the previous environment stays for daemons not yet restarted, older
    ones built by voiced go."""
    _environment(tmp_path / "env")
    _install(_plan(settings, tmp_path))
    first = _plan(settings, tmp_path, source=tmp_path / "checkout")
    _install(first)
    said: list[str] = []
    second = _plan(settings, tmp_path, source=tmp_path / "checkout")
    assert second.environment != first.environment
    _install(second, said)
    assert first.environment.exists()  # the previous one
    assert any("voiced restart all" in line for line in said)
    third = _plan(settings, tmp_path, prefix=tmp_path / "env")
    _install(third)
    assert not first.environment.exists() and second.environment.exists()
    assert (tmp_path / "env").exists(), "an environment voiced did not build is never removed"
    assert os.readlink(third.entry) == str(tmp_path / "env" / "bin" / "voiced")


def test_install_refuses_a_directory_that_is_not_voiceds(
    settings: Settings, tmp_path: Path
) -> None:
    """A state dir with someone else's files is refused before anything is
    written, so that uninstall can never mistake it for voiced's."""
    _environment(tmp_path / "env")
    p = _plan(settings, tmp_path)
    p.state_dir.mkdir()
    (p.state_dir / "precious.txt").write_text("mine")
    with pytest.raises(InstallError, match=r"precious\.txt"):
        _install(p)
    assert sorted(x.name for x in p.state_dir.iterdir()) == ["precious.txt"]
    assert not p.config_file.exists()
    (p.state_dir / "precious.txt").unlink()
    (p.state_dir / "control.sock").write_text("")  # left by a watchdog: allowed
    _install(p)


def test_install_adopts_and_replaces_a_legacy_venv(settings: Settings, tmp_path: Path) -> None:
    """~/.voiced as a venv (before ADR 0017) is adopted; the first switch away
    removes the venv's files and nothing else."""
    _environment(tmp_path / "env")
    p = _plan(settings, tmp_path)
    legacy = _environment(p.state_dir)
    (legacy / "include").mkdir()
    (legacy / "notes.txt").write_text("mine")
    said: list[str] = []
    _install(p, said)
    assert sorted(x.name for x in legacy.iterdir()) == ["bin", "install.json", "notes.txt"]
    assert [x.name for x in (legacy / "bin").iterdir()] == ["voiced"]
    assert p.entry.is_symlink()
    assert any("before ADR 0017" in line for line in said)


def test_legacy_venv_installing_itself_keeps_its_entry_point(
    settings: Settings, tmp_path: Path
) -> None:
    """`~/.voiced/bin/voiced install` from a legacy venv: the environment is
    the state dir itself, so there is nothing to link and nothing to remove."""
    settings.server.log_dir = tmp_path / "logs"
    state = _environment(tmp_path / "state")
    p = plan_install(
        settings,
        state_dir=state,
        home=tmp_path,
        staging_dir=tmp_path / "stage",
        config_file=tmp_path / "c.toml",
        prefix=state,
    )
    _install(p)
    assert not p.entry.is_symlink() and (state / "lib").is_dir()
    manifest = read_manifest(state)
    assert manifest is not None and manifest.environment == state


def test_run_install_dry_run_touches_nothing(settings: Settings, tmp_path: Path) -> None:
    p = _plan(settings, tmp_path, source=tmp_path / "checkout")
    said: list[str] = []

    def runner(argv: Sequence[str]) -> CommandResult:
        pytest.fail("dry run must not execute anything")

    run_install(p, runner=runner, dry_run=True, say=said.append)
    assert not p.config_file.exists()
    assert not p.staging_dir.exists()
    assert not p.state_dir.exists()
    assert not (tmp_path / "logs").exists()
    text = "\n".join(said)
    assert f"would create {p.config_file}" in text
    assert "/fake/uv venv --managed-python" in text
    assert f"would link {p.entry}" in text
    assert f"would write the plists and scripts to {p.staging_dir}" in text


def test_run_install_fails_if_the_environment_has_no_entry_point(
    settings: Settings, tmp_path: Path
) -> None:
    p = _plan(settings, tmp_path)
    with pytest.raises(InstallError, match="missing"):
        run_install(p, runner=lambda argv: CommandResult(0), say=lambda _: None)
    assert not p.entry.exists()


def test_install_refuses_an_environment_without_the_engines(
    settings: Settings, tmp_path: Path
) -> None:
    """`uv tool install ~/voiced` has no extras; linking it would leave
    daemons that cannot load a model."""
    _environment(tmp_path / "env")
    p = _plan(settings, tmp_path)
    with pytest.raises(InstallError, match="--source"):
        run_install(p, runner=pytest.fail, query=lambda argv: CommandResult(1), say=lambda _: None)
    assert not p.entry.exists()


def test_run_install_stops_on_failure(settings: Settings, tmp_path: Path) -> None:
    p = _plan(settings, tmp_path, source=tmp_path / "checkout")
    with pytest.raises(InstallError, match="boom"):
        run_install(p, runner=lambda argv: CommandResult(1, "", "boom"), say=lambda _: None)
    assert not p.install_script_path.exists()
    # The directory was claimed first, so the next install still knows it.
    manifest = read_manifest(p.state_dir)
    assert manifest is not None and manifest.environment is None


def test_source_build_needs_uv(settings: Settings, tmp_path: Path) -> None:
    p = dataclasses.replace(_plan(settings, tmp_path, source=tmp_path), uv=None)
    with pytest.raises(InstallError, match="brew install uv"):
        run_install(p, runner=lambda argv: CommandResult(0), dry_run=True, say=lambda _: None)


def test_run_install_tightens_an_existing_readable_config(
    settings: Settings, tmp_path: Path
) -> None:
    """Older installs created the config 0644; re-installing makes it 0600."""
    _environment(tmp_path / "env")
    p = _plan(settings, tmp_path)
    p.config_file.write_text('[server]\napi_key = "k"\n')
    p.config_file.chmod(0o644)
    said: list[str] = []
    run_install(p, runner=lambda argv: CommandResult(0), dry_run=True, say=said.append)
    assert stat.S_IMODE(p.config_file.stat().st_mode) == 0o644, "dry run changes nothing"
    assert any("would tighten" in line for line in said)
    run_install(p, runner=lambda argv: CommandResult(0), skip_models=True, say=said.append)
    assert stat.S_IMODE(p.config_file.stat().st_mode) == 0o600


def test_cli_dry_run_prints_everything_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("VOICED_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.setenv("VOICED_SERVER__LOG_DIR", str(tmp_path / "logs"))
    staging = tmp_path / "stage"
    argv = ["install", "--dry-run", "--state-dir", str(tmp_path / "state")]
    code = main([*argv, "--staging-dir", str(staging), "--source", str(tmp_path)])
    out = capsys.readouterr().out
    labels = Settings().launchd
    assert code == 0
    assert not staging.exists() and not (tmp_path / "state").exists()
    assert not (tmp_path / "logs").exists()
    assert "dry run" in out
    assert f"<string>{labels.label('watchdog')}</string>" in out
    assert f"<string>{pwd.getpwuid(os.getuid()).pw_name}</string>" in out
    assert f"{staging / 'install-daemons.sh'} (would be written)" in out
    assert "--require-hashes" in out and "--build-constraints" in out


def test_cli_install_refuses_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Root daemons running a user-writable environment would hand root to
    anything that can write to it."""
    from voiced import cli_lifecycle

    monkeypatch.setenv("VOICED_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setattr(cli_lifecycle, "is_root", lambda: True)
    assert main(["install", "--dry-run"]) == 2
    assert "not with sudo" in capsys.readouterr().out
    assert not (tmp_path / "config.toml").exists()


def test_install_refuses_a_users_own_venv(settings: Settings, tmp_path: Path) -> None:
    """A venv without voiced in it is not a legacy install: refused, and
    nothing in it is touched by install or uninstall."""
    _environment(tmp_path / "env")
    p = _plan(settings, tmp_path)
    mine = _environment(p.state_dir)
    shutil.rmtree(next(mine.glob("lib/python3*/site-packages/voiced-*.dist-info")))
    before = sorted(str(x.relative_to(mine)) for x in mine.rglob("*"))
    with pytest.raises(InstallError, match="not voiced's"):
        _install(p)
    targets = uninstall_targets(p, settings, logs=False, models=False, config=False)
    assert remove_targets(targets, p, lambda _: None) == 0
    assert sorted(str(x.relative_to(mine)) for x in mine.rglob("*")) == before


def test_legacy_venv_through_a_symlinked_state_dir_is_refused(
    settings: Settings, tmp_path: Path
) -> None:
    """~/.voiced -> /elsewhere, a legacy venv: refused like any symlinked
    state dir, untouched; installing with the target's own path works."""
    real = _environment(tmp_path / "real")
    (tmp_path / "state").symlink_to(real)
    p = _plan(settings, tmp_path, prefix=real)
    with pytest.raises(InstallError, match="symlink"):
        _install(p)
    assert not (real / "install.json").exists()
    direct = dataclasses.replace(p, state_dir=real)
    _install(direct)
    assert not (real / "bin" / "voiced").is_symlink() and (real / "lib").is_dir()


def test_install_waits_for_another_install(settings: Settings, tmp_path: Path) -> None:
    """The app and the CLI never relink or prune under each other."""
    _environment(tmp_path / "env")
    p = _plan(settings, tmp_path)
    p.state_dir.mkdir()
    said: list[str] = []
    with install_lock(p.state_dir):
        thread = threading.Thread(target=_install, args=(p, said))
        thread.start()
        for _ in range(100):
            if said:
                break
            time.sleep(0.05)
        assert said and said[0].startswith("waiting for another install")
        assert not p.entry.exists()
    thread.join(10)
    assert p.entry.is_symlink()


def test_install_reads_the_manifest_again_under_the_lock(
    settings: Settings, tmp_path: Path
) -> None:
    """What an install that held the lock left decides what this one keeps:
    its environment is this one's rollback, not pruned."""
    _environment(tmp_path / "env")
    p = _plan(settings, tmp_path)
    p.state_dir.mkdir()
    said: list[str] = []
    with install_lock(p.state_dir):
        thread = threading.Thread(target=_install, args=(p, said))
        thread.start()
        for _ in range(100):
            if said:
                break
            time.sleep(0.05)
        # Meanwhile another install (the app's) finished:
        other = _environment(p.state_dir / "envs" / "20260101T000000")
        write_manifest(p.state_dir, Manifest(source="app", environment=other))
    thread.join(10)
    assert other.exists()
    assert any(f"changed from {other}" in line for line in said)


def test_prune_keeps_what_voiced_did_not_build(settings: Settings, tmp_path: Path) -> None:
    _environment(tmp_path / "env")
    p = _plan(settings, tmp_path)
    _install(p)
    envs = p.state_dir / "envs"
    (envs / "not-a-venv").mkdir(parents=True)
    _environment(envs / "20260101T000000.partial")  # the app, still building
    old = _environment(envs / "20250101T000000")
    _install(_plan(settings, tmp_path, source=tmp_path / "checkout"))
    left = {x.name for x in envs.iterdir()}
    assert {"20260101T000000.partial", "not-a-venv"} <= left
    assert not old.exists()


def test_a_failed_model_download_changes_nothing(settings: Settings, tmp_path: Path) -> None:
    """The link, the manifest and the environments switch only after the
    models are in."""
    _environment(tmp_path / "env")
    _install(_plan(settings, tmp_path))
    p = _plan(settings, tmp_path, source=tmp_path / "checkout")
    before = (os.readlink(p.entry), (p.state_dir / "install.json").read_text())

    def runner(argv: Sequence[str]) -> CommandResult:
        if argv[-1:] == [str(p.environment)]:
            _environment(p.environment)
        return CommandResult(1, "", "no network") if "-c" in argv else CommandResult(0)

    with pytest.raises(InstallError, match="model download"):
        run_install(p, runner=runner, query=_check_ok, say=lambda _: None)
    assert (os.readlink(p.entry), (p.state_dir / "install.json").read_text()) == before


def test_reinstalling_the_active_environment_keeps_its_origin_and_rollback(
    settings: Settings, tmp_path: Path
) -> None:
    """`voiced install` re-run from an app environment stays the app's, and
    the environment before it (the rollback) is not pruned."""
    _install(_plan(settings, tmp_path, source=tmp_path / "checkout"))
    p_app = _plan(settings, tmp_path, source=tmp_path / "checkout", origin="app")
    first = read_manifest(p_app.state_dir)
    _install(p_app)
    again = _plan(settings, tmp_path, prefix=p_app.environment)
    assert again.origin == "cli"
    _install(again)
    manifest = read_manifest(p_app.state_dir)
    assert manifest is not None and manifest.source == "app"
    assert first is not None and first.environment is not None and first.environment.exists()


def test_build_system_matches_the_build_group() -> None:
    """[build-system] requires what the hashed build lock pins."""
    data = tomllib.loads((Path(__file__).resolve().parents[2] / "pyproject.toml").read_text())
    assert data["build-system"]["requires"] == data["dependency-groups"]["build"]


# Uninstall ---------------------------------------------------------------------


def test_uninstall_removes_only_what_voiced_owns(settings: Settings, tmp_path: Path) -> None:
    """A file the user put into the state dir survives, and so does the dir."""
    _environment(tmp_path / "env")
    _install(_plan(settings, tmp_path))
    p = _plan(settings, tmp_path, source=tmp_path / "checkout")
    _install(p)
    (p.state_dir / "python" / "cpython-3.12").mkdir(parents=True)
    (p.state_dir / "control.sock").write_text("")
    (p.state_dir / "precious.txt").write_text("mine")
    said: list[str] = []
    targets = uninstall_targets(p, settings, logs=False, models=False, config=False)
    remove_targets(targets, p, said.append)
    assert sorted(x.name for x in p.state_dir.iterdir()) == ["precious.txt"]
    assert (tmp_path / "env" / "bin" / "voiced").exists(), "not voiced's to remove"
    assert any("kept" in line and "precious.txt" in line for line in said)
    (p.state_dir / "precious.txt").unlink()
    _install(_plan(settings, tmp_path))
    remove_targets(
        uninstall_targets(p, settings, logs=False, models=False, config=False), p, said.append
    )
    assert not p.state_dir.exists()


# What the app writes into ~/.voiced, holding the install lock, before it
# builds anything there (ADR 0018, Flow step 2).
APP_MARKER = '{"source": "app", "environment": null}'


def _app_build(state: Path, *, environment: bool = True) -> Path:
    """What the app leaves before `voiced install`: the marker, uv's Python
    and the environment it built; returns the environment."""
    state.mkdir(parents=True)
    (state / "install.json").write_text(APP_MARKER)
    installation = state / "python" / "cpython-3.12.15-macos-aarch64-none"
    (installation / "bin").mkdir(parents=True)
    (installation / "bin" / "python3").write_text("")
    (state / "python" / ".lock").write_text("")
    (state / "envs").mkdir()
    env = state / "envs" / "20261018T120000"
    return _environment(env) if environment else env


def test_the_app_marker_is_a_manifest() -> None:
    assert Manifest.from_json(json.loads(APP_MARKER)) == Manifest(source="app", environment=None)


def test_install_adopts_what_the_app_built(settings: Settings, tmp_path: Path) -> None:
    env = _app_build(tmp_path / "state")
    (env.parent / "20261018T110000.partial").mkdir()  # another build, unfinished
    p = _plan(settings, tmp_path, prefix=env, origin="app")
    _install(p)
    manifest = read_manifest(p.state_dir)
    assert manifest is not None and manifest.source == "app"
    assert manifest.environment == env
    assert p.entry.resolve() == (env / "bin" / "voiced").resolve()
    assert (env.parent / "20261018T110000.partial").is_dir(), "never pruned"


def _uninstall(settings: Settings, tmp_path: Path) -> InstallPlan:
    p = _plan(settings, tmp_path)
    targets = uninstall_targets(p, settings, logs=False, models=False, config=False)
    assert targets.refused is None
    remove_targets(targets, p, lambda _: None)
    return p


def test_uninstall_removes_what_the_app_built_before_install(
    settings: Settings, tmp_path: Path
) -> None:
    """The app built Python and an environment and `voiced install` never
    ran (or failed): uninstall removes all of it, marker included."""
    _app_build(tmp_path / "state")
    (tmp_path / "state" / "control.sock").write_text("")
    p = _uninstall(settings, tmp_path)
    assert not p.state_dir.exists()


def test_uninstall_after_the_app_deleted_a_failed_build(settings: Settings, tmp_path: Path) -> None:
    """The environment step failed and the app removed its .partial: the
    marker, python/ and an empty envs/ are left, and all go."""
    _app_build(tmp_path / "state", environment=False)
    p = _uninstall(settings, tmp_path)
    assert not p.state_dir.exists()


def test_uninstall_removes_an_interrupted_build(settings: Settings, tmp_path: Path) -> None:
    """The app stopped while building: the .partial goes too (uninstall
    holds the lock the builder holds), and so does the state dir."""
    env = _app_build(tmp_path / "state", environment=False)
    _environment(env.with_name(env.name + ".partial"))
    (env.parent / "20261018T130000.partial").mkdir()  # before uv venv wrote anything
    p = _uninstall(settings, tmp_path)
    assert not p.state_dir.exists()


def test_uninstall_through_a_symlinked_state_dir_removes_nothing(
    settings: Settings, tmp_path: Path
) -> None:
    target = tmp_path / "elsewhere"
    _app_build(target)
    p = _plan(settings, tmp_path)
    p.state_dir.symlink_to(target)
    targets = uninstall_targets(p, settings, logs=False, models=False, config=False)
    assert targets.refused is not None
    assert f"symlink to {target.resolve()}" in targets.refused
    assert f"--state-dir {target.resolve()}" in targets.refused, "says what to do"
    assert remove_targets(targets, p, lambda _: None) == 0
    assert (target / "python" / ".lock").exists()
    assert (target / "envs" / "20261018T120000" / "pyvenv.cfg").exists()


def test_install_refuses_a_symlinked_state_dir(settings: Settings, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    p = _plan(settings, tmp_path)
    p.state_dir.symlink_to(target)
    with pytest.raises(InstallError) as refused:
        _install(p)
    assert f"symlink to {target.resolve()}" in str(refused.value)
    assert f"--state-dir {target.resolve()}" in str(refused.value), "says what to do"
    assert "move those files" not in str(refused.value)
    assert list(target.iterdir()) == []


def test_symlinked_state_dir_message_quotes_the_real_path(
    settings: Settings, tmp_path: Path
) -> None:
    target = tmp_path / "with space"
    target.mkdir()
    p = _plan(settings, tmp_path)
    p.state_dir.symlink_to(target)
    with pytest.raises(InstallError, match=re.escape(f"--state-dir '{target.resolve()}'")):
        _install(p)


def test_uninstall_refuses_a_directory_that_is_not_voiceds(
    settings: Settings, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    (state / "bin").mkdir(parents=True)
    (state / "bin" / "voiced").write_text("someone else's")
    p = _plan(settings, tmp_path)
    targets = uninstall_targets(p, settings, logs=False, models=False, config=False)
    said: list[str] = []
    assert targets.refused is not None and targets.all() == []
    assert remove_targets(targets, p, said.append) == 0
    assert (state / "bin" / "voiced").exists()
    assert "not voiced's" in said[0]


def test_uninstall_removes_a_legacy_venv(settings: Settings, tmp_path: Path) -> None:
    p = _plan(settings, tmp_path)
    legacy = _environment(p.state_dir)
    (legacy / "notes.txt").write_text("mine")
    remove_targets(
        uninstall_targets(p, settings, logs=False, models=False, config=False), p, lambda _: None
    )
    assert sorted(x.name for x in legacy.iterdir()) == ["notes.txt"]


def test_uninstall_never_deletes_home(settings: Settings, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "keep.txt").write_text("x")
    (home / "pyvenv.cfg").write_text("x")  # even if it looks like a venv
    p = plan_install(settings, state_dir=home, home=home, config_file=home / "c.toml", uv=None)
    remove_targets(
        uninstall_targets(p, settings, logs=False, models=False, config=True), p, lambda _: None
    )
    assert (home / "keep.txt").exists() and home.exists()


def test_cli_uninstall_names_homebrew_from_the_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from voiced.layout import Manifest, write_manifest

    state = tmp_path / "state"
    keg = Path("/opt/homebrew/opt/voiced/libexec")
    write_manifest(state, Manifest(source="homebrew", environment=keg))
    monkeypatch.setenv("VOICED_CONFIG", str(tmp_path / "config.toml"))
    runner = lambda argv: CommandResult(113, "", "not found")  # noqa: E731
    from voiced.cli import build_parser
    from voiced.cli_lifecycle import run_lifecycle

    args = build_parser().parse_args(["uninstall", "--yes", "--state-dir", str(state)])
    assert run_lifecycle(args, Settings(), runner, print) == 0
    out = capsys.readouterr().out
    assert f"voiced itself ({keg}) is Homebrew's" in out
    assert not state.exists()
