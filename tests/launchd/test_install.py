"""Installer plan, the root step, the Metal probe, dry-run, and uninstall safety."""

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
from voiced.config import DEFAULT_LAUNCHD_LABEL_PREFIX, LOG_DIR_IN_HOME, SERVICES, Settings
from voiced.launchd import install as install_module
from voiced.launchd import ports as ports_module
from voiced.launchd.control import CommandResult, run_streaming
from voiced.launchd.install import (
    BASE_PATH,
    PLIST_END,
    PRIVILEGED_SCRIPT,
    InstallError,
    InstallPlan,
    new_environment_dir,
    plan_install,
    plan_summary,
    privileged_script,
    removal_refusal,
    remove_targets,
    revision_line,
    root_command,
    run_install,
    uninstall_targets,
    write_default_config,
)
from voiced.launchd.ports import bind_error as real_bind_error
from voiced.launchd.ports import listener_pids as real_listener_pids
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


def test_metal_probe_matches_golden(plan: InstallPlan) -> None:
    probe = plan.metal_probe(HOME / ".voiced/bin/python")
    _compare(probe.script, "metal-probe.sh")
    _compare(probe.plist.decode(), "metalprobe.plist")
    assert plistlib.loads(probe.plist)["KeepAlive"] is False
    assert "StandardErrorPath" not in plistlib.loads(probe.plist)
    # The fixed prefix, whatever the config's label_prefix: config never steers root.
    assert probe.label == f"{DEFAULT_LAUNCHD_LABEL_PREFIX}.metalprobe"
    assert "trap " in probe.script and "EXIT" in probe.script
    assert probe.script_path == HOME / ".config/voiced/metal-probe.sh"


def _heredoc(script: str) -> str:
    """The plist between the probe script's heredoc markers."""
    lines = script.splitlines()
    begin = next(i for i, line in enumerate(lines) if line.endswith(f"<<'{PLIST_END}'"))
    return "\n".join(lines[begin + 1 : lines.index(PLIST_END)]) + "\n"


def test_metal_probe_carries_its_plist(plan: InstallPlan) -> None:
    """Root installs the plist written into the script, not a file the user
    could rewrite after review; the result file is touched only as the user."""
    probe = plan.metal_probe(HOME / ".voiced/bin/python")
    assert _heredoc(probe.script) == probe.plist.decode()
    assert " cp " not in probe.script and ".config/voiced/launchd" not in probe.script
    for line in probe.script.splitlines():
        if '"$OUT"' in line and not line.startswith("OUT="):
            assert line.count('"$OUT"') == line.count("/usr/bin/sudo -n -u someone --"), line
    # Every command by absolute path; nothing found through PATH as root.
    for word in ("launchctl", "rm", "cat", "chown", "chmod", "seq", "sleep", "test"):
        assert not re.search(rf"(^|[;&|] *|do |then |; ){word} ", probe.script), word


def test_metal_probe_refuses_a_path_that_ends_its_plist(plan: InstallPlan) -> None:
    with pytest.raises(InstallError, match=PLIST_END):
        plan.metal_probe(Path(f"/x\n{PLIST_END}\n/python"))


def _stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!/bin/bash\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _runnable(script: str, stubs: Path, tools: Sequence[str], dst: Path) -> str:
    """The probe script with its absolute tools pointed at stubs and the
    plist written to ``dst``, to run as a normal user."""
    for tool in tools:
        script = re.sub(rf"/(usr/)?(s?bin)/{tool}\b", str(stubs / tool), script)
    return script.replace("/Library/LaunchDaemons/", f"{dst}/")


def test_metal_probe_script_cleans_up_when_bootstrap_fails(
    plan: InstallPlan, tmp_path: Path
) -> None:
    """Run the generated script with stubbed tools: a failed bootstrap must still rm the plist."""
    probe = plan.metal_probe(HOME / ".voiced/bin/python")
    stubs = tmp_path / "bin"
    stubs.mkdir()
    dst = tmp_path / "LaunchDaemons"
    dst.mkdir()
    log = tmp_path / "calls.log"
    _stub(
        stubs,
        "launchctl",
        f'echo "launchctl $*" >> {log}\ncase "$1" in bootstrap) exit 5;; esac\nexit 113',
    )
    _stub(stubs, "sudo", 'shift 4; "$@"')  # sudo -n -u someone -- CMD
    for tool in ("chown", "chmod", "sleep"):
        _stub(stubs, tool, f'echo "{tool} $*" >> {log}')
    _stub(stubs, "rm", f'echo "rm $*" >> {log}; /bin/rm "$@"')
    tools = ("launchctl", "sudo", "chown", "chmod", "sleep", "rm")
    script = tmp_path / "metal-probe.sh"
    script.write_text(_runnable(probe.script, stubs, tools, dst))
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    proc = subprocess.run(["/bin/bash", str(script)], env=env, capture_output=True, text=True)
    assert proc.returncode != 0  # bootstrap failed under set -e
    calls = log.read_text().splitlines()
    installed = f"{dst}/{probe.label}.plist"
    assert f"launchctl bootstrap system {installed}" in calls
    assert f"chown root:wheel {installed}" in calls
    assert calls[-2] == f"launchctl bootout system/{probe.label}"  # the trap's bootout
    assert calls[-1] == f"rm -f {installed}"  # ran even though that bootout failed
    assert not Path(installed).exists()


def test_metal_probe_waits_for_bootout_before_bootstrap(plan: InstallPlan) -> None:
    lines = plan.metal_probe(HOME / ".voiced/bin/python").script.splitlines()
    label = f"{DEFAULT_LAUNCHD_LABEL_PREFIX}.metalprobe"
    bootout = lines.index(f"/bin/launchctl bootout system/{label} 2>/dev/null || true")
    wait = next(i for i, line in enumerate(lines) if f"print system/{label}" in line)
    bootstrap = lines.index(f"/bin/launchctl bootstrap system /Library/LaunchDaemons/{label}.plist")
    assert bootout < wait < bootstrap


def test_metal_probe_quotes_odd_paths(settings: Settings, tmp_path: Path) -> None:
    weird = tmp_path / "dir with spaces $HOME 'q'"
    settings.server.log_dir = weird / "logs"
    p = plan_install(settings, home=weird, uv=None)
    script = p.metal_probe(weird / "python").script
    out = next(line for line in script.splitlines() if line.startswith("OUT="))
    assert shlex.split(out) == [f"OUT={weird / 'logs' / 'metal-probe.json'}"]


# The root step (ADR 0018) ------------------------------------------------------


def test_root_command_is_the_packaged_script(settings: Settings, tmp_path: Path) -> None:
    """One line to paste: bash by path, the script, the mode, the account."""
    p = plan_install(settings, home=tmp_path, uv=None)
    user = pwd.getpwuid(os.getuid()).pw_name
    script = p.privileged_script
    assert script.is_file() and script.name == PRIVILEGED_SCRIPT
    for mode in ("install", "uninstall"):
        assert p.root_command(mode) == f"sudo /bin/bash {shlex.quote(str(script))} {mode} {user}"
    odd = Path("/x y/voiced-privileged.sh")
    line = root_command(odd, "install", "a.b")
    assert shlex.split(line) == ["sudo", "/bin/bash", str(odd), "install", "a.b"]


def test_privileged_script_is_the_active_environments(tmp_path: Path) -> None:
    """The environment bin/voiced points at, by its stable path (Homebrew's
    opt); this package's copy when that environment has none of its own."""
    env = tmp_path / "env"
    copy = env / "lib/python3.12/site-packages/voiced/launchd" / PRIVILEGED_SCRIPT
    own = Path(install_module.__file__).parent / PRIVILEGED_SCRIPT
    assert privileged_script(env) == own
    copy.parent.mkdir(parents=True)
    copy.write_text("#!/bin/bash\n")
    assert privileged_script(env) == copy


def test_foreign_writer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The script and every directory to it: this account's or root's, and
    writable by nobody else (a shared Homebrew is another admin's)."""
    from voiced.launchd.install import foreign_writer

    d = tmp_path / "env"
    d.mkdir()
    script = d / PRIVILEGED_SCRIPT
    script.write_text("#!/bin/bash\n")
    assert foreign_writer(script) is None
    other = os.getuid() + 1
    assert (
        foreign_writer(script, uid=other)
        == f"{script} belongs to {pwd.getpwuid(os.getuid()).pw_name}"
    )
    d.chmod(0o777)
    assert foreign_writer(script) == f"{d} is writable by every account"
    d.chmod(0o1777)  # sticky: nobody else can replace the script
    assert foreign_writer(script) is None
    d.chmod(0o775)
    monkeypatch.setattr(install_module, "group_members", lambda gid: {"root", "someone-else"})
    assert foreign_writer(script) == f"{d} is writable by its group, which has someone-else"
    me = pwd.getpwuid(os.getuid()).pw_name
    monkeypatch.setattr(install_module, "group_members", lambda gid: {"root", me})
    assert foreign_writer(script) is None  # a single-user Homebrew's admin group
    d.chmod(0o755)
    link = tmp_path / "opt"
    link.symlink_to(d)
    assert foreign_writer(link / PRIVILEGED_SCRIPT) is None
    d.chmod(0o777)  # reached through a link: the resolved path is what is checked
    assert foreign_writer(link / PRIVILEGED_SCRIPT) == f"{d} is writable by every account"
    d.chmod(0o755)
    assert foreign_writer(tmp_path / "missing").startswith(
        f"{tmp_path / 'missing'} cannot be checked"
    )


def _chmod_acl(path: Path, *args: str) -> None:
    subprocess.run(["/bin/chmod", *args, str(path)], check=True)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS ACLs")
def test_foreign_writer_refuses_an_acl_that_lets_others_write(tmp_path: Path) -> None:
    """Mode bits are not the whole story: an allow entry for anyone but this
    account or root that grants a write, add or delete refuses."""
    from voiced.launchd.install import foreign_writer

    d = tmp_path / "env"
    d.mkdir()
    script = d / PRIVILEGED_SCRIPT
    script.write_text("#!/bin/bash\n")
    me = pwd.getpwuid(os.getuid()).pw_name
    _chmod_acl(d, "+a", f"user:{me} allow add_file,delete_child")  # this account: fine
    _chmod_acl(d, "+a", "everyone deny add_file")  # a deny takes nothing away from us
    _chmod_acl(d, "+a", "everyone allow read")  # reading changes nothing
    assert foreign_writer(script) is None
    _chmod_acl(d, "+a", "everyone allow add_file,delete_child")
    found = foreign_writer(script)
    assert found is not None
    assert found.startswith(f"{d} has an ACL that lets others write: group everyone ")
    assert "delete_child" in found
    _chmod_acl(d, "-N")
    _chmod_acl(script, "+a", "staff allow write")  # on the script itself
    assert (
        foreign_writer(script) == f"{script} has an ACL that lets others write: group staff write"
    )
    _chmod_acl(script, "-N")
    d.chmod(0o1777)  # sticky does not excuse an ACL
    _chmod_acl(d, "+a", "everyone allow delete_child")
    assert foreign_writer(script) is not None


def test_root_command_names_the_resolved_script(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """a/env -> b/link -> c/real with b open to everyone: the check walks the
    resolved path, and the sudo line names that path, so no link in the
    chain is run through."""
    from voiced.launchd.install import foreign_writer

    real = tmp_path / "c" / "real"
    copy = real / "lib/python3.12/site-packages/voiced/launchd" / PRIVILEGED_SCRIPT
    copy.parent.mkdir(parents=True)
    copy.write_text("#!/bin/bash\n")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "link").symlink_to(real)
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "env").symlink_to(tmp_path / "b" / "link")
    (tmp_path / "b").chmod(0o777)
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "a" / "env"))
    p = plan_install(settings, home=tmp_path, uv=None)
    assert p.environment == tmp_path / "a" / "env"
    assert p.privileged_script == copy
    assert foreign_writer(p.privileged_script) is None
    assert shlex.split(p.root_command("install"))[2] == str(copy)
    (tmp_path / "b").chmod(0o755)


def test_group_members_leave_out_the_setup_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """``admin`` holds _mbsetupuser on every Mac; it is no one who could edit
    a Homebrew keg, so it must not refuse a single-user one. Every other
    member counts, system accounts included."""
    import grp

    from voiced.launchd.install import SETUP_ACCOUNT, group_members

    fake = grp.struct_group(("admin", "*", 80, ["root", SETUP_ACCOUNT, "_www", "alice"]))
    monkeypatch.setattr(grp, "getgrgid", lambda gid: fake)
    monkeypatch.setattr(pwd, "getpwall", lambda: [])
    assert group_members(80) == {"root", "_www", "alice"}


def test_install_prints_no_root_step_for_a_script_others_can_change(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    p = plan_install(
        settings,
        home=home,
        state_dir=home / ".voiced",
        config_file=home / ".config/voiced/config.toml",
        uv=None,
    )
    assert p.root_step_refusal() is None
    monkeypatch.setattr(install_module, "foreign_writer", lambda path: "/opt/x belongs to admin")
    refusal = p.root_step_refusal()
    assert refusal is not None
    assert refusal.startswith(
        "another account could change the root script: /opt/x belongs to admin"
    )


def test_daemon_mismatches(settings: Settings, tmp_path: Path) -> None:
    """The root step installs daemons for ~/.voiced, the default config and
    the default labels; anything else is said, not silently ignored."""
    home = tmp_path / "home"
    default = {"home": home, "uv": None, "state_dir": home / ".voiced"}
    config = home / ".config/voiced/config.toml"
    assert plan_install(settings, config_file=config, **default).daemon_mismatches() == []
    p = plan_install(
        settings, **{**default, "state_dir": tmp_path / "s"}, config_file=tmp_path / "c.toml"
    )
    found = p.daemon_mismatches()
    assert len(found) == 2 and "state dir" in found[0] and "config" in found[1]
    settings.launchd.label_prefix = "org.example.voiced"
    found = plan_install(settings, config_file=config, **default).daemon_mismatches()
    assert found == [
        f"[launchd] label_prefix is org.example.voiced, not {DEFAULT_LAUNCHD_LABEL_PREFIX}"
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


def test_engine_variables_come_from_the_config_not_the_plists(
    settings: Settings, tmp_path: Path
) -> None:
    """The plists carry only the fixed environment the privileged script
    renders (ADR 0018). The services, and the download as a child of a
    voiced that set them (apply_engine_environment), get the rest from the
    config; the download's own argument is only the config path."""
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
    Administrator" is uid 0, so believing it would write a root UserName."""
    monkeypatch.setenv("LOGNAME", "System Administrator")
    monkeypatch.setenv("USER", "System Administrator")
    p = plan_install(settings, home=tmp_path, uv=None)
    assert p.user == pwd.getpwuid(os.getuid()).pw_name != "System Administrator"
    assert p.root_command("install").endswith(f" install {p.user}")


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
    assert calls[1][2:5] == ["-m", "voiced.models", "download"]
    assert p.entry.is_symlink() and os.readlink(p.entry) == str(env / "bin" / "voiced")
    manifest = read_manifest(p.state_dir)
    assert manifest is not None
    assert (manifest.source, manifest.environment) == ("cli", env)
    assert manifest.lock_sha256 == sha256_file(install_module.packaged_lock())
    assert manifest.python == os.path.realpath(env / "bin" / "python")
    assert sorted(x.name for x in p.state_dir.iterdir()) == ["bin", "install.json"]


def test_install_stages_nothing_for_root(settings: Settings, tmp_path: Path) -> None:
    """Root renders the plists itself, so no file a same-user process could
    rewrite after review is left for it: an install writes no plist and no
    script, and removes what an earlier release staged for its sudo script."""
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
    assert [x for x in left if not x.is_relative_to(env)] == []
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
        run_install(p, runner=pytest.fail, query=lambda argv: CommandResult(1), say=lambda _: None)
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
    run_install(p, runner=lambda argv: CommandResult(0), skip_models=True, say=said.append)
    assert stat.S_IMODE(p.config_file.stat().st_mode) == 0o600


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
    # A scratch state dir and config: the daemons would not run this install.
    assert "No root step" in out and f"the state dir is {tmp_path / 'state'}" in out
    assert "sudo" not in out


def test_cli_install_prints_the_root_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The default layout: one line to paste for install, one for uninstall."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("VOICED_CONFIG", raising=False)
    monkeypatch.setenv("VOICED_SERVER__LOG_DIR", str(tmp_path / "logs"))
    assert main(["install", "--dry-run", "--source", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    user = pwd.getpwuid(os.getuid()).pw_name
    script = shlex.quote(str(Path(install_module.__file__).parent / PRIVILEGED_SCRIPT))
    assert f"\n  sudo /bin/bash {script} install {user}\n" in out
    assert f"To remove them later: sudo /bin/bash {script} uninstall {user}\n" in out
    assert "read it" in out and "No root step" not in out
    assert not home.exists()


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
        return CommandResult(1, "", "no network") if "voiced.models" in argv else CommandResult(0)

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


# The root script (ADR 0018): shipped in the package, compiled into the app
# through a symlink. The tests run the file itself.
PRIVILEGED = Path(install_module.__file__).parent / PRIVILEGED_SCRIPT
APP_COPY = (
    Path(__file__).parents[2] / "menubar/Sources/VoicedMenuBarUI/Installer" / PRIVILEGED_SCRIPT
)


def test_the_app_embeds_the_packaged_script() -> None:
    """One script for both: the file the app's `.embedInCode` names is a
    relative symlink to the packaged one (the Swift tests compare the bytes
    compiled in), and the wheel ships the package directory."""
    assert APP_COPY.is_symlink()
    assert not os.path.isabs(os.readlink(APP_COPY))
    assert APP_COPY.resolve() == PRIVILEGED.resolve()
    pyproject = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text())
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["src/voiced"]
    assert PRIVILEGED.read_bytes().startswith(b"#!/bin/bash\n")


@pytest.mark.skipif(sys.platform != "darwin", reason="the script uses macOS tools (plutil)")
def test_privileged_script_renders_what_the_cli_expects(settings: Settings, tmp_path: Path) -> None:
    """The root step renders the plists from the user name alone; they run
    what `voiced install` links, read the config it writes, carry the
    environment the Metal probe gets too, and use the labels status, doctor
    and the watchdog address, whatever this config says about the log dir or
    the Hugging Face cache."""
    home = tmp_path / "home"
    home.mkdir()
    dest = tmp_path / "dest"
    dest.mkdir()
    settings.server.log_dir = tmp_path / "custom-logs"
    settings.install.hf_home = tmp_path / "hf"
    settings.tts.espeak_data = tmp_path / "espeak-ng-data"
    plan = plan_install(
        settings,
        state_dir=home / ".voiced",
        home=home,
        config_file=home / ".config/voiced/config.toml",
        uv=None,
    )
    assert plan.daemon_mismatches() == []
    user = pwd.getpwuid(os.getuid()).pw_name
    env = {"PATH": "/usr/bin:/bin", "VOICED_TEST_DEST": str(dest), "VOICED_TEST_HOME": str(home)}
    result = subprocess.run(
        ["/bin/bash", str(PRIVILEGED), "install", user], env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert sorted(x.name for x in dest.iterdir()) == sorted(
        f"{plan.labels[s]}.plist" for s in SERVICES
    )
    for service in SERVICES:
        rendered = plistlib.loads((dest / f"{plan.labels[service]}.plist").read_bytes())
        command = ["watchdog"] if service == "watchdog" else ["serve", service]
        assert rendered == {
            "Label": f"{DEFAULT_LAUNCHD_LABEL_PREFIX}.{service}",
            "ProgramArguments": [str(plan.entry), *command],
            "UserName": user,
            "RunAtLoad": True,
            # Respawned only while the entry point resolves (not after `brew uninstall`).
            "KeepAlive": {"PathState": {str(plan.entry): True}},
            "ThrottleInterval": 10,
            "ProcessType": "Interactive",
            "WorkingDirectory": str(home),
            "EnvironmentVariables": {
                **plan.environment_variables(),
                "VOICED_STDIO_LOG": str(home / LOG_DIR_IN_HOME / service),
            },
            "AssociatedBundleIdentifiers": [f"{DEFAULT_LAUNCHD_LABEL_PREFIX}.menubar"],
        }
        assert rendered["Label"] == plan.labels[service]
    assert plan.environment_variables()["PATH"] == f"{home}/.voiced/bin:{BASE_PATH}"


@pytest.mark.skipif(sys.platform != "darwin", reason="the script uses macOS tools (plutil)")
def test_privileged_script_refuses_while_another_run_holds_the_lock(tmp_path: Path) -> None:
    """A kernel lock on every macOS the app supports (14 has no lockf): held,
    the run says so; a lock it cannot take at all is not reported as held."""
    import fcntl

    home, dest = tmp_path / "home", tmp_path / "dest"
    home.mkdir()
    dest.mkdir()
    user = pwd.getpwuid(os.getuid()).pw_name
    env = {"PATH": "/usr/bin:/bin", "VOICED_TEST_DEST": str(dest), "VOICED_TEST_HOME": str(home)}

    def install() -> subprocess.CompletedProcess[str]:
        argv = ["/bin/bash", str(PRIVILEGED), "install", user]
        return subprocess.run(argv, env=env, capture_output=True, text=True)

    lock = tmp_path / "dest.lock"
    with lock.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        refused = install()
    assert refused.returncode == 1
    assert "another voiced install or uninstall is running" in refused.stderr
    assert list(dest.iterdir()) == []
    assert install().returncode == 0, "a lock file nobody holds does not block"

    lock.unlink()
    assert install().returncode == 0
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600  # the script's umask 077

    def uninstall() -> subprocess.CompletedProcess[str]:
        argv = ["/bin/bash", str(PRIVILEGED), "uninstall", user]
        return subprocess.run(argv, env=env, capture_output=True, text=True)

    with lock.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        refused = uninstall()
    assert refused.returncode == 1
    assert "another voiced install or uninstall is running" in refused.stderr
    assert len(list(dest.iterdir())) == len(SERVICES), "a held lock refuses uninstall"
    assert uninstall().returncode == 0 and list(dest.iterdir()) == []

    lock.unlink()
    lock.mkdir()  # cannot be opened for writing
    broken = install()
    assert broken.returncode == 1 and "another voiced" not in broken.stderr
    assert f"cannot open {lock}" in broken.stderr


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
            query=_check_ok,
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
    """A reinstall or an update runs while the services listen."""
    _environment(tmp_path / "env")
    p = _plan(settings, tmp_path)
    ports = list(p.ports.values())
    serve = ("Python", ["/x/bin/python3.12", str(p.entry), "serve", "stt"])
    _listening(
        monkeypatch,
        {port: [20 + i] for i, port in enumerate(ports)},
        dict.fromkeys(range(20, 24), serve),
    )
    _install(p)
    assert p.entry.is_symlink()


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
        "stt.enabled=true` and `voiced restart stt`. Its daemon starts and serves nothing."
    ) in said
    assert not any(line.startswith("tts: off") for line in said)
