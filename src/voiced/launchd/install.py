"""Installer: plan, render and execute ``voiced install`` / ``uninstall``.

The plan is a plain value (:class:`InstallPlan`) so ``--dry-run`` and the tests
can show every step without touching the machine. The step that needs root
(rendering the plists into ``/Library/LaunchDaemons`` and bootstrapping them)
is never executed here: it is :data:`PRIVILEGED_SCRIPT`, the script the menu
bar app runs too (ADR 0018), and ``voiced install`` prints the one ``sudo``
line that runs it. See ADR 0006 for the layout, and ADR 0017 for the state
directory (``voiced.layout``).
"""

from __future__ import annotations

import contextlib
import errno
import grp
import os
import pwd
import shlex
import shutil
import stat
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import voiced
from voiced.config import (
    DEFAULT_LAUNCHD_LABEL_PREFIX,
    ENV_HF_HOME,
    SERVICES,
    DaemonName,
    Settings,
    config_path,
)
from voiced.launchd.control import (
    Runner,
    bootout_argv,
    bootstrap_argv,
    plist_path,
    print_argv,
    run_command,
    run_streaming,
)
from voiced.launchd.plist import log_paths, render_plist
from voiced.launchd.ports import foreign_listeners
from voiced.launchd.progress import Progress
from voiced.layout import (
    ENVS,
    MANIFEST,
    PYTHONS,
    Manifest,
    ManifestError,
    NotVoiceds,
    Origin,
    SymlinkedStateDir,
    built_environments,
    claim,
    entry_point,
    install_lock,
    is_legacy_venv,
    legacy_venv_paths,
    link_entry_point,
    partial_environments,
    same_path,
    sha256_file,
    stable_environment,
    write_manifest,
)

Service = DaemonName
METAL_PROBE_FILE = "metal-probe.json"

# The root step, shipped in the package and compiled into the menu bar app
# (whose copy is a symlink to this one, ADR 0018). It renders the plists from
# the user name alone, for the default layout below.
PRIVILEGED_SCRIPT = "voiced-privileged.sh"
# What the plists it renders run and read, relative to the home.
DAEMON_STATE_DIR = Path(".voiced")
DAEMON_CONFIG = Path(".config/voiced/config.toml")
# What earlier releases staged in <config dir>/launchd for a sudo script to
# copy; `install` removes them, so no stale script can install them.
LEGACY_STAGING = "launchd"
LEGACY_SCRIPTS = ("install-daemons.sh", "uninstall-daemons.sh", "metal-probe.sh")

# Homebrew (both architectures) first so espeak-ng and ffmpeg are found, then
# the system paths. The state directory's bin is prepended per plan.
BASE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
DEFAULT_PYTHON_VERSION = "3.12"

Say = Callable[[str], None]


def repo_root() -> Path | None:
    """The checkout this package was imported from, if it is a dev checkout."""
    candidate = Path(voiced.__file__).resolve().parents[2]
    pyproject = candidate / "pyproject.toml"
    if pyproject.is_file() and 'name = "voiced"' in pyproject.read_text(errors="replace"):
        return candidate
    return None


# The dependency lock every install path uses: `uv export` of uv.lock with
# the engine extras, shipped in the package (ADR 0017). A test keeps it equal
# to uv.lock. The build lock pins the build backend for --source builds.
LOCK = Path("src/voiced/requirements.lock")
BUILD_LOCK = Path("src/voiced/build-requirements.lock")


def packaged_lock() -> Path:
    return Path(voiced.__file__).resolve().parent / LOCK.name


def example_config_path() -> Path | None:
    """``config.example.toml``: shipped inside the wheel, or at the repo root."""
    packaged = Path(voiced.__file__).resolve().parent / "config.example.toml"
    if packaged.is_file():
        return packaged
    root = repo_root()
    if root is not None and (root / "config.example.toml").is_file():
        return root / "config.example.toml"
    return None


def example_config_text() -> str:
    path = example_config_path()
    if path is not None:
        return path.read_text()
    return (
        "# voiced configuration. All keys are optional; see\n"
        "# https://github.com/JulianBeaulieu/voiced/blob/main/config.example.toml\n"
    )


def installing_user() -> str:
    """The account running the installer, from its uid. Not
    ``getpass.getuser()``, which believes ``LOGNAME`` and ``USER``: with
    ``LOGNAME="System Administrator"`` it would name uid 0 as the daemons'
    ``UserName``."""
    return pwd.getpwuid(os.getuid()).pw_name


def find_uv() -> str | None:
    return shutil.which("uv")


def is_root() -> bool:
    return os.geteuid() == 0


def q(path: Path | str) -> str:
    """Shell-quote a path for the generated scripts."""
    return shlex.quote(str(path))


def privileged_script(environment: Path) -> Path:
    """:data:`PRIVILEGED_SCRIPT` as installed in ``environment``, so the
    printed path is that environment's; this package's own copy when there
    is no single one there (an editable checkout, or an environment not
    built yet)."""
    pattern = f"lib/python3*/site-packages/voiced/launchd/{PRIVILEGED_SCRIPT}"
    found = sorted(environment.glob(pattern))
    if len(found) == 1:
        return found[0]
    return Path(voiced.__file__).resolve().parent / "launchd" / PRIVILEGED_SCRIPT


def _account(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return f"uid {uid}"


# Not a person: macOS's setup assistant account, a member of `admin` on every
# Mac, so of the group that owns Homebrew's directories. On a stock Mac it is
# the only account below uid 500 in wheel, staff or admin besides root.
SETUP_ACCOUNT = "_mbsetupuser"


def group_members(gid: int) -> set[str]:
    """The accounts in group ``gid`` but the setup account: its listed
    members and the accounts whose primary group it is."""
    try:
        members = set(grp.getgrgid(gid).gr_mem)
    except KeyError:
        members = set()
    members |= {entry.pw_name for entry in pwd.getpwall() if entry.pw_gid == gid}
    return members - {SETUP_ACCOUNT}


# acl_perm_t names that let a holder change a file or a directory's entries.
ACL_WRITES = frozenset(
    (
        "write add_file append add_subdirectory delete delete_child writeattr "
        "writeextattr writesecurity chown"
    ).split()
)
ACL_TYPE_EXTENDED = 0x00000100


def acl_entries(path: Path) -> list[str]:
    """``path``'s extended ACL as ``acl_to_text`` lines
    (``tag:uuid:name:id:allow|deny:perms``); empty without one."""
    import ctypes
    import ctypes.util

    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    libc.acl_get_file.restype = ctypes.c_void_p
    libc.acl_get_file.argtypes = [ctypes.c_char_p, ctypes.c_int]
    libc.acl_to_text.restype = ctypes.c_void_p
    libc.acl_to_text.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    libc.acl_free.argtypes = [ctypes.c_void_p]
    acl = libc.acl_get_file(os.fsencode(path), ACL_TYPE_EXTENDED)
    if not acl:
        if ctypes.get_errno() == errno.ENOENT:  # no ACL
            return []
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
    try:
        text = libc.acl_to_text(acl, None)
        if not text:
            raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
        try:
            lines = ctypes.string_at(text).decode().splitlines()
        finally:
            libc.acl_free(text)
    finally:
        libc.acl_free(acl)
    return [line for line in lines if line and not line.startswith("!#acl")]


def acl_writer(path: Path, uid: int) -> str | None:
    """An ACL entry on ``path`` that allows anyone but ``uid`` and root to
    change it, as ``ls -le`` would name it, or None."""
    for entry in acl_entries(path):
        fields = entry.split(":")
        if len(fields) < 6:
            return f"an ACL entry it cannot read ({entry})"
        tag, _, name, ident, kind, perms = fields[:6]
        if kind != "allow" or not ACL_WRITES & set(perms.split(",")):
            continue
        if tag == "user" and ident in (str(uid), "0"):
            continue
        return f"{tag} {name} {perms}"
    return None


def foreign_writer(path: Path, uid: int | None = None) -> str | None:
    """Why an account other than ``uid`` (default: this one) and root could
    change what ``path`` holds, or None. The user runs the root script with
    sudo, so it, and every directory on the way to it, must belong to them
    or root, with no mode bit and no ACL entry letting anyone else write.
    A sticky directory lets nobody replace an entry they do not own.

    The path is checked resolved, and the sudo line names the resolved path
    (:meth:`InstallPlan.root_command`): what root runs then has no link in
    it to swap, however many hops led there. A volume mounted with
    ownership ignored shows every file as the user's and cannot be judged."""
    uid = os.getuid() if uid is None else uid
    me = _account(uid)
    resolved = path.resolve()
    for part in (resolved, *resolved.parents):
        try:
            st = os.lstat(part)
            acl = acl_writer(part, uid) if sys.platform == "darwin" else None
        except OSError as exc:
            return f"{part} cannot be checked ({exc.strerror})"
        if st.st_uid not in (0, uid):
            return f"{part} belongs to {_account(st.st_uid)}"
        if acl is not None:
            return f"{part} has an ACL that lets others write: {acl}"
        if stat.S_ISDIR(st.st_mode) and st.st_mode & stat.S_ISVTX:
            continue
        if st.st_mode & stat.S_IWOTH:
            return f"{part} is writable by every account"
        if st.st_mode & stat.S_IWGRP:
            others = sorted(group_members(st.st_gid) - {me, "root"})
            if others:
                return f"{part} is writable by its group, which has {', '.join(others)}"
    return None


def root_command(script: Path, mode: Literal["install", "uninstall"], user: str) -> str:
    """The line the user pastes. ``/bin/bash`` by path: the script needs no
    execute bit and no ``PATH``; sudo resets the environment."""
    return f"sudo /bin/bash {shlex.join([str(script), mode, user])}"


@dataclass(frozen=True)
class InstallPlan:
    """Everything ``voiced install`` is going to do, as data.

    ``environment`` is what ``<state_dir>/bin/voiced`` will point at: the
    environment of the ``voiced`` running the install, or with ``source``
    a new one built from that checkout into ``<state_dir>/envs``
    (ADR 0017).
    """

    user: str
    home: Path
    state_dir: Path
    config_file: Path
    log_dir: Path
    labels: dict[str, str]  # every DaemonName including "metalprobe"
    environment: Path
    origin: Origin
    source: Path | None = None
    # How the user chose state_dir, for messages.
    state_dir_flag: str = "[install] venv_dir"
    python: str = ""
    uv: str | None = None
    # HF_HOME and the espeak-ng overrides as the config sets them, which is
    # how the services get them (ADR 0018).
    engine_env: dict[str, str] = field(default_factory=dict)
    # The four service ports, by their config key, and the address the
    # services bind them on, for the ports step.
    ports: dict[str, int] = field(default_factory=dict)
    bind: str = "127.0.0.1"

    # Paths -------------------------------------------------------------

    @property
    def entry(self) -> Path:
        return entry_point(self.state_dir)

    @property
    def hf_home(self) -> Path | None:
        value = self.engine_env.get(ENV_HF_HOME)
        return Path(value) if value else None

    @property
    def python_bin(self) -> Path:
        """The new environment's interpreter."""
        return self.environment / "bin" / "python"

    @property
    def lock(self) -> Path:
        return self.source / LOCK if self.source is not None else packaged_lock()

    @property
    def legacy_staging_dir(self) -> Path:
        return self.config_file.parent / LEGACY_STAGING

    def legacy_staged_files(self) -> list[Path]:
        """What an earlier ``voiced install`` staged for its sudo script."""
        names = [f"{label}.plist" for label in self.labels.values()]
        return [self.legacy_staging_dir / name for name in (*names, *LEGACY_SCRIPTS)]

    # The root step -----------------------------------------------------

    @property
    def privileged_script(self) -> Path:
        """Resolved: the sudo line names the file :func:`foreign_writer`
        checked, with no link on the way."""
        return privileged_script(self.environment).resolve()

    def root_command(self, mode: Literal["install", "uninstall"]) -> str:
        return root_command(self.privileged_script, mode, self.user)

    def root_step_refusal(self) -> str | None:
        """Why no root step is printed for this plan, or None: the daemons
        would not run this install, or another account could change the
        script before the user runs it as root."""
        mismatches = self.daemon_mismatches()
        if mismatches:
            return (
                "the daemons run ~/.voiced/bin/voiced with ~/.config/voiced/config.toml "
                f"under the default labels, and here {'; '.join(mismatches)}. "
                "Run `voiced serve` yourself instead."
            )
        problem = foreign_writer(self.privileged_script)
        if problem is not None:
            return (
                f"another account could change the root script: {problem}. Install voiced "
                "into an environment of your own (`voiced install --source <checkout>`)."
            )
        return None

    def daemon_mismatches(self) -> list[str]:
        """Where this plan differs from what the root step installs: the
        daemons run ``~/.voiced/bin/voiced`` with ``~/.config/voiced/config.toml``
        under the default labels, whatever this config says (ADR 0018)."""
        found: list[str] = []
        state = self.home / DAEMON_STATE_DIR
        if os.path.realpath(self.state_dir) != os.path.realpath(state):
            found.append(f"the state dir is {self.state_dir}, not {state}")
        config = self.home / DAEMON_CONFIG
        if os.path.realpath(self.config_file) != os.path.realpath(config):
            found.append(f"the config is {self.config_file}, not {config}")
        prefix = self.labels["stt"].removesuffix(".stt")
        if prefix != DEFAULT_LAUNCHD_LABEL_PREFIX:
            found.append(f"[launchd] label_prefix is {prefix}, not {DEFAULT_LAUNCHD_LABEL_PREFIX}")
        return found

    def environment_variables(self) -> dict[str, str]:
        """The fixed environment the root step renders into the daemons'
        plists (a test runs it to compare), and the Metal probe's."""
        return {
            "HOME": str(self.home),
            "PATH": f"{self.entry.parent}:{BASE_PATH}",
            "VOICED_CONFIG": str(self.config_file),
            "PYTHONUNBUFFERED": "1",
        }

    # Shell commands ----------------------------------------------------

    def build_commands(self) -> list[list[str]]:
        """With ``source``: create the environment and install the locked
        dependencies, then voiced itself, with hashes throughout. Empty
        otherwise: the environment exists already. ``UV_NO_CONFIG``: a
        ``uv.toml`` of the user's cannot change what is installed."""
        if self.source is None:
            return []
        env = ["/usr/bin/env", "UV_NO_CONFIG=1"]
        uv = self.uv or "uv"
        python = str(self.python_bin)
        managed = f"UV_PYTHON_INSTALL_DIR={self.state_dir / PYTHONS}"
        version = self.python or DEFAULT_PYTHON_VERSION
        install = [*env, uv, "pip", "install", "--python", python, "--no-deps"]
        return [
            [
                *env,
                managed,
                uv,
                "venv",
                "--managed-python",
                "--python",
                version,
                str(self.environment),
            ],
            [*install, "--require-hashes", "-r", str(self.lock)],
            [*install, "--build-constraints", str(self.source / BUILD_LOCK), str(self.source)],
        ]

    def check_command(self) -> list[str]:
        """Fails unless the environment has the engines (the ``stt`` and
        ``tts`` extras), before anything points at it."""
        return [str(self.python_bin), "-P", "-c", ENGINE_CHECK]

    def model_download_command(self, report: bool = False) -> list[str]:
        """``voiced.models`` in the new environment. The engine variables
        come with this process's environment, which ``voiced`` set from the
        config (``apply_engine_environment``), so the models land in the
        cache the services read. ``report``: byte progress lines for
        ``--json-progress``."""
        argv = [str(self.python_bin), "-P", "-m", "voiced.models", "download"]
        argv += ["--config", str(self.config_file)]
        return [*argv, "--report"] if report else argv

    # One-shot Metal probe -----------------------------------------------

    def metal_probe(self, python: Path) -> MetalProbe:
        """A self-removing one-shot daemon that runs ``voiced.launchd.metal_probe``.

        The plist is inside the script, so root reads nothing else the user
        can write; the result file in the user's log dir is removed and read
        as the user."""
        label = f"{DEFAULT_LAUNCHD_LABEL_PREFIX}.metalprobe"  # never the config's
        out = self.log_dir / METAL_PROBE_FILE
        plist = render_plist(
            label=label,
            program_arguments=[
                str(python),
                "-m",
                "voiced.launchd.metal_probe",
                "--out",
                str(out),
            ],
            log_dir=self.log_dir,
            user_name=self.user,
            working_directory=self.home,
            environment=self.environment_variables(),
            keep_alive=False,
        )
        body = plist.decode()
        if f"\n{PLIST_END}\n" in f"\n{body}":
            raise InstallError(f"a path in the probe's plist contains a line {PLIST_END}")
        dst = plist_path(label)
        as_user = f"/usr/bin/sudo -n -u {q(self.user)} --"
        bootout, probe, bootstrap = (
            shlex.join([LAUNCHCTL, *argv[1:]])
            for argv in (bootout_argv(label), print_argv(label), bootstrap_argv(label))
        )
        # Under `set -e` a failing bootout inside the trap would abort before rm.
        cleanup = f"{bootout} 2>/dev/null || true; /bin/rm -f {q(dst)}"
        stderr_log = log_paths(self.log_dir, "metalprobe").stderr
        no_result = f"no result after 60 s; see {stderr_log}"
        lines = [
            "#!/bin/bash",
            "# voiced: one-shot LaunchDaemon that checks MLX/Metal without a GUI session.",
            "# Installs the job, waits for its result, then removes it again (also on error).",
            "# Its plist is below; root reads no other file. Review, then run with sudo.",
            "set -euo pipefail",
            "umask 022",
            f"OUT={q(out)}",
            # The trap guarantees no RunAtLoad plist is left behind.
            f"trap {q(cleanup)} EXIT",
            f'{as_user} /bin/rm -f -- "$OUT"',
            f"{bootout} 2>/dev/null || true",
            # bootout is asynchronous: bootstrapping while the old instance is
            # still being torn down fails with error 5.
            f"for _ in $(/usr/bin/seq 1 30); do {probe} >/dev/null 2>&1 || break;"
            " /bin/sleep 1; done",
            f"/bin/rm -f {q(dst)}",
            f"/bin/cat > {q(dst)} <<'{PLIST_END}'",
            body.rstrip("\n"),
            PLIST_END,
            f"/usr/sbin/chown root:wheel {q(dst)}",
            f"/bin/chmod 644 {q(dst)}",
            bootstrap,
            f'for _ in $(/usr/bin/seq 1 60); do {as_user} /bin/test -f "$OUT" && break;'
            " /bin/sleep 1; done",
            f'if {as_user} /bin/test -f "$OUT"; then {as_user} /bin/cat -- "$OUT"; else',
            f"  echo {q(no_result)}; exit 1",
            "fi",
            "",
        ]
        return MetalProbe(
            label=label,
            script_path=self.config_file.parent / "metal-probe.sh",
            plist=plist,
            script="\n".join(lines),
            result_file=out,
        )


# Ends the probe's plist inside its script; a quoted heredoc expands nothing.
PLIST_END = "VOICED_PLIST_END"
LAUNCHCTL = "/bin/launchctl"


@dataclass(frozen=True)
class MetalProbe:
    label: str
    script_path: Path
    plist: bytes
    script: str
    result_file: Path

    def write(self) -> None:
        self.script_path.parent.mkdir(parents=True, exist_ok=True)
        self.script_path.write_text(self.script)


ENGINE_CHECK = (
    "import mlx.core, parakeet_mlx, mlx_audio, voiced; print('voiced', voiced.__version__)"
)


def new_environment_dir(state_dir: Path) -> Path:
    """A fresh ``envs/<id>``: a build never writes into an environment a
    daemon may be running."""
    base = state_dir / ENVS / time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    candidate, n = base, 1
    while candidate.exists():
        n += 1
        candidate = base.with_name(f"{base.name}-{n}")
    return candidate


def plan_install(
    settings: Settings,
    *,
    state_dir: Path | None = None,
    python: str | None = None,
    source: Path | None = None,
    home: Path | None = None,
    config_file: Path | None = None,
    uv: str | Literal["auto"] | None = "auto",
    prefix: str | Path | None = None,
    origin: Origin | None = None,
) -> InstallPlan:
    """Resolve CLI flags, config and defaults into an :class:`InstallPlan`.

    Without ``source`` the environment is the one this command runs from
    (``prefix``, default ``sys.prefix``); with it, a new one under
    ``<state_dir>/envs``. ``origin`` is who made the environment, for the
    manifest: by default ``homebrew`` for a keg and ``cli`` otherwise; the
    app passes ``app``.
    """
    config_file = config_file or config_path()
    state = (state_dir or settings.install.venv_dir).expanduser().absolute()
    if source is None:
        environment, found = stable_environment(sys.prefix if prefix is None else prefix)
    else:
        source = source.expanduser().absolute()
        environment, found = new_environment_dir(state), "cli"
    return InstallPlan(
        user=installing_user(),
        home=home or Path.home(),
        state_dir=state,
        config_file=config_file,
        log_dir=settings.server.log_dir,
        labels={name: settings.launchd.label(name) for name in (*SERVICES, "metalprobe")},
        environment=environment,
        origin=origin or found,
        source=source,
        state_dir_flag="[install] venv_dir" if state_dir is None else "--state-dir",
        python=python if python is not None else settings.install.python,
        uv=find_uv() if uv == "auto" else uv,
        engine_env=settings.engine_environment(),
        ports=settings.ports(),
        bind=settings.server.bind,
    )


def plan_summary(plan: InstallPlan) -> str:
    """Human-readable plan, printed before both the dry and the real run."""
    if plan.source is not None:
        built = f"built from {plan.source} with uv ({plan.uv or 'not found'})"
    elif plan.origin == "homebrew":
        built = "Homebrew's (`brew upgrade voiced` replaces it)"
    else:
        built = "the one this command runs from"
    lines = [
        f"user           {plan.user}",
        f"state dir      {plan.state_dir}",
        f"environment    {plan.environment}",
        f"               {built}",
        f"lock           {plan.lock}",
        f"config         {plan.config_file}",
        f"log dir        {plan.log_dir}",
        f"hf_home        {plan.hf_home or '(Hugging Face default)'}",
    ]
    lines += [f"{key:<14} {value}" for key, value in plan.engine_env.items() if key != ENV_HF_HOME]
    for service in SERVICES:
        lines.append(f"label {service:<9}{plan.labels[service]}")
    return "\n".join(lines)


def remove_legacy_staging(plan: InstallPlan, say: Say) -> None:
    """Remove what an earlier release staged for its sudo script, and the
    directory if that empties it."""
    for path in plan.legacy_staged_files():
        if path.is_file() or path.is_symlink():
            path.unlink()
            say(f"removed {path}")
    with contextlib.suppress(OSError):  # missing, or holds something else
        plan.legacy_staging_dir.rmdir()


def write_default_config(config_file: Path) -> bool:
    """Create the config file from the example if it does not exist.

    It is created 0600: it is where ``api_key`` goes, home directories on
    macOS are readable by the ``staff`` group every local user is in, and the
    daemons run as this user, so nobody else needs to read it.
    """
    if config_file.exists():
        return False
    config_file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(config_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(example_config_text())
    return True


class InstallError(RuntimeError):
    pass


def current_manifest(plan: InstallPlan) -> Manifest | None:
    """:func:`voiced.layout.claim` with the error said in installer terms."""
    try:
        return claim(plan.state_dir)
    except SymlinkedStateDir as exc:
        raise InstallError(f"Not installing into {plan.state_dir}: {exc}.") from exc
    except NotVoiceds as exc:
        raise InstallError(
            f"{plan.state_dir} is not voiced's: {exc}. "
            f"Set {plan.state_dir_flag} to another directory, or move those files."
        ) from exc
    except ManifestError as exc:
        raise InstallError(str(exc)) from exc


def remove_legacy_venv(state_dir: Path, say: Say) -> None:
    """Remove a pre-ADR 0017 venv from the state directory, keeping
    ``bin/voiced`` (the entry point) and anything that is not the venv's."""
    for path in legacy_venv_paths(state_dir):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
        say(f"removed {path}")


def prune_environments(state_dir: Path, keep: Sequence[Path | None], say: Say) -> None:
    """Remove the environments voiced built other than ``keep`` (the new and
    the previous one: daemons not yet restarted still run the previous)."""
    kept = [p for p in keep if p is not None]
    for env in built_environments(state_dir):
        if any(same_path(env, k) for k in kept if k.exists()):
            continue
        shutil.rmtree(env)
        say(f"removed unused environment {env}")


@dataclass(frozen=True)
class Step:
    """One step of ``voiced install``, in order: ``id`` names it (for the
    app's progress), ``shown`` is how it reads, ``run`` does it. A
    ``command`` step shows its argv; the others read "would ..." in a dry
    run."""

    id: str
    shown: str
    run: Callable[[], object]  # the result is ignored
    command: bool = False

    def render(self, dry_run: bool) -> str:
        if self.command:
            return f"$ {self.shown}"
        return f"would {self.shown}" if dry_run else self.shown


def install_steps(
    plan: InstallPlan,
    previous: Manifest | None,
    *,
    runner: Runner = run_streaming,
    query: Runner = run_command,
    skip_models: bool = False,
    report: bool = False,
    say: Say = print,
) -> list[Step]:
    """Everything ``voiced install`` does, in order. Nothing that the
    running daemons use changes before the models are in: a failed install
    leaves the previous one as it was (ADR 0017). ``report``: the model
    download reports its bytes (``--json-progress``)."""
    steps: list[Step] = []
    found: dict[str, str] = {}

    def run(argv: Sequence[str], what: str) -> Callable[[], None]:
        def go() -> None:
            result = runner(argv)
            if not result.ok:
                output = f": {result.output}" if result.output else ""
                raise InstallError(f"{what} failed (exit {result.returncode}){output}")

        return go

    config = plan.config_file
    if not config.exists():
        steps.append(
            Step(
                "config",
                f"create {config} from config.example.toml",
                lambda: write_default_config(config),
            )
        )
    else:
        # Installs before 0600 became the default left it readable by every
        # local account; it is where api_key goes.
        mode = stat.S_IMODE(config.stat().st_mode)
        if mode & 0o077:
            steps.append(
                Step(
                    "config",
                    f"tighten {config} from {mode:o} to 600",
                    lambda: os.chmod(config, mode & 0o700 | 0o600),
                )
            )

    def claim_dir() -> None:
        plan.log_dir.mkdir(parents=True, exist_ok=True)
        # Claimed before building into it, so an interrupted install is
        # still recognised as voiced's.
        if previous is None:
            write_manifest(plan.state_dir, Manifest(source=plan.origin, environment=None))

    claimed = "" if previous is not None else f", and claim {plan.state_dir} ({MANIFEST})"
    steps.append(Step("claim", f"create the configured log dir {plan.log_dir}{claimed}", claim_dir))
    for argv in plan.build_commands():
        what = "uv venv" if "venv" in argv else "uv pip install"
        steps.append(Step("build", shlex.join(argv), run(argv, what), command=True))

    target = plan.environment / "bin" / "voiced"

    def check() -> None:
        if not target.exists():
            raise InstallError(f"{target} missing")
        result = query(plan.check_command())
        if not result.ok:
            raise InstallError(
                f"{plan.environment} does not have the engines (voiced's stt and tts "
                "extras). Install voiced with them, or build an environment from a "
                "checkout: voiced install --source <checkout>"
            )
        found["version"] = result.stdout.strip().removeprefix("voiced ").strip()

    steps.append(
        Step("check", f"{shlex.join(plan.check_command()[:3])} <engine check>", check, command=True)
    )

    def ports() -> None:
        conflicts, notes = foreign_listeners(plan.ports, plan.bind, plan.entry, query)
        for note in notes:
            say(note)
        if conflicts:
            raise InstallError(
                f"{'; '.join(conflicts)}. Stop it, or set another port in {plan.config_file}"
            )

    numbers = ", ".join(str(port) for port in plan.ports.values())
    steps.append(Step("ports", f"check that ports {numbers} are free", ports))
    if skip_models:
        say("skipping model download (--skip-models)")
    else:
        download = plan.model_download_command(report)
        steps.append(
            Step("models", shlex.join(download), run(download, "model download"), command=True)
        )

    # The switch: everything above can fail without touching what runs now.
    in_place = same_path(plan.environment, plan.state_dir)  # a legacy venv, adopted
    old = previous.environment if previous is not None else None
    unchanged = old is not None and same_path(old, plan.environment)
    if not in_place:
        steps.append(
            Step(
                "link",
                f"link {plan.entry} -> {target}",
                lambda: link_entry_point(plan.state_dir, plan.environment),
            )
        )
    # Re-running install from the active environment keeps who made it (an
    # app environment stays the app's).
    source = previous.source if unchanged and previous is not None else plan.origin

    def manifest() -> None:
        write_manifest(
            plan.state_dir,
            Manifest(
                source=source,
                environment=plan.environment,
                version=found.get("version", ""),
                lock_sha256=sha256_file(plan.lock) if plan.lock.is_file() else "",
                python=os.path.realpath(plan.python_bin),
            ),
        )

    steps.append(Step("manifest", f"write {plan.state_dir / MANIFEST}", manifest))
    if old is not None and same_path(old, plan.state_dir) and not in_place:
        # Its bin/voiced is the entry point now, so it cannot be kept to go
        # back to: the switch away from a legacy venv has no rollback.
        steps.append(
            Step(
                "legacy",
                f"remove the old environment in {plan.state_dir} (a venv from before ADR 0017)",
                lambda: remove_legacy_venv(plan.state_dir, say),
            )
        )
    if not unchanged:
        steps.append(
            Step(
                "prune",
                f"remove environments in {plan.state_dir / ENVS} other than "
                "the new and the previous one",
                lambda: prune_environments(plan.state_dir, [plan.environment, old], say),
            )
        )
    if any(path.exists() or path.is_symlink() for path in plan.legacy_staged_files()):
        steps.append(
            Step(
                "unstage",
                f"remove the plists and sudo scripts an earlier release staged in "
                f"{plan.legacy_staging_dir}",
                lambda: remove_legacy_staging(plan, say),
            )
        )
    return steps


def run_install(
    plan: InstallPlan,
    *,
    runner: Runner = run_streaming,
    query: Runner = run_command,
    dry_run: bool = False,
    skip_models: bool = False,
    say: Say = print,
    progress: Progress | None = None,
) -> None:
    """Run (or with ``dry_run`` only show) :func:`install_steps`, reporting
    each step to ``progress``.

    Long steps (build, model download) run through ``runner``, which by
    default streams output and has no timeout; the engine check through
    ``query``, which captures it. The whole install holds
    :func:`voiced.layout.install_lock`. Raises :class:`InstallError`.
    """
    progress = progress or Progress()
    if plan.source is not None and plan.uv is None:
        raise InstallError("--source builds with uv, which is not on PATH: brew install uv")
    previous = current_manifest(plan)  # refuses before anything is written
    if dry_run:
        for step in install_steps(
            plan,
            previous,
            runner=runner,
            query=query,
            skip_models=skip_models,
            report=progress.streaming,
            say=say,
        ):
            say(step.render(dry_run=True))
            progress.step(step.id, "planned", step.shown)
        return
    plan.state_dir.mkdir(parents=True, exist_ok=True)
    with install_lock(
        plan.state_dir, lambda: say(f"waiting for another install in {plan.state_dir}")
    ):
        previous = current_manifest(plan)  # what the other install left
        steps = install_steps(
            plan,
            previous,
            runner=runner,
            query=query,
            skip_models=skip_models,
            report=progress.streaming,
            say=say,
        )
        for step in steps:
            say(step.render(dry_run=False))
            progress.step(step.id, "start", step.shown)
            step.run()
            progress.step(step.id, "done", step.shown)
    old = previous.environment if previous is not None else None
    if old is not None and not same_path(old, plan.environment):
        say(
            f"The environment changed from {old}: running daemons switch to the new one "
            "within one watchdog interval, or now with `voiced restart all`."
        )


# Uninstall ------------------------------------------------------------------


def removal_refusal(path: Path, home: Path) -> str | None:
    """Why ``path`` must not be deleted recursively, or None if it is fine.

    Refuses the filesystem root, the home directory and every ancestor of it,
    and symlinks (so a link into the wrong place cannot be followed). The
    caller checks existence first.
    """
    if path.is_symlink():
        return "is a symlink"
    real = path.resolve()
    real_home = home.resolve()
    if real == Path(real.anchor):
        return "is the filesystem root"
    if real == real_home:
        return "is the home directory"
    if real in real_home.parents:
        return "contains the home directory"
    return None


def hf_cache_dirs(settings: Settings, hf_home: Path | None) -> list[Path]:
    """Hugging Face cache directories for the configured models."""
    hub = (hf_home or Path.home() / ".cache" / "huggingface") / "hub"
    models = (settings.stt.model, settings.tts.model)
    return [hub / f"models--{m.replace('/', '--')}" for m in models]


@dataclass(frozen=True)
class UninstallTargets:
    """Directories deleted recursively, and individual files; never a shared parent."""

    trees: list[Path]
    files: list[Path]
    prune_dirs: list[Path]  # removed afterwards, in order, only if empty
    # Why the state directory is left alone (it is not voiced's), or None.
    refused: str | None = None
    manifest: Manifest | None = None

    def all(self) -> list[Path]:
        return [*self.trees, *self.files]


def state_targets(state_dir: Path) -> UninstallTargets:
    """What of the state directory voiced owns (:func:`voiced.layout.claim`,
    the same rule install applies): the layout's entries, the environments
    it built or began to build, and a legacy venv's files. Nothing else in it, and nothing
    outside it (Homebrew's keg is Homebrew's)."""
    try:
        manifest = claim(state_dir)
    except SymlinkedStateDir as exc:
        return UninstallTargets([], [], [], refused=str(exc))
    except NotVoiceds as exc:
        return UninstallTargets([], [], [], refused=f"not voiced's: {exc}")
    except ManifestError as exc:
        return UninstallTargets([], [], [], refused=str(exc))
    entry = entry_point(state_dir)
    files = [entry, state_dir / "control.sock", state_dir / MANIFEST]
    if manifest is None:  # nothing installed; at most a stray socket
        return UninstallTargets([], files, [state_dir])
    # Unfinished builds too: uninstall holds the lock the builder holds.
    trees = [*built_environments(state_dir), *partial_environments(state_dir)]
    trees.append(state_dir / PYTHONS)
    if is_legacy_venv(state_dir):
        legacy = legacy_venv_paths(state_dir)
        trees += [p for p in legacy if p.is_dir() and not p.is_symlink()]
        files += [p for p in legacy if not (p.is_dir() and not p.is_symlink())]
    prune = [entry.parent, state_dir / ENVS, state_dir]
    return UninstallTargets(trees, files, prune, manifest=manifest)


def uninstall_targets(
    plan: InstallPlan, settings: Settings, *, logs: bool, models: bool, config: bool
) -> UninstallTargets:
    """What ``uninstall`` deletes.

    The log dir and the config dir may be shared with other things
    (``log_dir = "~/Library/Logs"`` is a legal config), so only the files
    voiced wrote are removed there, and the directory itself only if that
    leaves it empty. The state directory likewise (:func:`state_targets`).
    """
    state = state_targets(plan.state_dir)
    trees = list(state.trees)
    files = list(state.files)
    prune = list(state.prune_dirs)
    if logs:
        for name in (*SERVICES, "metalprobe"):
            paths = log_paths(plan.log_dir, name)
            files += [paths.app, paths.stdout, paths.stderr]
            files += sorted(plan.log_dir.glob(f"{name}.log.*"))  # rotated app logs
        files.append(plan.log_dir / METAL_PROBE_FILE)
        prune.append(plan.log_dir)
    if models:
        trees += hf_cache_dirs(settings, plan.hf_home)
    if config:
        files += [plan.config_file, plan.config_file.parent / "metal-probe.sh"]
        files += plan.legacy_staged_files()
        prune.append(plan.legacy_staging_dir)
    return UninstallTargets(trees, files, prune, refused=state.refused, manifest=state.manifest)


def remove_targets(targets: UninstallTargets, plan: InstallPlan, say: Say) -> int:
    """Delete the targets with the safety rules; returns how many were removed."""
    removed = 0
    if targets.refused is not None:
        say(f"refusing to touch {plan.state_dir}: {targets.refused}")
    for tree in targets.trees:
        if not tree.exists() and not tree.is_symlink():
            continue
        reason = removal_refusal(tree, plan.home)
        if reason is not None:
            say(f"refusing to remove {tree}: {reason}")
            continue
        shutil.rmtree(tree)
        say(f"removed {tree}")
        removed += 1
    for file in targets.files:
        if file.is_symlink() or file.is_file() or file.is_socket():
            file.unlink()
            say(f"removed {file}")
            removed += 1
    for directory in targets.prune_dirs:
        if not directory.is_dir() or removal_refusal(directory, plan.home) is not None:
            continue
        left = sorted(p.name for p in directory.iterdir())
        if not left:
            directory.rmdir()
            say(f"removed empty {directory}")
        elif directory == plan.state_dir and targets.refused is None:
            say(f"kept {directory}: it also holds {', '.join(left)}")
    return removed
