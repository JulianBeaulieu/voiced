"""Installer plan, the LaunchAgents, the Metal probe, dry-run, and uninstall safety."""

from __future__ import annotations

import dataclasses
import errno
import inspect
import json
import os
import plistlib
import pwd
import re
import shlex
import shutil
import socket
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

from tests import hf_cache_support as hf
from tests.scripts_support import load
from voiced.cli import main
from voiced.config import (
    DEFAULT_LAUNCHD_LABEL_PREFIX,
    SERVICES,
    Settings,
    load_settings,
)
from voiced.launchd import install as install_module
from voiced.launchd import ports as ports_module
from voiced.launchd.control import (
    CommandResult,
    legacy_removal_command,
    run_streaming,
    session_refusal,
)
from voiced.launchd.install import (
    BASE_PATH,
    InstallError,
    InstallPlan,
    install_agents,
    new_environment_dir,
    plan_install,
    plan_summary,
    removal_refusal,
    remove_agents,
    remove_targets,
    revision_line,
    run_install,
    run_metal_probe,
    uninstall_targets,
    write_default_config,
)
from voiced.launchd.ports import bind_error as real_bind_error
from voiced.launchd.ports import listener_pids as real_listener_pids
from voiced.launchd.progress import Progress
from voiced.layout import (
    Manifest,
    ManifestError,
    install_lock,
    read_manifest,
    sha256_file,
    write_manifest,
)
from voiced.models import locked, pinned_revisions

GOLDEN = Path(__file__).parent / "golden"
HOME = Path("/Users/someone")


def golden_plan(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> InstallPlan:
    """Fixed user, home, label prefix and paths so the probe script is reproducible."""
    monkeypatch.setattr(install_module, "installing_user", lambda: "someone")
    settings.launchd.label_prefix = "org.example.voiced"
    settings.install.hf_home = None
    return plan_install(
        settings,
        state_dir=HOME / ".voiced",
        home=HOME,
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


ENGINES_FOUND = CommandResult(0, "voiced 0.0.1\n")
"""The engine check of an environment that has them."""


class FakeLaunchd:
    """launchctl as it answers for the user's GUI domain: labels load from
    the plists bootstrapped, each with a pid. A job booted out stays listed
    for ``linger`` more prints, as launchd tears it down, and bootstrapping
    it meanwhile fails as launchd does (error 5). ``session`` False: nobody
    is logged in at the Mac. ``daemons``: labels loaded in the system domain.
    ``check``: what the engine check (any other command) returns."""

    def __init__(
        self,
        fail: set[str] | None = None,
        *,
        linger: int = 0,
        session: bool = True,
        daemons: set[str] | None = None,
        check: CommandResult = ENGINES_FOUND,
    ) -> None:
        self.check = check
        self.loaded: dict[str, int] = {}
        self.unloading: dict[str, int] = {}
        self.calls: list[list[str]] = []
        self.fail = fail or set()
        self.linger = linger
        self.session = session
        self.daemons = daemons or set()
        self.next_pid = 100

    def __call__(self, argv: Sequence[str]) -> CommandResult:
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] != "launchctl":
            return self.check
        verb, target = argv[1], argv[-1]
        gui = f"gui/{os.getuid()}"
        label = target.rsplit("/", 1)[-1]
        if verb == "print":
            if target == gui:
                return CommandResult(0 if self.session else 113)
            if target.startswith("system/"):
                return CommandResult(0) if label in self.daemons else CommandResult(113)
            if self.unloading.get(label, 0) > 0:
                self.unloading[label] -= 1
                return CommandResult(0, f"{target} = {{\n\tstate = exiting\n}}")
            if label not in self.loaded:
                return CommandResult(113, "", f'Could not find service "{label}"')
            return CommandResult(
                0, f"{target} = {{\n\tstate = running\n\tpid = {self.loaded[label]}\n}}"
            )
        if verb == "bootout":
            if self.loaded.pop(label, None) is None:
                return CommandResult(3)
            self.unloading[label] = self.linger
            return CommandResult(0)
        if verb == "bootstrap":
            assert argv[2] == gui
            label = plistlib.loads(Path(target).read_bytes())["Label"]
            if label in self.fail or self.unloading.get(label, 0) > 0:
                return CommandResult(5, "", "Bootstrap failed: 5: Input/output error")
            self.next_pid += 1
            self.loaded[label] = self.next_pid
            return CommandResult(0)
        raise AssertionError(argv)

    def verbs(self) -> list[tuple[str, str]]:
        """(verb, label), or for bootstrap (verb, plist path)."""
        return [
            (c[1], c[-1] if c[1] == "bootstrap" else c[-1].rsplit("/", 1)[-1])
            for c in self.calls
            if c[0] == "launchctl"
        ]


def test_agent_plists_run_what_install_links(settings: Settings, tmp_path: Path) -> None:
    """Every key the services need, in the user's domain: the entry point
    with its command, kept alive while it exists, the config path and the
    log prefix, the app's bundle id; no UserName, no log paths for launchd
    to open."""
    home = tmp_path / "home"
    settings.server.log_dir = tmp_path / "custom-logs"
    settings.install.hf_home = tmp_path / "hf"
    plan = plan_install(
        settings,
        state_dir=home / ".voiced",
        home=home,
        config_file=home / ".config/voiced/config.toml",
        uv=None,
    )
    for service in SERVICES:
        rendered = plistlib.loads(plan.agent_plist(service))
        command = ["watchdog"] if service == "watchdog" else ["serve", service]
        assert rendered == {
            "Label": f"{DEFAULT_LAUNCHD_LABEL_PREFIX}.{service}",
            "ProgramArguments": [str(plan.entry), *command],
            "RunAtLoad": True,
            # Respawned only while the entry point resolves (not after `brew uninstall`).
            "KeepAlive": {"PathState": {str(plan.entry): True}},
            "ThrottleInterval": 10,
            "ProcessType": "Interactive",
            "WorkingDirectory": str(home),
            "EnvironmentVariables": {
                "HOME": str(home),
                "PATH": f"{home}/.voiced/bin:{BASE_PATH}",
                "VOICED_CONFIG": str(home / ".config/voiced/config.toml"),
                "PYTHONUNBUFFERED": "1",
                "VOICED_STDIO_LOG": str(tmp_path / "custom-logs" / service),
            },
            "AssociatedBundleIdentifiers": [f"{DEFAULT_LAUNCHD_LABEL_PREFIX}.menubar"],
        }
        assert plan.agent_path(service) == home / f"Library/LaunchAgents/{rendered['Label']}.plist"


def test_agents_follow_a_custom_layout(settings: Settings, tmp_path: Path) -> None:
    """No root renders them any more, so a scratch state dir, config and
    label prefix get agents of their own (a test harness beside the real
    install, ADR 0003)."""
    settings.launchd.label_prefix = "org.example.voicedtest"
    p = _plan(settings, tmp_path)
    watchdog = plistlib.loads(p.agent_plist("watchdog"))
    assert watchdog["Label"] == "org.example.voicedtest.watchdog"
    assert watchdog["ProgramArguments"] == [str(tmp_path / "state/bin/voiced"), "watchdog"]
    assert watchdog["EnvironmentVariables"]["VOICED_CONFIG"] == str(tmp_path / "config.toml")


def test_install_loads_the_agents_as_the_user(settings: Settings, tmp_path: Path) -> None:
    _environment(tmp_path / "env")
    p = _plan(settings, tmp_path)
    launchd = FakeLaunchd()
    said: list[str] = []
    run_install(p, runner=lambda argv: CommandResult(0), query=launchd, say=said.append)
    labels = [p.labels[s] for s in SERVICES]
    assert sorted(launchd.loaded) == sorted(labels)
    assert [v for v in launchd.verbs() if v[0] == "bootstrap"] == [
        ("bootstrap", str(p.agent_path(s))) for s in SERVICES
    ]
    for service in SERVICES:
        path = p.agent_path(service)
        assert path.read_bytes() == p.agent_plist(service)
        assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert not any(line.startswith("sudo") for line in said)


def test_install_leaves_up_to_date_agents_running(settings: Settings, tmp_path: Path) -> None:
    """A second install changes nothing that runs; a changed plist is booted
    out and loaded again, a label booted out by hand is loaded again."""
    _environment(tmp_path / "env")
    p = _plan(settings, tmp_path)
    launchd = FakeLaunchd()
    install_agents(p, launchd, lambda _: None)
    pids = dict(launchd.loaded)
    launchd.calls.clear()
    install_agents(p, launchd, lambda _: None)
    assert launchd.loaded == pids
    assert {verb for verb, _ in launchd.verbs()} == {"print"}

    p.agent_path("tts").write_bytes(b"<plist/>")
    del launchd.loaded[p.labels["stt"]]
    launchd.calls.clear()
    install_agents(p, launchd, lambda _: None)
    changed = [v for v in launchd.verbs() if v[0] != "print"]
    assert changed == [
        ("bootstrap", str(p.agent_path("stt"))),
        ("bootout", p.labels["tts"]),
        ("bootstrap", str(p.agent_path("tts"))),
    ]
    assert launchd.loaded[p.labels["watchdog"]] == pids[p.labels["watchdog"]]
    assert p.agent_path("tts").read_bytes() == p.agent_plist("tts")


def test_a_label_that_fails_to_load_is_named(settings: Settings, tmp_path: Path) -> None:
    _environment(tmp_path / "env")
    p = _plan(settings, tmp_path)
    launchd = FakeLaunchd(fail={p.labels["tts"]})
    with pytest.raises(InstallError, match=f"not loaded: {re.escape(p.labels['tts'])}"):
        install_agents(p, launchd, lambda _: None)
    assert sorted(launchd.loaded) == sorted([p.labels["stt"], p.labels["watchdog"]])


def _daemon(directory: Path, label: str, user: str) -> None:
    plist = {"Label": label, "UserName": user, "ProgramArguments": ["/x/voiced", "watchdog"]}
    (directory / f"{label}.plist").write_bytes(plistlib.dumps(plist))


def test_install_refuses_while_old_launchdaemons_hold_the_labels(
    settings: Settings, tmp_path: Path, no_launchd: Path
) -> None:
    """Both would run and fight over the ports, and only root removes the
    daemons: the install stops before it writes anything, with the one line
    that does it (ADR 0035)."""
    _environment(tmp_path / "env")
    p = _plan(settings, tmp_path)
    for service in SERVICES:
        _daemon(no_launchd, p.labels[service], p.user)
    with pytest.raises(InstallError) as refused:
        run_install(p, runner=lambda argv: CommandResult(0), query=FakeLaunchd(), dry_run=True)
    line = legacy_removal_command([p.labels[s] for s in SERVICES])
    assert f"\n\n  {line}\n  voiced install" in str(refused.value)
    assert "the last time voiced needs a password" in str(refused.value)
    with pytest.raises(InstallError):
        run_install(p, runner=lambda argv: CommandResult(0), query=FakeLaunchd())
    assert not p.state_dir.exists() and not p.agent_path("stt").exists()


def test_old_launchdaemons_are_the_default_labels_and_the_loaded_ones(
    settings: Settings, tmp_path: Path, no_launchd: Path
) -> None:
    """The old root step installed the default labels only, whatever the
    prefix of this install; and a daemon still loaded after its plist was
    deleted holds the ports as much as one with a plist."""
    settings.launchd.label_prefix = "org.example.voicedtest"
    p = _plan(settings, tmp_path)
    assert p.legacy_refusal(FakeLaunchd()) is None
    watchdog = f"{DEFAULT_LAUNCHD_LABEL_PREFIX}.watchdog"
    _daemon(no_launchd, watchdog, p.user)
    refusal = p.legacy_refusal(FakeLaunchd())
    assert refusal is not None and f"system/{watchdog}" in refusal
    (no_launchd / f"{watchdog}.plist").unlink()
    stt = f"{DEFAULT_LAUNCHD_LABEL_PREFIX}.stt"
    refusal = p.legacy_refusal(FakeLaunchd(daemons={stt}))
    assert refusal is not None and f"system/{stt}" in refusal and watchdog not in refusal


def test_another_accounts_old_launchdaemons_are_not_offered_for_removal(
    settings: Settings, tmp_path: Path, no_launchd: Path
) -> None:
    p = _plan(settings, tmp_path)
    _daemon(no_launchd, p.labels["watchdog"], "someoneelse")
    refusal = p.legacy_refusal(FakeLaunchd())
    assert refusal is not None and "installed for someoneelse" in refusal
    assert "sudo" not in refusal


def test_prepare_builds_and_downloads_and_changes_nothing_that_runs(
    settings: Settings, tmp_path: Path, no_launchd: Path
) -> None:
    """The app's first half of a migration: with the old daemons still
    running, the engine check and the models, no ports step (they hold the
    ports), and nothing linked, recorded or loaded (ADR 0035)."""
    env = _environment(tmp_path / "env")
    p = _plan(settings, tmp_path)
    _daemon(no_launchd, p.labels["watchdog"], p.user)
    launchd = FakeLaunchd()
    steps: list[str] = []
    progress = Progress()
    progress.step = lambda step, state, shown: steps.append(f"{step} {state}")  # type: ignore[method-assign]
    calls: list[list[str]] = []
    run_install(
        p,
        runner=lambda argv: calls.append(list(argv)) or CommandResult(0),  # type: ignore[func-returns-value]
        query=launchd,
        prepare=True,
        say=lambda _: None,
        progress=progress,
    )
    ids = [line.split()[0] for line in steps if line.endswith(" done")]
    assert ids == ["config", "claim", "check", "models"]
    assert calls[0][:5] == [str(env / "bin/python"), "-P", "-m", "voiced.models", "download"]
    assert not p.entry.exists() and not (p.state_dir / "install.json").read_text().count(
        'environment": "'
    )
    assert launchd.loaded == {} and not p.agent_path("stt").exists()


@pytest.mark.parametrize("legacy", [False, True], ids=["fresh", "old daemons"])
@pytest.mark.parametrize("dry_run", [False, True], ids=["install", "dry run"])
def test_without_a_login_session_install_changes_nothing(
    settings: Settings, tmp_path: Path, no_launchd: Path, legacy: bool, dry_run: bool
) -> None:
    """Over SSH with nobody logged in at the Mac there is no gui domain, so
    the agents cannot load: the install says so first, before the old
    daemons' refusal and before any step, so nothing is built or removed
    for an install that cannot finish. A dry run lists the steps, then
    refuses the same way."""
    _environment(tmp_path / "env")
    p = _plan(settings, tmp_path)
    if legacy:
        for service in SERVICES:
            _daemon(no_launchd, p.labels[service], p.user)
    launchd = FakeLaunchd(session=False)
    calls: list[list[str]] = []
    said: list[str] = []
    with pytest.raises(InstallError) as refused:
        run_install(
            p,
            runner=lambda argv: calls.append(list(argv)) or CommandResult(0),  # type: ignore[func-returns-value]
            query=launchd,
            dry_run=dry_run,
            say=said.append,
        )
    reason = session_refusal(p.user, FakeLaunchd(session=False))
    removal = legacy_removal_command([p.labels[s] for s in SERVICES])
    assert str(refused.value).startswith(f"Not installing yet; nothing was changed. {reason}")
    assert (removal in str(refused.value)) == legacy
    assert calls == [] and launchd.calls[0] == ["launchctl", "print", f"gui/{os.getuid()}"]
    assert all(c[2].startswith("system/") for c in launchd.calls[1:])
    assert all(line.startswith(("would ", "$ ")) for line in said) and bool(said) == dry_run
    assert not p.state_dir.exists() and not p.config_file.exists() and not p.log_dir.exists()
    assert not p.agent_path("stt").parent.exists()


def test_prepare_needs_no_login_session(settings: Settings, tmp_path: Path) -> None:
    """``prepare`` loads no agent; the install that completes it checks."""
    _environment(tmp_path / "env")
    p = _plan(settings, tmp_path)
    launchd = FakeLaunchd(session=False)
    run_install(p, runner=lambda argv: CommandResult(0), query=launchd, prepare=True, say=[].append)
    assert ["launchctl", "print", f"gui/{os.getuid()}"] not in launchd.calls
    with pytest.raises(InstallError, match="Not logged in at this Mac"):
        run_install(p, runner=lambda argv: CommandResult(0), query=launchd, say=[].append)
    assert not p.entry.exists()


def test_the_agents_step_checks_the_session_again(settings: Settings, tmp_path: Path) -> None:
    """A logout during a long install: the agents step refuses, and says
    what is left to do."""
    _environment(tmp_path / "env")
    p = _plan(settings, tmp_path)
    timed_out = CommandResult(124, "", "launchctl: timed out after 30.0s")
    for gui in (CommandResult(113), timed_out):
        query = lambda argv, gui=gui: gui  # noqa: E731
        with pytest.raises(InstallError) as refused:
            install_agents(p, query, lambda _: None)
        assert str(refused.value) == (
            f"{session_refusal(p.user, query)} Then run `voiced install` "
            "again: the steps before this one are done, the agents are not loaded."
        )
    assert "timed out after 30.0s. Run `voiced install` again;" in str(refused.value)
    assert not p.agent_path("stt").exists()


def test_a_changed_agent_is_loaded_again_only_once_the_old_one_is_gone(
    settings: Settings, tmp_path: Path
) -> None:
    """bootout returns while launchd still tears the job down; bootstrapping
    then would fail (the fake refuses it as launchd does)."""
    _environment(tmp_path / "env")
    p = _plan(settings, tmp_path)
    launchd = FakeLaunchd(linger=3)
    install_agents(p, launchd, lambda _: None)
    p.agent_path("tts").write_bytes(b"<plist/>")
    launchd.calls.clear()
    install_agents(p, launchd, lambda _: None)
    tts = [
        c
        for c in launchd.calls
        if c[-1].endswith(p.labels["tts"]) or c[-1] == str(p.agent_path("tts"))
    ]
    assert [c[1] for c in tts] == [
        "print",
        "bootout",
        "print",
        "print",
        "print",
        "print",
        "bootstrap",
    ]
    assert p.labels["tts"] in launchd.loaded


def test_an_agent_that_never_unloads_is_said_not_failed_later(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from voiced.launchd import control

    monkeypatch.setattr(control, "BOOTOUT_WAIT_S", 0.0)
    _environment(tmp_path / "env")
    p = _plan(settings, tmp_path)
    launchd = FakeLaunchd(linger=10**6)
    install_agents(p, FakeLaunchd(), lambda _: None)
    launchd.loaded = {p.labels["tts"]: 7}
    p.agent_path("tts").write_bytes(b"<plist/>")
    with pytest.raises(InstallError, match="still unloading"):
        install_agents(p, launchd, lambda _: None)
    assert not any(c[1] == "bootstrap" and c[-1] == str(p.agent_path("tts")) for c in launchd.calls)
    launchd.loaded = {p.labels["stt"]: 8}
    with pytest.raises(InstallError, match="still unloading"):
        remove_agents(p, launchd, lambda _: None)


def test_the_removal_line_is_fixed_commands() -> None:
    """Root runs nothing the user's account wrote: launchctl and rm by path,
    the plists in /Library/LaunchDaemons, nothing else. The app runs the same
    commands (LegacyDaemons.swift)."""
    line = legacy_removal_command(["a.b.stt", "a.b.watchdog"])
    wait = (
        "n=0; while /bin/launchctl print system/{} >/dev/null 2>&1 && [ $n -lt 150 ]; "
        "do /bin/sleep 0.2; n=$((n+1)); done"
    )
    assert shlex.split(line) == [
        "sudo",
        "/bin/sh",
        "-c",
        "/bin/launchctl bootout system/a.b.stt 2>/dev/null; "
        "/bin/launchctl bootout system/a.b.watchdog 2>/dev/null; "
        "/bin/rm -f /Library/LaunchDaemons/a.b.stt.plist "
        "/Library/LaunchDaemons/a.b.watchdog.plist; "
        f"{wait.format('a.b.stt')}; {wait.format('a.b.watchdog')}",
    ]
    # Then waits, bounded, until launchd has let go of each (30 s), as the old script did.
    done = subprocess.run(["/bin/sh", "-n", "-c", shlex.split(line)[-1]], check=False)
    assert done.returncode == 0
    swift = (
        Path(__file__).parents[2] / "menubar/Sources/VoicedMenuBarUI/Installer/LegacyDaemons.swift"
    )
    inner = shlex.split(
        legacy_removal_command([f"{DEFAULT_LAUNCHD_LABEL_PREFIX}.{s}" for s in SERVICES])
    )[-1]
    assert f'"{inner}"' in swift.read_text()


def test_metal_probe_runs_as_a_one_shot_agent(plan: InstallPlan, tmp_path: Path) -> None:
    """Bootstrapped from a temporary plist, booted out after its result."""
    p = dataclasses.replace(plan, log_dir=tmp_path / "logs", home=tmp_path)
    launchd = FakeLaunchd()
    seen: dict[str, Any] = {}

    def query(argv: Sequence[str]) -> CommandResult:
        result = launchd(argv)
        if argv[1] == "bootstrap":
            seen.update(plistlib.loads(Path(argv[-1]).read_bytes()))
            seen["path"] = argv[-1]
            (tmp_path / "logs/metal-probe.json").write_text('{"ok": true}\n')
        return result

    assert run_metal_probe(p, Path("/x/python"), query, sleep=lambda _: None) == '{"ok": true}\n'
    label = p.labels["metalprobe"]
    assert seen["Label"] == label and seen["KeepAlive"] is False
    assert seen["ProgramArguments"][:3] == ["/x/python", "-m", "voiced.launchd.metal_probe"]
    assert "UserName" not in seen
    assert not Path(seen["path"]).exists()
    assert launchd.verbs()[-1] == ("bootout", label) and label not in launchd.loaded


def test_metal_probe_without_a_result_is_booted_out(
    plan: InstallPlan, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = dataclasses.replace(plan, log_dir=tmp_path / "logs", home=tmp_path)
    monkeypatch.setattr(install_module, "METAL_PROBE_WAIT_S", 0.0)
    launchd = FakeLaunchd()
    with pytest.raises(InstallError, match="no result"):
        run_metal_probe(p, Path("/x/python"), launchd, sleep=lambda _: None)
    assert p.labels["metalprobe"] not in launchd.loaded


def test_default_install_runner_streams() -> None:
    assert inspect.signature(run_install).parameters["runner"].default is run_streaming


def test_engine_variables_come_from_the_config_not_the_plists(
    settings: Settings, tmp_path: Path
) -> None:
    """The plists carry only a fixed environment (ADR 0018). The services,
    and the download as a child of a voiced that set them
    (apply_engine_environment), get the rest from the config; the
    download's own argument is only the config path."""
    settings.install.hf_home = tmp_path / "hf"
    settings.tts.espeak_library = tmp_path / "lib/libespeak-ng.dylib"
    settings.tts.espeak_data = tmp_path / "data"
    p = plan_install(settings, home=tmp_path, uv=None)
    assert p.model_download_command()[-2:] == ["--config", str(p.config_file)]
    assert set(p.environment_variables()) == {"HOME", "PATH", "VOICED_CONFIG", "PYTHONUNBUFFERED"}
    summary = plan_summary(p)
    assert f"hf_home        {tmp_path / 'hf'}" in summary
    assert f"VOICED_ESPEAK_DATA {tmp_path / 'data'}" in summary


def test_model_download_names_the_cache_it_uses(tmp_path: Path) -> None:
    """The cache the services read: HF_HOME as voiced set it from the config."""
    pytest.importorskip("huggingface_hub")
    argv = [sys.executable, "-c", "from voiced.models import cache_dir; print(cache_dir())"]
    base = {k: v for k, v in os.environ.items() if not k.startswith("HF_")}

    def cache(**env: str) -> str:
        out = subprocess.run(argv, env={**base, **env}, capture_output=True, text=True, check=True)
        return out.stdout.strip()

    assert cache(HOME=str(tmp_path)) == str(tmp_path / ".cache/huggingface/hub")
    assert cache(HF_HOME="/x/hf") == "/x/hf/hub"


def test_build_commands_use_the_locks_and_a_managed_python(
    settings: Settings, tmp_path: Path
) -> None:
    """--source: a fresh environment under envs/ on a uv-managed Python in the
    state dir, every dependency with its hash from the lock, then voiced
    without dependencies and with a hashed build backend; no uv.toml of the
    user's in any of it (UV_NO_CONFIG)."""
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
            "UV_NO_CONFIG=1",
            f"UV_PYTHON_INSTALL_DIR={tmp_path / 'state' / 'python'}",
            "/fake/uv",
            "venv",
            "--managed-python",
            "--python",
            "3.12",
            str(p.environment),
        ],
        [
            *("/usr/bin/env", "UV_NO_CONFIG=1"),
            *("/fake/uv", "pip", "install", "--python", python, "--no-deps"),
            *("--require-hashes", "-r", str(checkout / "src/voiced/requirements.lock")),
        ],
        [
            *("/usr/bin/env", "UV_NO_CONFIG=1"),
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


def test_model_download_runs_in_the_new_environment(plan: InstallPlan) -> None:
    """voiced.models from the new environment's own voiced, which reports its
    bytes only for --json-progress (the plain install streams to a terminal)."""
    plan = dataclasses.replace(plan, environment=Path("/tmp/a=b/env"))
    cmd = plan.model_download_command()
    assert cmd == [
        "/tmp/a=b/env/bin/python",
        "-P",
        "-m",
        "voiced.models",
        "download",
        "--config",
        str(plan.config_file),
    ]
    assert plan.model_download_command(report=True) == [*cmd, "--report"]


def test_shipped_lock_matches_uv_lock() -> None:
    """Every install path installs from these files; they must say what
    uv.lock says. ``scripts/refresh_locks.py`` writes them."""
    uv = shutil.which("uv")
    root = Path(__file__).resolve().parents[2]
    if uv is None or not (root / "uv.lock").is_file():
        pytest.skip("needs uv and a checkout")
    refresh = load("refresh_locks")
    for path in refresh.EXPORTS:
        argv = refresh.export_argv(uv, path)
        out = subprocess.run(argv, cwd=root, capture_output=True, text=True, check=True).stdout
        assert out == (root / path).read_text(), f"{path} is stale; run: {refresh.COMMAND}"
    assert install_module.packaged_lock().is_file()


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
    """``VOICED_CONFIG=~/config.toml`` puts an earlier release's staging dir at
    ``~/launchd``; only what voiced wrote goes."""
    home = tmp_path / "home"
    staging = home / "launchd"
    staging.mkdir(parents=True)
    (home / "config.toml").write_text("x")
    (home / "metal-probe.sh").write_text("x")
    (staging / "mine.plist").write_text("keep")
    p = plan_install(
        settings, state_dir=home / ".voiced", home=home, config_file=home / "config.toml"
    )
    for path in p.legacy_staged_files():
        path.write_text("x")
    targets = uninstall_targets(p, settings, logs=False, models=False, config=True)
    assert home not in targets.trees and staging not in targets.trees
    remove_targets(targets, p, lambda _: None)
    assert sorted(x.name for x in home.iterdir()) == ["launchd"]
    assert sorted(x.name for x in staging.iterdir()) == ["mine.plist"]


KOKORO = locked("mlx-community/Kokoro-82M-bf16")
PARAKEET = locked("mlx-community/parakeet-tdt-0.6b-v3")
FOREIGN_REVISION = "f" * 40


def test_remove_models_frees_the_blobs_and_keeps_other_revisions(
    settings: Settings, tmp_path: Path
) -> None:
    """The repository folder holds links; the bytes are in the shared store.
    The pinned revisions go with every blob only they use; another tool's
    revision of the same repository, and another repository sharing a
    blob, keep theirs."""
    assert KOKORO is not None and PARAKEET is not None
    settings.install.hf_home = tmp_path / "hf"
    hub = tmp_path / "hf" / "hub"
    weights, voice, shared, config = b"w" * 5000, b"v" * 700, b"s" * 300, b"{}"
    hf.add_revision(
        hub,
        KOKORO.repo,
        KOKORO.revision,
        {"config.json": config, "kokoro.safetensors": weights, "voices/af.safetensors": voice},
        ref="main",
    )
    other_tool = hf.add_revision(
        hub, KOKORO.repo, FOREIGN_REVISION, {"config.json": config, "extra.bin": b"x" * 40}
    )
    hf.add_revision(hub, "someone/else", "a" * 40, {"voice.safetensors": voice})
    hf.add_revision(hub, PARAKEET.repo, PARAKEET.revision, {"model.safetensors": shared})
    before = hf.du(hub)

    p = plan_install(settings, home=tmp_path)
    targets = uninstall_targets(p, settings, logs=False, models=True, config=False)
    assert targets.models is not None
    assert {(r.repo, r.revision) for r in targets.models.revisions} == {
        (KOKORO.repo, KOKORO.revision),
        (PARAKEET.repo, PARAKEET.revision),
    }
    assert hf.repo_dir(hub, KOKORO.repo) not in targets.trees  # never rmtree'd
    said: list[str] = []
    assert remove_targets(targets, p, said.append) == 2

    assert not (hf.repo_dir(hub, KOKORO.repo) / "snapshots" / KOKORO.revision).exists()
    assert not hf.store_path(hub, weights).exists()
    assert not hf.store_path(hub, shared).exists()
    assert not hf.repo_dir(hub, PARAKEET.repo).exists()  # its only revision
    # Another tool's: its revision of Kokoro, the blob it shares, and a
    # repository that shares the voice's bytes through the store.
    assert (other_tool / "config.json").read_bytes() == config
    assert (other_tool / "extra.bin").read_bytes() == b"x" * 40
    assert hf.store_path(hub, voice).exists()
    snapshot = hf.repo_dir(hub, "someone/else") / "snapshots" / ("a" * 40)
    assert (snapshot / "voice.safetensors").read_bytes() == voice
    freed = len(weights) + len(shared)
    assert before - hf.du(hub) >= freed
    assert f"freed {freed / 1000:.1f} KB ({freed:,} bytes) of models in {hub}" in said
    assert f"removed {revision_line(targets.models.revisions[0])}" in said


def test_remove_models_keeps_a_model_voiced_does_not_pin(
    settings: Settings, tmp_path: Path
) -> None:
    settings.install.hf_home = tmp_path / "hf"
    settings.tts.model = "someone/custom-voice"
    hub = tmp_path / "hf" / "hub"
    snapshot = hf.add_revision(hub, "someone/custom-voice", "c" * 40, {"m.bin": b"m" * 10})
    p = plan_install(settings, home=tmp_path)
    targets = uninstall_targets(p, settings, logs=False, models=True, config=False)
    assert targets.models is not None
    assert targets.models.revisions == [] and targets.models.unpinned == ["someone/custom-voice"]
    said: list[str] = []
    assert remove_targets(targets, p, said.append) == 0
    assert (snapshot / "m.bin").exists()
    assert any(line.startswith("kept someone/custom-voice: ") for line in said)


def test_remove_models_without_a_cache(settings: Settings, tmp_path: Path) -> None:
    settings.install.hf_home = tmp_path / "hf"
    p = plan_install(settings, home=tmp_path)
    targets = uninstall_targets(p, settings, logs=False, models=True, config=False)
    assert targets.models is not None and targets.models.revisions == []
    assert remove_targets(targets, p, lambda _: None) == 0


# What each release pinned. Re-pinning a model moves its old revision to
# the lock's previous, so uninstall still removes what an earlier release fetched;
# add each release's pins here.
RELEASED_PINS = {
    "v0.1.0": {
        ("mlx-community/parakeet-tdt-0.6b-v3", "ed2b7e8c15f9aaa0b5772e2efb986255eaef7e15"),
        ("mlx-community/Kokoro-82M-bf16", "a71e4d38b236d968966a2002c4c895dbd12b1c3c"),
    },
    "v0.1.1": {
        ("mlx-community/parakeet-tdt-0.6b-v3", "ed2b7e8c15f9aaa0b5772e2efb986255eaef7e15"),
        ("mlx-community/Kokoro-82M-bf16", "a71e4d38b236d968966a2002c4c895dbd12b1c3c"),
    },
}


@pytest.mark.parametrize("release", sorted(RELEASED_PINS))
def test_uninstall_knows_every_released_pin(release: str) -> None:
    assert RELEASED_PINS[release] <= pinned_revisions()


def test_remove_models_says_which_branch_shares_a_pinned_revision(
    settings: Settings, tmp_path: Path
) -> None:
    """Kokoro's ``main`` is the pinned commit: another tool that fetched
    ``main`` uses voiced's revision, so the dry run says so."""
    assert KOKORO is not None
    settings.install.hf_home = tmp_path / "hf"
    hub = tmp_path / "hf" / "hub"
    hf.add_revision(hub, KOKORO.repo, KOKORO.revision, {"config.json": b"{}"}, ref="main")
    p = plan_install(settings, home=tmp_path)
    targets = uninstall_targets(p, settings, logs=False, models=True, config=False)
    assert targets.models is not None
    [revision] = targets.models.revisions
    assert revision.refs == ("main",)
    assert revision_line(revision) == (
        f"{KOKORO.repo} revision {KOKORO.revision} (also `main`, which other tools may use)"
    )


def test_remove_models_reports_a_repository_the_scan_cannot_read(
    settings: Settings, tmp_path: Path
) -> None:
    """A download interrupted before any snapshot leaves a folder the scan
    skips with a warning; uninstall names it instead of saying nothing."""
    assert PARAKEET is not None
    settings.install.hf_home = tmp_path / "hf"
    hub = tmp_path / "hf" / "hub"
    (hf.repo_dir(hub, PARAKEET.repo) / "blobs").mkdir(parents=True)
    hf.add_revision(hub, "someone/broken-too", "b" * 40, {"m": b"m"})
    shutil.rmtree(hf.repo_dir(hub, "someone/broken-too") / "snapshots")
    p = plan_install(settings, home=tmp_path)
    targets = uninstall_targets(p, settings, logs=False, models=True, config=False)
    assert targets.models is not None
    assert targets.models.broken == [PARAKEET.repo]
    said: list[str] = []
    assert remove_targets(targets, p, said.append) == 0
    [note] = said
    assert note == (
        f"cannot remove {PARAKEET.repo}: the cache scan could not read it; run `voiced install` "
        f"again to finish the download, or `rm -r {hf.repo_dir(hub, PARAKEET.repo)}`"
    )


def test_remove_models_removes_the_optional_pins_too(settings: Settings, tmp_path: Path) -> None:
    """Phonon's model, its voices repository and the gated 40M models are
    pinned like the defaults, so uninstall removes their pinned revisions
    whichever engine the config names; another revision stays."""
    settings.install.hf_home = tmp_path / "hf"
    hub = tmp_path / "hf" / "hub"
    optional = [locked(repo) for repo in ("gradium/phonon-31m-en", "gradium/phonon-voices")]
    optional.append(locked("gradium/phonon-40m-en"))
    for k, pinned in enumerate(optional):
        assert pinned is not None
        hf.add_revision(hub, pinned.repo, pinned.revision, {"f": bytes([k]) * 100})
    other = hf.add_revision(hub, "gradium/phonon-voices", FOREIGN_REVISION, {"g": b"g" * 50})
    p = plan_install(settings, home=tmp_path)
    targets = uninstall_targets(p, settings, logs=False, models=True, config=False)
    assert targets.models is not None
    assert {(r.repo, r.revision) for r in targets.models.revisions} == {
        (o.repo, o.revision) for o in optional if o is not None
    }
    assert remove_targets(targets, p, lambda _: None) == 3
    assert (other / "g").read_bytes() == b"g" * 50


def test_plan_uses_the_real_account_not_logname(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LOGNAME and USER are whatever the environment says; "System
    Administrator" is uid 0, so believing it would take root's old
    LaunchDaemons for this user's."""
    monkeypatch.setenv("LOGNAME", "System Administrator")
    monkeypatch.setenv("USER", "System Administrator")
    p = plan_install(settings, home=tmp_path, uv=None)
    assert p.user == pwd.getpwuid(os.getuid()).pw_name != "System Administrator"


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
        config_file=tmp_path / "config.toml",
        uv="/fake/uv",
        **kwargs,
    )


def _install(
    p: InstallPlan, said: list[str] | None = None, launchd: FakeLaunchd | None = None
) -> list[list[str]]:
    """Runs ``p``; returns every command, the engine check included."""
    calls: list[list[str]] = []

    def runner(argv: Sequence[str]) -> CommandResult:
        calls.append(list(argv))
        if argv[-1:] == [str(p.environment)]:  # uv venv
            _environment(p.environment)
        return CommandResult(0)

    launchd = launchd or FakeLaunchd()

    def query(argv: Sequence[str]) -> CommandResult:
        if argv[0] != "launchctl":
            calls.append(list(argv))
        return launchd(argv)

    run_install(p, runner=runner, query=query, say=(said if said is not None else []).append)
    return calls


def test_install_links_the_running_environment(settings: Settings, tmp_path: Path) -> None:
    env = _environment(tmp_path / "env")
    p = _plan(settings, tmp_path)
    calls = _install(p)
    assert [c[0] for c in calls] == [str(env / "bin/python")] * 2  # check, then models
    assert "import mlx.core" in calls[0][-1]
    assert calls[1][2:5] == ["-m", "voiced.models", "download"]
    assert p.entry.is_symlink() and os.readlink(p.entry) == str(env / "bin" / "voiced")
    manifest = read_manifest(p.state_dir)
    assert manifest is not None
    assert (manifest.source, manifest.environment) == ("cli", env)
    assert manifest.lock_sha256 == sha256_file(install_module.packaged_lock())
    assert manifest.python == os.path.realpath(env / "bin" / "python")
    assert sorted(x.name for x in p.state_dir.iterdir()) == ["bin", "install.json"]


def test_install_removes_what_an_early_release_staged(settings: Settings, tmp_path: Path) -> None:
    """What releases before ADR 0018 staged for a sudo script goes: an
    install writes no script, and plists only in ~/Library/LaunchAgents."""
    env = _environment(tmp_path / "env")
    p = _plan(settings, tmp_path)
    staging = p.legacy_staging_dir
    staging.mkdir(parents=True)
    for path in p.legacy_staged_files():
        path.write_text("<plist/>")
    (staging / "mine.txt").write_text("keep")
    said: list[str] = []
    _install(p, said)
    assert sorted(x.name for x in staging.iterdir()) == ["mine.txt"]
    assert any(line.startswith("remove the plists and sudo scripts") for line in said)
    left = [x for x in tmp_path.rglob("*") if x.suffix in (".plist", ".sh")]
    agents = tmp_path / "Library/LaunchAgents"
    assert [x for x in left if not x.is_relative_to(env) and not x.is_relative_to(agents)] == []
    (staging / "mine.txt").unlink()
    for path in p.legacy_staged_files()[:1]:
        path.write_text("<plist/>")
    _install(p)
    assert not staging.exists()


def test_install_builds_a_source_environment(settings: Settings, tmp_path: Path) -> None:
    p = _plan(settings, tmp_path, source=tmp_path / "checkout")
    calls = _install(p)
    assert [c[c.index("/fake/uv") + 1] for c in calls[:3]] == [
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
    assert not p.state_dir.exists()
    assert not (tmp_path / "logs").exists()
    text = "\n".join(said)
    assert f"would create {p.config_file}" in text
    assert "/fake/uv venv --managed-python" in text
    assert f"would link {p.entry}" in text
    assert "staged" not in text


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
        run_install(
            p,
            runner=pytest.fail,
            query=FakeLaunchd(check=CommandResult(1)),
            say=lambda _: None,
        )
    assert not p.entry.exists()


def test_run_install_stops_on_failure(settings: Settings, tmp_path: Path) -> None:
    p = _plan(settings, tmp_path, source=tmp_path / "checkout")
    with pytest.raises(InstallError, match="boom"):
        run_install(p, runner=lambda argv: CommandResult(1, "", "boom"), say=lambda _: None)
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
    run_install(
        p,
        runner=lambda argv: CommandResult(0),
        query=FakeLaunchd(),
        skip_models=True,
        say=said.append,
    )
    assert stat.S_IMODE(p.config_file.stat().st_mode) == 0o600


WHISPER = "mlx-community/whisper-large-v3-turbo-asr-8bit"
CHOICE = {"stt.enabled": True, "stt.engine": "whisper", "stt.model": WHISPER, "tts.enabled": False}


def _chosen(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> InstallPlan:
    """The app's first-run choice over a config that runs Parakeet: the
    plan stages it, the engines checked as this voiced has them."""
    from voiced.engines.catalogue import engines_list

    monkeypatch.setattr("voiced.control.ops.engines_in_subprocess", lambda **kw: engines_list())
    _environment(tmp_path / "env")
    (tmp_path / "config.toml").write_text(
        '# kept\n[stt]\nengine = "parakeet"\nmodel = "mlx-community/parakeet-tdt-0.6b-v3"\n'
    )
    return _plan(settings, tmp_path, changes=CHOICE)


def test_the_chosen_settings_are_downloaded_first_and_written_at_the_switch(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, models_downloaded: Path
) -> None:
    """The app's first-run choices (``--set``): the plan and the models step
    use them, and the config gets them only once the models are in, with
    every check ``voiced settings set`` makes. A service switched off has
    its ports left out."""
    p = _chosen(settings, tmp_path, monkeypatch)
    before = p.config_file.read_text()
    assert p.off == ("tts",)
    assert set(p.ports) == {"[stt] http_port", "[stt] wyoming_port"}
    steps = [s.id for s in install_module.install_steps(p, None)]
    assert steps.index("models") < steps.index("settings") < steps.index("link")

    seen: list[tuple[list[str], str]] = []

    def runner(argv: Sequence[str]) -> CommandResult:
        if "voiced.models" in argv:
            seen.append((list(argv), p.config_file.read_text()))
        return CommandResult(0)

    said: list[str] = []
    run_install(p, runner=runner, query=FakeLaunchd(), say=said.append)
    ((download, config_then),) = seen
    assert config_then == before, "the config is unchanged while the models download"
    assert download[-8:] == [
        "--set", "stt.enabled=true", "--set", "stt.engine=whisper",
        "--set", f"stt.model={WHISPER}", "--set", "tts.enabled=false",
    ]  # fmt: skip
    written = load_settings(p.config_file)
    assert (written.stt.engine, written.stt.model) == ("whisper", WHISPER)
    assert written.stt.enabled and not written.tts.enabled
    assert p.config_file.read_text().startswith("# kept\n"), "comments kept"
    assert any("tts: " in line and "serves nothing" in line for line in said)


def test_the_plan_carries_the_engine_switch_as_staged(settings: Settings, tmp_path: Path) -> None:
    """The plan stages ``--set`` with this voiced's engines: a text to speech
    switch gains the voice the new engine has, and the models step and the
    switch get the same completed changes."""
    _environment(tmp_path / "env")
    p = _plan(
        settings, tmp_path, changes={"tts.engine": "phonon", "tts.model": "gradium/phonon-31m-en"}
    )
    assert p.changes["tts.voice"] != "af_heart"
    assert f"tts.voice={p.changes['tts.voice']}" in p.model_download_command()


def test_a_failed_download_leaves_the_config_as_it_was(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = _chosen(settings, tmp_path, monkeypatch)
    before = p.config_file.read_text()
    with pytest.raises(InstallError, match="model download failed"):
        run_install(
            p,
            runner=lambda argv: CommandResult(1 if "voiced.models" in argv else 0),
            query=FakeLaunchd(),
            say=[].append,
        )
    assert p.config_file.read_text() == before


def test_the_models_step_downloads_what_the_staged_settings_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``download --set``: the config is not written yet, so the step reads
    the choice from its arguments."""
    from voiced.models import commands

    fetched: list[str] = []
    monkeypatch.setattr("voiced.models.commands.cache_dir", lambda: "/hf/hub")
    monkeypatch.setattr("voiced.models.commands.fetch", lambda model, write: fetched.append(model))
    config = tmp_path / "config.toml"
    config.write_text('[stt]\nengine = "fake"\n[tts]\nengine = "fake"\n')
    commands.download(config, None, {"stt.enabled": False, "tts.enabled": False})
    assert fetched == []


def test_the_models_step_passes_a_service_switched_off(settings: Settings, tmp_path: Path) -> None:
    """``voiced.models download`` downloads nothing for a service that is
    off (tests/core/test_models.py); the step hands it the choice."""
    _environment(tmp_path / "env")
    p = _plan(settings, tmp_path, changes={"stt.enabled": False})
    steps = install_module.install_steps(p, None, query=FakeLaunchd(), say=[].append)
    (models,) = [step.shown for step in steps if step.id == "models"]
    assert shlex.split(models)[-2:] == ["--set", "stt.enabled=false"]


def test_cli_install_takes_only_the_service_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--set`` parses as ``voiced settings set`` does, takes only which
    services run with which model, and refuses an engine this voiced lacks
    before anything is planned."""
    monkeypatch.setenv("VOICED_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.setenv("VOICED_SERVER__LOG_DIR", str(tmp_path / "logs"))
    argv = ["install", "--dry-run", "--state-dir", str(tmp_path / "state")]
    assert main([*argv, "--set", "tts.enabled=false", "--set", "stt.enabled=yes"]) == 0
    out = capsys.readouterr().out
    assert "would set tts.enabled=False, stt.enabled=True in" in out
    assert "check that ports 8881, 10300 are free" in out
    assert main([*argv, "--set", "stt.enabled=maybe"]) == 1
    assert "--set stt.enabled: must be true or false" in capsys.readouterr().out
    assert main([*argv, "--set", "bind=lan"]) == 1
    assert "--set bind: install sets only stt.enabled" in capsys.readouterr().out
    # Refused by its key, before anything is read: api_key=- would read stdin.
    monkeypatch.setattr("sys.stdin", None)
    assert main([*argv, "--set", "api_key=-"]) == 1
    assert "--set api_key: install sets only" in capsys.readouterr().out
    assert main([*argv, "--set", "stt.engine=whisper"]) == 1
    assert "--set stt.engine needs stt.model in the same install" in capsys.readouterr().out
    assert main([*argv, "--set", "stt.engine=nope", "--set", "stt.model=x"]) == 1
    assert "--set stt.engine: 'nope' is not installed" in capsys.readouterr().out
    assert not (tmp_path / "missing.toml").exists()


def test_cli_dry_run_prints_everything_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("VOICED_CONFIG", str(tmp_path / "missing.toml"))
    monkeypatch.setenv("VOICED_SERVER__LOG_DIR", str(tmp_path / "logs"))
    argv = ["install", "--dry-run", "--state-dir", str(tmp_path / "state")]
    code = main([*argv, "--source", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert not (tmp_path / "state").exists()
    assert not (tmp_path / "logs").exists()
    assert "dry run" in out
    assert "--require-hashes" in out and "--build-constraints" in out
    assert "would write the LaunchAgents" in out
    assert "sudo" not in out


def test_cli_install_prints_the_removal_line_for_old_launchdaemons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    no_launchd: Path,
) -> None:
    monkeypatch.setenv("VOICED_CONFIG", str(tmp_path / "missing.toml"))
    user = pwd.getpwuid(os.getuid()).pw_name
    labels = [f"{DEFAULT_LAUNCHD_LABEL_PREFIX}.{s}" for s in SERVICES]
    for label in labels:
        _daemon(no_launchd, label, user)
    assert main(["install", "--dry-run", "--state-dir", str(tmp_path / "state")]) == 1
    out = capsys.readouterr().out
    assert "install failed: Not installing yet; nothing was changed. voiced still runs" in out
    assert f"\n\n  {legacy_removal_command(labels)}\n  voiced install" in out


def test_cli_install_without_a_login_session_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    logged_out: None,
) -> None:
    monkeypatch.setenv("VOICED_CONFIG", str(tmp_path / "config.toml"))
    assert main(["install", "--state-dir", str(tmp_path / "state"), "--skip-models"]) == 1
    out = capsys.readouterr().out
    assert "install failed: Not installing yet; nothing was changed." in out
    assert "Not logged in at this Mac" in out
    assert not (tmp_path / "state").exists() and not (tmp_path / "config.toml").exists()


def test_cli_install_refuses_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The services are the user's LaunchAgents; root's domain is not theirs."""
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
        return CommandResult(1, "", "no network") if "voiced.models" in argv else CommandResult(0)

    with pytest.raises(InstallError, match="model download"):
        run_install(p, runner=runner, query=FakeLaunchd(), say=lambda _: None)
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


def test_the_manifest_records_the_environment_it_replaced(
    settings: Settings, tmp_path: Path
) -> None:
    """`previous` is the environment prune keeps: re-running install keeps
    it, and running the previous environment's install (a rollback) makes
    the two swap."""
    first = _plan(settings, tmp_path, source=tmp_path / "checkout", origin="app")
    _install(first)
    manifest = read_manifest(first.state_dir)
    assert manifest is not None and manifest.previous is None
    second = _plan(settings, tmp_path, source=tmp_path / "checkout", origin="app")
    _install(second)
    manifest = read_manifest(first.state_dir)
    assert manifest is not None
    assert (manifest.environment, manifest.previous) == (second.environment, first.environment)
    _install(_plan(settings, tmp_path, prefix=second.environment, origin="app"))
    manifest = read_manifest(first.state_dir)
    assert manifest is not None and manifest.previous == first.environment
    _install(_plan(settings, tmp_path, prefix=first.environment, origin="app"))
    manifest = read_manifest(first.state_dir)
    assert manifest is not None
    assert (manifest.environment, manifest.previous) == (first.environment, second.environment)
    assert second.environment.exists()
    # Re-running install after the previous environment was deleted by hand
    # has nothing to go back to.
    shutil.rmtree(second.environment)
    _install(_plan(settings, tmp_path, prefix=first.environment, origin="app"))
    manifest = read_manifest(first.state_dir)
    assert manifest is not None
    assert (manifest.environment, manifest.previous) == (first.environment, None)


def test_a_new_environment_never_takes_a_moved_or_unfinished_id(tmp_path: Path) -> None:
    envs = tmp_path / "envs"
    (envs / "20261019T000000.broken").mkdir(parents=True)
    (envs / "20261019T000000-2.partial").mkdir()
    (envs / "20261019T000000-3").mkdir()
    assert new_environment_dir(tmp_path, "20261019T000000").name == "20261019T000000-4"
    assert new_environment_dir(tmp_path, "20261019T000001").name == "20261019T000001"
    # Gone, but install.json still names them.
    gone = Manifest(
        source="cli",
        environment=envs / "20261019T000001",
        previous=envs / "20261019T000001-2",
    )
    assert new_environment_dir(tmp_path, "20261019T000001", gone).name == "20261019T000001-3"


def test_an_install_refuses_a_name_another_one_built_while_it_waited(
    settings: Settings, tmp_path: Path
) -> None:
    """Both planned in the same second, before either built: the one that
    waited for the lock must not build into the other's environment."""
    first = _plan(settings, tmp_path, source=tmp_path / "checkout")
    _install(first)
    with pytest.raises(InstallError, match="while this one waited"):
        _install(first)
    manifest = read_manifest(first.state_dir)
    assert manifest is not None and manifest.environment == first.environment


def test_a_missing_active_environment_is_no_rollback(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The app moves an environment whose Python is gone to `<id>.broken`
    before it builds a fresh one: the environment that one replaced stays
    `previous`, and prune keeps it. All four installs land in one second,
    so none may take a name that is there or that install.json names."""
    now = time.gmtime(1_792_454_400)
    monkeypatch.setattr("voiced.launchd.install.time.gmtime", lambda secs=None: now)
    first = _plan(settings, tmp_path, source=tmp_path / "checkout", origin="app")
    _install(first)
    second = _plan(settings, tmp_path, source=tmp_path / "checkout", origin="app")
    _install(second)
    second.environment.rename(second.environment.with_name(second.environment.name + ".broken"))
    third = _plan(settings, tmp_path, source=tmp_path / "checkout", origin="app")
    _install(third)
    manifest = read_manifest(third.state_dir)
    assert manifest is not None
    assert (manifest.environment, manifest.previous) == (third.environment, first.environment)
    assert first.environment.exists()

    # Without that one either, there is nothing to go back to.
    shutil.rmtree(third.environment)
    shutil.rmtree(first.environment)
    fourth = _plan(settings, tmp_path, source=tmp_path / "checkout", origin="app")
    # Not the first's name, which is gone but install.json names; `-2` is
    # free again, since the third install pruned `<id>-2.broken`.
    assert fourth.environment not in (first.environment, third.environment)
    assert fourth.environment.name.endswith("-2")
    _install(fourth)
    manifest = read_manifest(fourth.state_dir)
    assert manifest is not None
    assert (manifest.environment, manifest.previous) == (fourth.environment, None)


def test_switching_away_from_a_legacy_venv_has_no_previous(
    settings: Settings, tmp_path: Path
) -> None:
    _environment(tmp_path / "env")
    p = _plan(settings, tmp_path)
    _environment(p.state_dir)
    _install(p)
    manifest = read_manifest(p.state_dir)
    assert manifest is not None and manifest.previous is None


def test_a_manifest_without_previous_reads() -> None:
    manifest = Manifest.from_json({"source": "app", "environment": "/e"})
    assert manifest.previous is None
    assert manifest.to_json()["previous"] is None
    with pytest.raises(ManifestError):
        Manifest.from_json({"source": "app", "environment": "/e", "previous": 1})


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
    (p.state_dir / "stt.off").write_text("4242\n")  # a parked process killed by SIGKILL
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


def test_uninstall_leaves_a_symlinked_envs_alone(settings: Settings, tmp_path: Path) -> None:
    env = _app_build(tmp_path / "state")
    _environment(env.with_name(env.name + ".partial"))
    elsewhere = tmp_path / "elsewhere"
    shutil.move(env.parent, elsewhere)
    env.parent.symlink_to(elsewhere)
    said: list[str] = []
    p = _plan(settings, tmp_path)
    remove_targets(
        uninstall_targets(p, settings, logs=False, models=False, config=False), p, said.append
    )
    assert sorted(x.name for x in elsewhere.iterdir()) == [
        "20261018T120000",
        "20261018T120000.partial",
    ]
    assert (elsewhere / "20261018T120000" / "pyvenv.cfg").exists()
    assert sorted(x.name for x in p.state_dir.iterdir()) == ["envs"]
    assert any("kept" in line and "envs" in line for line in said)


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


def test_uninstall_stops_and_removes_the_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """No root step: the dry run names the agents, --yes boots them out and
    deletes their plists before the files they run."""
    from voiced.cli import build_parser
    from voiced.cli_lifecycle import run_lifecycle

    monkeypatch.setenv("VOICED_CONFIG", str(tmp_path / "config.toml"))
    state = tmp_path / "state"
    settings = Settings()
    settings.server.log_dir = tmp_path / "logs"
    p = plan_install(settings, state_dir=state, uv=None)
    launchd = FakeLaunchd()
    install_agents(p, launchd, lambda _: None)
    write_manifest(state, Manifest(source="cli", environment=None))

    def uninstall(*flags: str) -> str:
        args = build_parser().parse_args(["uninstall", "--state-dir", str(state), *flags])
        assert run_lifecycle(args, settings, launchd, print) == 0
        return capsys.readouterr().out

    out = uninstall()
    assert f"{p.agent_path('stt')} ({p.labels['stt']}, stopped first)" in out
    assert "sudo" not in out and len(launchd.loaded) == 3
    out = uninstall("--yes")
    assert launchd.loaded == {}
    assert not any(p.agent_path(s).exists() for s in SERVICES)
    assert f"stopped {p.labels['watchdog']}" in out and not state.exists()
    remove_agents(p, launchd, lambda _: None)  # again: nothing to do


def test_uninstall_waits_for_old_launchdaemons_to_go(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    no_launchd: Path,
) -> None:
    from voiced.cli import build_parser
    from voiced.cli_lifecycle import run_lifecycle

    monkeypatch.setenv("VOICED_CONFIG", str(tmp_path / "config.toml"))
    settings = Settings()
    _daemon(no_launchd, settings.launchd.label("stt"), pwd.getpwuid(os.getuid()).pw_name)
    write_manifest(tmp_path / "state", Manifest(source="cli", environment=None))
    args = build_parser().parse_args(["uninstall", "--yes", "--state-dir", str(tmp_path / "state")])
    assert run_lifecycle(args, settings, FakeLaunchd(), print) == 1
    out = capsys.readouterr().out
    assert legacy_removal_command([settings.launchd.label("stt")]) in out
    assert (tmp_path / "state/install.json").exists()


# The ports step (ADR 0018) ---------------------------------------------------


def _listening(
    monkeypatch: pytest.MonkeyPatch,
    by_port: dict[int, list[int]],
    argvs: dict[int, Any],
    lsof: bool = True,
    bound: set[int] | None = None,
) -> None:
    """lsof's answer per port (``lsof``: whether lsof is there), each pid's
    name and argv, and the ports bind() finds in use (``bound``, by default
    those lsof lists)."""
    in_use = OSError(errno.EADDRINUSE, "Address already in use")
    taken = set(by_port) if bound is None else bound
    monkeypatch.setattr(
        ports_module, "bind_error", lambda bind, port: in_use if port in taken else None
    )
    monkeypatch.setattr(
        ports_module,
        "listener_pids",
        lambda port, runner: by_port.get(port, []) if lsof else None,
    )
    monkeypatch.setattr(ports_module, "process_of", lambda pid: argvs[pid])


def test_a_port_held_by_another_program_stops_the_install(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Named by port, config key and holder, before the models and before
    anything the running install uses changes."""
    _environment(tmp_path / "env")
    settings.stt.wyoming_port, settings.tts.http_port = 10300, 8880
    p = _plan(settings, tmp_path)
    _listening(
        monkeypatch,
        {10300: [11], 8880: [12]},
        {11: ("python3.12", ["python3", "-m", "wyoming_faster_whisper"]), 12: ("nginx", ["nginx"])},
    )
    calls: list[list[str]] = []
    with pytest.raises(InstallError) as info:
        run_install(
            p,
            runner=lambda argv: calls.append(list(argv)) or CommandResult(0),
            query=FakeLaunchd(),
            say=lambda _: None,
        )
    message = str(info.value)
    assert "port 10300 ([stt] wyoming_port) is in use by python3.12 (pid 11)" in message
    assert "port 8880 ([tts] http_port) is in use by nginx (pid 12)" in message
    assert str(p.config_file) in message
    assert calls == [], "no model download"
    assert not p.entry.exists()


def test_voiceds_own_running_services_are_not_a_conflict(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reinstall or an update runs while the services listen: the
    processes launchd runs for this install's agents."""
    _environment(tmp_path / "env")
    p = _plan(settings, tmp_path)
    serve = ("Python", ["/x/bin/python3.12", str(p.entry), "serve", "stt"])
    _listening(
        monkeypatch,
        {port: [20 if "stt" in key else 22] for key, port in p.ports.items()},
        {20: serve, 22: serve},
    )
    launchd = FakeLaunchd()
    launchd.loaded = {p.labels["stt"]: 20, p.labels["tts"]: 22, p.labels["watchdog"]: 99}
    _install(p, launchd=launchd)
    assert p.entry.is_symlink()


def test_the_same_entry_point_outside_this_installs_agents_is_a_conflict(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An old LaunchDaemon (or a `voiced serve`) runs the same entry point,
    but launchd does not run it for this user's agents: the agents would
    fail to bind and respawn every 10 s for ever, so the install says so."""
    _environment(tmp_path / "env")
    p = _plan(settings, tmp_path)
    port = p.ports["[stt] wyoming_port"]
    serve = ("python3.12", ["/x/bin/python3.12", str(p.entry), "serve", "stt"])
    _listening(monkeypatch, {port: [31]}, {31: serve})
    with pytest.raises(
        InstallError,
        match=rf"port {port} \(\[stt\] wyoming_port\) is in use by python3.12 \(pid 31\)",
    ):
        _install(p)


@pytest.mark.parametrize(
    "argv",
    [
        ["/x/bin/python3.12", "/elsewhere/.voiced/bin/voiced", "serve", "stt"],
        ["/x/bin/python3.12", "-m", "voiced", "serve", "stt"],
        ["/x/bin/tail", "-f", "ENTRY"],  # this entry point, not run as the program
        ["ENTRY"],
        [],
    ],
)
def test_only_a_listener_running_this_entry_point_is_voiceds_own(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    """Another install's voiced, or one whose argv cannot be read, is a
    conflict like any other program."""
    _environment(tmp_path / "env")
    p = _plan(settings, tmp_path)
    argv = [str(p.entry) if a == "ENTRY" else a for a in argv]
    _listening(monkeypatch, {settings.stt.http_port: [30]}, {30: ("Python", argv)})
    with pytest.raises(InstallError, match=r"is in use by Python \(pid 30\)"):
        _install(p)


def test_a_listener_the_bind_does_not_collide_with_is_still_a_conflict(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A program on 127.0.0.1 does not stop a bind on 0.0.0.0, but local
    clients would reach it instead of voiced."""
    _environment(tmp_path / "env")
    settings.server.bind = "0.0.0.0"
    p = _plan(settings, tmp_path)
    port = settings.stt.http_port
    _listening(monkeypatch, {port: [40]}, {40: ("nc", ["nc", "-l"])}, bound=set())
    with pytest.raises(InstallError, match=rf"port {port} \(\[stt\] http_port\) is in use by nc"):
        _install(p)


def test_a_port_lsof_cannot_name_is_another_accounts(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bind() sees every account's sockets; lsof as the user only the
    user's own processes."""
    _environment(tmp_path / "env")
    p = _plan(settings, tmp_path)
    _listening(monkeypatch, {settings.tts.wyoming_port: []}, {})
    with pytest.raises(InstallError) as info:
        _install(p)
    assert (
        f"port {settings.tts.wyoming_port} ([tts] wyoming_port) is in use by another "
        "account or the system" in str(info.value)
    )


def test_a_port_that_cannot_be_bound_otherwise_stops_the_install(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bind address this Mac does not have, say."""
    _environment(tmp_path / "env")
    settings.server.bind = "192.0.2.1"
    p = _plan(settings, tmp_path)
    error = OSError(errno.EADDRNOTAVAIL, "Can't assign requested address")
    monkeypatch.setattr(ports_module, "bind_error", lambda bind, port: error)
    with pytest.raises(InstallError) as info:
        _install(p)
    assert (
        f"port {settings.stt.http_port} ([stt] http_port) cannot be bound on 192.0.2.1: "
        "Can't assign requested address" in str(info.value)
    )


def test_without_lsof_a_port_in_use_is_said_not_refused(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It cannot be told apart from voiced's own running service."""
    _environment(tmp_path / "env")
    _listening(monkeypatch, {settings.stt.http_port: []}, {}, lsof=False)
    said: list[str] = []
    _install(_plan(settings, tmp_path), said)
    assert (
        f"port {settings.stt.http_port} ([stt] http_port) is in use; lsof not found to "
        "tell by whom" in said
    )


def test_the_ports_step_checks_the_four_configured_ports(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.stt.http_port, settings.stt.wyoming_port = 1001, 1002
    settings.tts.http_port, settings.tts.wyoming_port = 1003, 1004
    _environment(tmp_path / "env")
    settings.server.bind = "::1"
    asked: list[tuple[str, int]] = []
    monkeypatch.setattr(ports_module, "bind_error", lambda bind, port: asked.append((bind, port)))
    _install(_plan(settings, tmp_path))
    assert asked == [("::1", 1001), ("::1", 1002), ("::1", 1003), ("::1", 1004)]


def test_bind_error_binds_as_the_services_do() -> None:
    """The function itself, not the stub every test gets (tests/conftest.py):
    a listening socket's port is in use, a closed one's is free again."""
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        port = held.getsockname()[1]
        error = real_bind_error("127.0.0.1", port)
        assert error is not None and error.errno == errno.EADDRINUSE
    assert real_bind_error("127.0.0.1", port) is None


def test_listener_pids_reads_lsof() -> None:
    """The function itself, not the stub every test gets (tests/conftest.py)."""
    seen: list[list[str]] = []

    def lsof(argv: Sequence[str]) -> CommandResult:
        seen.append(list(argv))
        return CommandResult(
            1 if argv[3] == "-iTCP:2" else 0, "12\n34\n" if argv[3] == "-iTCP:1" else ""
        )

    assert real_listener_pids(1, lsof) == [12, 34]
    assert seen == [["lsof", "-nP", "-t", "-iTCP:1", "-sTCP:LISTEN"]]
    assert real_listener_pids(2, lsof) == []  # lsof exits 1 when nothing listens
    assert real_listener_pids(3, lambda argv: CommandResult(127)) is None


def test_install_says_a_service_switched_off_stays_off(settings: Settings, tmp_path: Path) -> None:
    """The daemon is installed as always and parks itself (ADR 0033)."""
    _environment(tmp_path / "env")
    settings.stt.enabled = False
    p = _plan(settings, tmp_path)
    assert p.off == ("stt",)
    said: list[str] = []
    _install(p, said)
    assert (
        "stt: off ([stt] enabled = false); turn it on with `voiced settings set "
        "stt.enabled=true` and `voiced restart stt`. Its agent starts and serves nothing."
    ) in said
    assert not any(line.startswith("tts: off") for line in said)
