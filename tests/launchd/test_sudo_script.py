"""The sudo script `voiced install` prints, run with stand-ins for root's tools,
next to the app's privileged script on the same cases (ADR 0018): both refuse
the same accounts, homes, held locks and other accounts' installs, with
nothing written, and both add the same firewall exception."""

from __future__ import annotations

import dataclasses
import fcntl
import os
import plistlib
import pwd
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from voiced.config import SERVICES, Settings
from voiced.launchd.install import InstallPlan, plan_install, write_staging

from .test_install import PRIVILEGED

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="the scripts use macOS tools (plutil, stat -f)"
)

USER = pwd.getpwuid(os.getuid()).pw_name
FIREWALL_ON = "Firewall is enabled. (State = 1)"


@dataclass
class Machine:
    """A scratch /Library/LaunchDaemons, lock, home and firewall."""

    root: Path
    home: Path
    dest: Path
    calls: Path  # what the stand-ins were asked to do
    firewall: Path

    @property
    def lock(self) -> Path:
        return self.dest.with_name("dest.lock")  # where the privileged script's test mode has it

    def installed(self) -> list[str]:
        return sorted(p.name for p in self.dest.iterdir())

    def log(self) -> list[str]:
        return self.calls.read_text().splitlines() if self.calls.exists() else []


def _stub(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(f"#!/bin/bash\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture
def machine(tmp_path: Path) -> Machine:
    home = tmp_path / "home"
    env = home / ".voiced/envs/20260101T000000/bin"
    env.mkdir(parents=True)
    (env / "voiced").write_text("")
    (env / "python").write_text("")  # a regular file the user owns, inside the home
    (home / ".voiced/bin").mkdir()
    (home / ".voiced/bin/voiced").symlink_to(env / "voiced")
    dest = tmp_path / "dest"
    dest.mkdir()
    calls = tmp_path / "calls.log"
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    # Root's view: `id -u` says 0, launchctl and chown only log, sudo -u runs the command.
    _stub(bin_, "id", 'if [ "$*" = "-u" ]; then echo 0; else exec /usr/bin/id "$@"; fi')
    _stub(
        bin_,
        "launchctl",
        f'echo "launchctl $*" >> {calls}\ncase "$1" in print) exit 113;; esac',
    )
    _stub(bin_, "chown", f'echo "chown $*" >> {calls}')
    _stub(bin_, "sudo", 'while [ "$1" != "--" ]; do shift; done; shift; exec "$@"')
    firewall = _stub(
        tmp_path,
        "socketfilterfw",
        f'echo "socketfilterfw $*" >> {calls}\n'
        f'[ "$1" = --getglobalstate ] && echo "${{FW_STATE:-{FIREWALL_ON}}}"\ntrue',
    )
    return Machine(tmp_path, home, dest, calls, firewall)


def _plan(settings: Settings, machine: Machine, **changes: object) -> InstallPlan:
    settings.launchd.label_prefix = "io.github.julianbeaulieu.voiced"
    plan = plan_install(
        settings,
        state_dir=machine.home / ".voiced",
        home=machine.home,
        staging_dir=machine.root / "staging",
        config_file=machine.home / ".config/voiced/config.toml",
        uv=None,
    )
    plan = dataclasses.replace(plan, **{"user": USER, **changes})  # type: ignore[arg-type]
    write_staging(plan)
    return plan


Run = Callable[..., subprocess.CompletedProcess[str]]


def _sudo_script(plan: InstallPlan, machine: Machine) -> Run:
    """The printed script with its fixed system paths pointed at the scratch machine."""
    text = (
        plan.sudo_install_script()
        .replace("/Library/LaunchDaemons", str(machine.dest))
        .replace("/var/run/voiced-privileged.lock", str(machine.lock))
        .replace("/usr/libexec/ApplicationFirewall/socketfilterfw", str(machine.firewall))
    )
    script = machine.root / "install-daemons.sh"
    script.write_text(text)

    def run(*, as_root: bool = True, **env: str) -> subprocess.CompletedProcess[str]:
        path = f"{machine.root / 'bin'}:/usr/bin:/bin:/usr/sbin:/sbin"
        if not as_root:
            path = path.split(":", 1)[1]
        return subprocess.run(
            ["/bin/bash", str(script)],
            env={"PATH": path, **env},
            capture_output=True,
            text=True,
        )

    return run


def _privileged(machine: Machine, user: str, home: Path | None = None) -> Run:
    """The app's script in its test mode, on the same scratch machine."""

    def run(**env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/bash", str(PRIVILEGED), "install", user],
            env={
                "PATH": "/usr/bin:/bin",
                "VOICED_TEST_DEST": str(machine.dest),
                "VOICED_TEST_HOME": str(home or machine.home),
                "VOICED_TEST_FW": str(machine.firewall),
                **env,
            },
            capture_output=True,
            text=True,
        )

    return run


def test_installs_the_staged_plists_and_bootstraps_them(
    settings: Settings, machine: Machine
) -> None:
    plan = _plan(settings, machine)
    result = _sudo_script(plan, machine)(FW_STATE="Firewall is disabled. (State = 0)")
    assert result.returncode == 0, result.stderr
    assert machine.installed() == sorted(f"{plan.labels[s]}.plist" for s in SERVICES)
    for service in SERVICES:
        installed = (machine.dest / f"{plan.labels[service]}.plist").read_bytes()
        assert installed == plan.staged_plist(service).read_bytes()
    bootstraps = [c for c in machine.log() if c.startswith("launchctl bootstrap")]
    assert len(bootstraps) == 3
    assert not any("--add" in c for c in machine.log()), "the firewall is off"


def test_refuses_without_root(settings: Settings, machine: Machine) -> None:
    result = _sudo_script(_plan(settings, machine), machine)(as_root=False)
    assert result.returncode == 1 and "run this with sudo" in result.stderr
    assert machine.installed() == []


def _other_accounts_install(machine: Machine, owner: str) -> None:
    label = "io.github.julianbeaulieu.voiced.stt"
    (machine.dest / f"{label}.plist").write_bytes(plistlib.dumps({"UserName": owner}))


# Each case: what to change, the message both scripts refuse with. Nothing may be written.
REFUSALS: dict[str, tuple[Callable[[Machine], dict[str, object]], str]] = {
    "root": (lambda m: {"user": "root"}, "root is not a regular account"),
    "a system account": (lambda m: {"user": "daemon"}, "daemon is not a regular account"),
    # uid 4294967294, which id prints as -2: out of range either way
    "nobody": (lambda m: {"user": "nobody"}, "nobody is not a regular account"),
    "no such user": (lambda m: {"user": "no-such-user-voiced"}, "no such user"),
    "a home of another account": (lambda m: {"home": Path("/usr")}, "/usr is not owned by"),
}


@pytest.mark.parametrize("case", REFUSALS)
def test_both_scripts_refuse_the_same_accounts(
    settings: Settings, machine: Machine, case: str
) -> None:
    change, message = REFUSALS[case]
    changes = change(machine)
    plan = _plan(settings, machine, **changes)
    for name, run in (
        ("sudo script", _sudo_script(plan, machine)),
        ("privileged script", _privileged(machine, plan.user, plan.home)),
    ):
        result = run()
        assert result.returncode == 1, f"{name}: {result.stdout}{result.stderr}"
        assert message in result.stderr, f"{name}: {result.stderr}"
        assert machine.installed() == [], name


def test_both_scripts_refuse_while_the_other_holds_the_lock(
    settings: Settings, machine: Machine
) -> None:
    """One lock file for both: a CLI install and the app's never overlap."""
    plan = _plan(settings, machine)
    runs = {"sudo script": _sudo_script(plan, machine), "privileged": _privileged(machine, USER)}
    with machine.lock.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        for name, run in runs.items():
            result = run()
            assert result.returncode == 1, name
            assert "another voiced install or uninstall is running" in result.stderr, name
    assert machine.installed() == []
    assert runs["sudo script"]().returncode == 0, "a lock file nobody holds does not block"


def test_a_lock_that_cannot_be_taken_is_not_reported_as_held(
    settings: Settings, machine: Machine
) -> None:
    machine.lock.mkdir()
    result = _sudo_script(_plan(settings, machine), machine)()
    assert result.returncode == 1
    assert f"cannot open {machine.lock}" in result.stderr and "another" not in result.stderr


def test_another_accounts_install_is_kept_by_both(settings: Settings, machine: Machine) -> None:
    plan = _plan(settings, machine)
    for run in (_sudo_script(plan, machine), _privileged(machine, USER)):
        _other_accounts_install(machine, "root")
        result = run()
        assert result.returncode == 1
        assert "voiced is already installed for root on this Mac" in result.stderr
        assert machine.installed() == ["io.github.julianbeaulieu.voiced.stt.plist"]
        stt = plistlib.loads((machine.dest / machine.installed()[0]).read_bytes())
        assert stt == {"UserName": "root"}


def test_an_orphaned_install_is_taken_over_by_both(settings: Settings, machine: Machine) -> None:
    plan = _plan(settings, machine)
    for run in (_sudo_script(plan, machine), _privileged(machine, USER)):
        _other_accounts_install(machine, "deleted-account-voiced")
        result = run()
        assert result.returncode == 0, result.stderr
        assert "orphaned install of deleted account deleted-account-voiced" in result.stdout
        stt = plistlib.loads(
            (machine.dest / "io.github.julianbeaulieu.voiced.stt.plist").read_bytes()
        )
        assert stt["UserName"] == USER


def test_both_allow_the_services_python_when_the_firewall_is_on(
    settings: Settings, machine: Machine
) -> None:
    python = os.path.realpath(machine.home / ".voiced/envs/20260101T000000/bin/python")
    plan = _plan(settings, machine)
    for run in (_sudo_script(plan, machine), _privileged(machine, USER)):
        machine.calls.unlink(missing_ok=True)
        result = run()
        assert result.returncode == 0, result.stderr
        assert f"firewall: {python} may accept incoming connections" in result.stdout
        log = machine.log()
        assert f"socketfilterfw --add {python}" in log
        assert f"socketfilterfw --unblockapp {python}" in log


def test_a_python_outside_the_home_is_left_alone(settings: Settings, machine: Machine) -> None:
    """A Homebrew keg's Python is shared with every brewed program. The sudo
    script says so and succeeds; the app's script exits 5 for the app."""
    python = machine.home / ".voiced/envs/20260101T000000/bin/python"
    python.unlink()
    outside = machine.root / "keg/python"
    outside.parent.mkdir()
    outside.write_text("")
    python.symlink_to(outside)
    plan = _plan(settings, machine)
    result = _sudo_script(plan, machine)()
    assert result.returncode == 0, result.stderr
    assert f"{os.path.realpath(outside)} is outside" in result.stdout
    assert _privileged(machine, USER)().returncode == 5
    assert not any("--add" in c for c in machine.log())


def test_a_failed_firewall_exception_is_a_warning(settings: Settings, machine: Machine) -> None:
    """After the daemons are in, the firewall cannot fail the install."""
    _stub(
        machine.firewall.parent,
        machine.firewall.name,
        f'[ "$1" = --getglobalstate ] && echo "{FIREWALL_ON}" && exit 0\nexit 1',
    )
    result = _sudo_script(_plan(settings, machine), machine)()
    assert result.returncode == 0
    assert "warning: the firewall exception could not be added" in result.stderr
    assert len(machine.installed()) == 3


@pytest.mark.parametrize("problem", ["a directory", "another account's file"])
def test_a_python_that_is_not_the_users_file_is_not_allowed(
    settings: Settings, machine: Machine, problem: str
) -> None:
    python = machine.home / ".voiced/envs/20260101T000000/bin/python"
    if problem == "a directory":
        python.unlink()
        python.mkdir()
    else:  # stat reports root as its owner
        _stub(
            machine.root / "bin",
            "stat",
            'case "${!#}" in */python) echo 0;; *) exec /usr/bin/stat "$@";; esac',
        )
    result = _sudo_script(_plan(settings, machine), machine)()
    assert result.returncode == 0
    assert "warning: the firewall exception could not be added" in result.stderr
    assert not any("--add" in c for c in machine.log())
