"""Installer: plan, render and execute ``voiced install`` / ``uninstall``.

The plan is a plain value (:class:`InstallPlan`) so ``--dry-run`` and the tests
can show every step without touching the machine. The last step writes the
three LaunchAgent plists into ``~/Library/LaunchAgents`` and loads them into
the user's domain; nothing needs root (ADR 0035). See ADR 0006 for the
layout, and ADR 0017 for the state directory (``voiced.layout``).
"""

from __future__ import annotations

import contextlib
import os
import pwd
import shlex
import shutil
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import voiced
from voiced.config import (
    ENV_HF_HOME,
    HTTP_SERVICES,
    SERVICES,
    DaemonName,
    ServiceName,
    Settings,
    config_path,
)
from voiced.launchd.control import (
    LegacyDaemon,
    Runner,
    bootout_argv,
    bootstrap_argv,
    has_session,
    legacy_daemons,
    legacy_message,
    no_session_message,
    plist_path,
    print_service,
    run_command,
    run_streaming,
    wait_unloaded,
)
from voiced.launchd.parked import off_detail
from voiced.launchd.plist import log_paths, render_plist
from voiced.launchd.ports import FIREWALL_NOTE, firewall_enabled, foreign_listeners
from voiced.launchd.progress import Progress
from voiced.layout import (
    ENVS,
    MANIFEST,
    OFF_SERVICES,
    PARTIAL_SUFFIX,
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
    off_marker,
    partial_environments,
    read_manifest,
    same_path,
    sha256_file,
    stable_environment,
    write_manifest,
)
from voiced.models import (
    CachedRevision,
    cached_revisions,
    delete_revisions,
    hub_dir,
    pinned_revisions,
    repo_folder,
    size_text,
)
from voiced.settings_store import bind_mode

Service = DaemonName
METAL_PROBE_FILE = "metal-probe.json"

# What releases before ADR 0018 staged in <config dir>/launchd for a sudo
# script to copy; `install` removes them, so no stale script is left.
LEGACY_STAGING = "launchd"
LEGACY_SCRIPTS = ("install-daemons.sh", "uninstall-daemons.sh", "metal-probe.sh")

# Homebrew (both architectures) first so a brewed ffmpeg is found, then
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
    ``LOGNAME="System Administrator"`` it would name uid 0 as the services'
    ``UserName``."""
    return pwd.getpwuid(os.getuid()).pw_name


def find_uv() -> str | None:
    return shutil.which("uv")


def q(path: Path | str) -> str:
    """Shell-quote a path for a printed command."""
    return shlex.quote(str(path))


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
    # Services switched off in the config: their services start and park.
    off: tuple[ServiceName, ...] = ()
    # Settings to write to the config once the models are in (``--set``,
    # the app's first-run choices), as ``voiced settings set`` takes them.
    # The plan and the models step already use them.
    changes: Mapping[str, object] = field(default_factory=dict)

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

    # The LaunchAgents (ADR 0035) ----------------------------------------

    def agent_path(self, service: DaemonName) -> Path:
        return plist_path(self.labels[service], self.home)

    def agent_plist(self, service: DaemonName) -> bytes:
        """What ``~/Library/LaunchAgents/<label>.plist`` holds for ``service``:
        the entry point with ``watchdog`` or ``serve <service>``, kept alive
        while the entry point exists (ADR 0017)."""
        command = ["watchdog"] if service == "watchdog" else ["serve", service]
        return render_plist(
            label=self.labels[service],
            program_arguments=[str(self.entry), *command],
            log_dir=self.log_dir,
            working_directory=self.home,
            environment=self.environment_variables(),
            keep_alive={"PathState": {str(self.entry): True}},
        )

    def legacy_daemons(self, query: Runner) -> list[LegacyDaemon]:
        """The LaunchDaemons of 0.4.0 or earlier: the default labels, which the
        old root step installed, and this plan's."""
        return legacy_daemons([self.labels[s] for s in SERVICES], query)

    def legacy_refusal(self, query: Runner) -> str | None:
        """Why the agents cannot be installed yet, or None: LaunchDaemons from
        0.4.0 or earlier still hold the labels and the ports, and only root
        can remove them. The install stops before it changes anything, with
        the one line that does it, so both never run at once."""
        found = self.legacy_daemons(query)
        return legacy_message(found, self.user) if found else None

    def session_refusal(self, query: Runner) -> str | None:
        """Why the agents cannot be loaded, or None: an agent loads only into
        the GUI domain of a user logged in at the Mac, which an SSH login
        alone does not create."""
        return None if has_session(query) else no_session_message(self.user)

    def environment_variables(self) -> dict[str, str]:
        """The environment in the agents' plists, and the Metal probe's: the
        services read everything else from ``VOICED_CONFIG`` (ADR 0018)."""
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
        for key, value in self.changes.items():  # not written to the config yet
            argv += ["--set", f"{key}={str(value).lower() if isinstance(value, bool) else value}"]
        return [*argv, "--report"] if report else argv


ENGINE_CHECK = (
    "import mlx.core, parakeet_mlx, mlx_audio, voiced; print('voiced', voiced.__version__)"
)


def environment_taken(candidate: Path, manifest: Manifest | None) -> bool:
    """Whether a new environment must not be built at ``candidate``: it, its
    ``.partial`` or its ``.broken`` (the app's, ADR 0018) is there, or
    ``install.json`` names it, as the active or the previous environment,
    even if it is gone (ADR 0017). The app keeps the same rule in Swift
    (``InstallStages.environmentTaken`` / ``newEnvironmentDirectory``):
    change both together."""
    if any(
        os.path.lexists(candidate.with_name(candidate.name + suffix))
        for suffix in ("", PARTIAL_SUFFIX, ".broken")
    ):
        return True
    named = [] if manifest is None else [manifest.environment, manifest.previous]
    return any(same_path(candidate, p) for p in named if p is not None)


def new_environment_dir(
    state_dir: Path, stamp: str | None = None, manifest: Manifest | None = None
) -> Path:
    """A fresh ``envs/<id>``: a build never writes into an environment a
    service may be running, and ``install.json`` never names a path that once
    was another environment. Two installs within a second get ``<id>-2``."""
    base = state_dir / ENVS / (stamp or time.strftime("%Y%m%dT%H%M%S", time.gmtime()))
    candidate, n = base, 1
    while environment_taken(candidate, manifest):
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
    changes: Mapping[str, object] | None = None,
) -> InstallPlan:
    """Resolve CLI flags, config and defaults into an :class:`InstallPlan`.

    Without ``source`` the environment is the one this command runs from
    (``prefix``, default ``sys.prefix``); with it, a new one under
    ``<state_dir>/envs``. ``origin`` is who made the environment, for the
    manifest: by default ``homebrew`` for a keg and ``cli`` otherwise; the
    app passes ``app``. ``changes`` (``--set``) are staged onto
    ``settings`` (:func:`stage_settings`): the plan and the models step use
    them as staged, an engine switch completed with its voice, and the
    install writes them to the config at the switch. They are checked
    against this voiced's engines, the new environment's for the app; a
    ``source`` build's are checked at the switch, by its own Python. A
    service that is off has its ports left out.
    """
    config_file = config_file or config_path()
    if changes:
        from voiced.engines.catalogue import engines_list
        from voiced.settings_store import ConfigFileError, InvalidSetting, stage_settings

        try:
            staged = stage_settings(
                changes, config_file, engines=engines_list if source is None else None
            )
        except (InvalidSetting, ConfigFileError) as exc:
            raise InstallError(f"--set {exc}") from None
        settings, changes = staged.onto(settings), staged.wanted
    state = (state_dir or settings.install.venv_dir).expanduser().absolute()
    if source is None:
        environment, found = stable_environment(sys.prefix if prefix is None else prefix)
    else:
        source = source.expanduser().absolute()
        try:
            manifest = read_manifest(state)
        except ManifestError:
            # Safe to ignore here: run_install reads it again before it
            # writes anything, and refuses it.
            manifest = None
        environment, found = new_environment_dir(state, manifest=manifest), "cli"
    off = tuple(s for s in HTTP_SERVICES if not settings.service(s).enabled)
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
        # A service that is off opens no port (ADR 0033).
        ports=settings.ports([s for s in HTTP_SERVICES if s not in off]),
        bind=settings.server.bind,
        off=off,
        changes=dict(changes or {}),
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


# The LaunchAgents ---------------------------------------------------------


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def install_agents(plan: InstallPlan, query: Runner, say: Say) -> None:
    """Write each service's plist and load it, where it changed or is not
    loaded. An agent that is up to date and loaded keeps running: the
    watchdog moves the services to a new environment itself (ADR 0017).
    A label that fails to load does not stop the others. Needs the user's
    GUI session (:meth:`InstallPlan.session_refusal`): :func:`run_install`
    checks it before anything else, and this again, for a logout since."""
    refusal = plan.session_refusal(query)
    if refusal is not None:
        raise InstallError(refusal)
    failed: list[str] = []
    for service in SERVICES:
        label, path, plist = (
            plan.labels[service],
            plan.agent_path(service),
            plan.agent_plist(service),
        )
        loaded = print_service(label, query).loaded
        if loaded and _read_bytes(path) == plist:
            say(f"{label}: up to date")
            continue
        if loaded:
            query(bootout_argv(label))
            if not wait_unloaded(label, query):
                failed.append(f"{label} (still unloading; run `voiced install` again)")
                continue
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_bytes(plist)
        tmp.chmod(0o644)  # launchd refuses a plist others may write
        tmp.replace(path)
        result = query(bootstrap_argv(path))
        if result.ok:
            say(f"{label}: loaded")
        else:
            failed.append(f"{label} ({result.output or f'exit {result.returncode}'})")
    if failed:
        raise InstallError(f"not loaded: {'; '.join(failed)}")


def remove_agents(plan: InstallPlan, query: Runner, say: Say) -> int:
    """Boot out the services and delete their plists (``voiced uninstall``);
    how many it stopped or deleted."""
    removed = 0
    for service in SERVICES:
        label, path = plan.labels[service], plan.agent_path(service)
        if print_service(label, query).loaded:
            query(bootout_argv(label))
            if not wait_unloaded(label, query):
                raise InstallError(f"{label} is still unloading; run `voiced uninstall` again")
            say(f"stopped {label}")
            removed += 1
        if path.is_file() or path.is_symlink():
            path.unlink()
            say(f"removed {path}")
            removed += 1
    return removed


# The Metal probe ----------------------------------------------------------

METAL_PROBE_WAIT_S = 60.0


def run_metal_probe(
    plan: InstallPlan,
    python: Path,
    query: Runner = run_command,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Run ``voiced.launchd.metal_probe`` once as a LaunchAgent, as the
    services run, and return the JSON it wrote (ADR 0035). Its plist is in
    a temporary directory and the job is booted out afterwards, also on
    error, so nothing of it is left to load at the next login."""
    label = plan.labels["metalprobe"]
    out = plan.log_dir / METAL_PROBE_FILE
    plist = render_plist(
        label=label,
        program_arguments=[str(python), "-m", "voiced.launchd.metal_probe", "--out", str(out)],
        log_dir=plan.log_dir,
        working_directory=plan.home,
        environment=plan.environment_variables(),
        keep_alive=False,
    )
    plan.log_dir.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)
    query(bootout_argv(label))
    wait_unloaded(label, query, sleep)
    with tempfile.TemporaryDirectory(prefix="voiced-probe-") as tmp:
        path = Path(tmp) / f"{label}.plist"
        path.write_bytes(plist)
        path.chmod(0o644)
        try:
            result = query(bootstrap_argv(path))
            if not result.ok:
                raise InstallError(f"launchctl bootstrap failed: {result.output}")
            deadline = time.monotonic() + METAL_PROBE_WAIT_S
            while not out.is_file():
                if time.monotonic() >= deadline:
                    stderr = log_paths(plan.log_dir, "metalprobe").stderr
                    raise InstallError(f"no result after {METAL_PROBE_WAIT_S:.0f} s; see {stderr}")
                sleep(0.5)
        finally:
            query(bootout_argv(label))
    return out.read_text()


def write_default_config(config_file: Path) -> bool:
    """Create the config file from the example if it does not exist.

    It is created 0600: it is where ``api_key`` goes, home directories on
    macOS are readable by the ``staff`` group every local user is in, and the
    services run as this user, so nobody else needs to read it.
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


def write_settings(plan: InstallPlan, say: Say = print) -> None:
    """Write ``plan.changes`` to the config at the switch, once the models
    step has downloaded what they name, as ``voiced settings set`` does:
    every check included, the engines those of the environment being
    installed."""
    from voiced.control.ops import engines_in_subprocess
    from voiced.settings_store import InvalidSetting, apply_settings

    def engines() -> dict[str, Any]:
        return engines_in_subprocess(entry=plan.environment / "bin" / "voiced")

    try:
        change = apply_settings(plan.changes, plan.config_file, engines=engines)
    except InvalidSetting as exc:
        raise InstallError(f"{exc}. Nothing was changed in {plan.config_file}") from None
    for service in change.restart_required:
        # Daemons already running read the config at start.
        say(f"{service}: its settings changed; a running service takes them on restart")


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
    the previous one: services not yet restarted still run the previous)."""
    kept = [p for p in keep if p is not None]
    for env in built_environments(state_dir):
        if any(same_path(env, k) for k in kept if k.exists()):
            continue
        shutil.rmtree(env)
        say(f"removed unused environment {env}")


def rollback_target(
    plan: InstallPlan, previous: Manifest | None, *, unchanged: bool
) -> Path | None:
    """What ``install.json`` records as ``previous``: the environment this
    install replaces, which prune keeps (ADR 0018's "Roll back")."""
    if previous is None or previous.environment is None:
        return None
    if unchanged:  # kept, unless it was deleted since
        kept = previous.previous
        return kept if kept is not None and kept.exists() else None
    old = previous.environment
    if same_path(old, plan.state_dir):
        return None  # a legacy venv, removed by this install: nothing to go back to
    if old.exists():
        return old
    # The active environment is gone (the app moved a broken one aside to
    # repair it): the one it replaced is still the way back, if it is there.
    kept = previous.previous
    return kept if kept is not None and kept.exists() else None


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
    prepare: bool = False,
    say: Say = print,
) -> list[Step]:
    """Everything ``voiced install`` does, in order. Nothing that the
    running services use changes before the models are in: a failed install
    leaves the previous one as it was (ADR 0017). ``report``: the model
    download reports its bytes (``--json-progress``). ``prepare``: only up
    to the models, nothing the running services use, and no ports step (old
    LaunchDaemons may hold them): the app's first half of a migration
    (ADR 0035), before the prompt that removes those daemons."""
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
        # Only the processes launchd runs for this install's agents are voiced's
        # own: an old LaunchDaemon on the port is not, nor a `voiced serve`.
        pids = (print_service(plan.labels[s], query).pid for s in SERVICES)
        own = {pid for pid in pids if pid is not None}
        conflicts, notes = foreign_listeners(plan.ports, plan.bind, plan.entry, query, own)
        for note in notes:
            say(note)
        if conflicts:
            raise InstallError(
                f"{'; '.join(conflicts)}. Stop it, or set another port in {plan.config_file}"
            )

    numbers = ", ".join(str(port) for port in plan.ports.values())
    if not prepare:
        steps.append(Step("ports", f"check that ports {numbers} are free", ports))
    if skip_models:
        say("skipping model download (--skip-models)")
    else:
        download = plan.model_download_command(report)
        steps.append(
            Step("models", shlex.join(download), run(download, "model download"), command=True)
        )

    if prepare:
        return steps
    # The switch: everything above can fail without touching what runs now.
    if plan.changes:
        shown = ", ".join(f"{key}={value}" for key, value in plan.changes.items())
        steps.append(
            Step("settings", f"set {shown} in {config}", lambda: write_settings(plan, say))
        )
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
    # app environment stays the app's), and what it replaced.
    source = previous.source if unchanged and previous is not None else plan.origin
    replaced = rollback_target(plan, previous, unchanged=unchanged)

    def manifest() -> None:
        write_manifest(
            plan.state_dir,
            Manifest(
                source=source,
                environment=plan.environment,
                version=found.get("version", ""),
                lock_sha256=sha256_file(plan.lock) if plan.lock.is_file() else "",
                python=os.path.realpath(plan.python_bin),
                previous=replaced,
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
                lambda: prune_environments(plan.state_dir, [plan.environment, old, replaced], say),
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
    steps.append(
        Step(
            "agents",
            f"write the LaunchAgents in {plan.agent_path('watchdog').parent} and load the ones "
            "that changed",
            lambda: install_agents(plan, query, say),
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
    prepare: bool = False,
    say: Say = print,
    progress: Progress | None = None,
) -> None:
    """Run (or with ``dry_run`` only show) :func:`install_steps`, reporting
    each step to ``progress``.

    Long steps (build, model download) run through ``runner``, which by
    default streams output and has no timeout; the engine check through
    ``query``, which captures it. The whole install holds
    :func:`voiced.layout.install_lock`. Raises :class:`InstallError`.

    The refusals come first, before anything is written: without a login
    session at the Mac, then while old LaunchDaemons are there. A dry run
    refuses as the install would. ``prepare`` checks neither: it loads no
    agent and leaves the daemons running (the app removes them next), and
    the install that completes it checks both, before it changes anything
    the services use.
    """
    progress = progress or Progress()
    refusal = None if prepare else plan.session_refusal(query) or plan.legacy_refusal(query)
    if refusal is not None:
        raise InstallError(f"Not installing yet; nothing was changed. {refusal}")
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
            prepare=prepare,
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
        if plan.source is not None and environment_taken(plan.environment, previous):
            raise InstallError(
                f"Another install built {plan.environment} while this one waited: "
                "run this one again."
            )
        steps = install_steps(
            plan,
            previous,
            runner=runner,
            query=query,
            skip_models=skip_models,
            report=progress.streaming,
            prepare=prepare,
            say=say,
        )
        for step in steps:
            say(step.render(dry_run=False))
            progress.step(step.id, "start", step.shown)
            step.run()
            progress.step(step.id, "done", step.shown)
    if prepare:
        say(f"{plan.environment} is ready; nothing that runs has changed.")
        return
    old = previous.environment if previous is not None else None
    if old is not None and not same_path(old, plan.environment):
        say(
            f"The environment changed from {old}: running services switch to the new one "
            "within one watchdog interval, or now with `voiced restart all`."
        )
    for service in plan.off:
        # The agent stays loaded; its process parks (voiced.launchd.parked,
        # ADR 0033).
        say(f"{service}: {off_detail(service)}. Its agent starts and serves nothing.")
    if bind_mode(plan.bind) == "lan" and firewall_enabled(query):
        say(f"Firewall: {FIREWALL_NOTE}")


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


@dataclass(frozen=True)
class ModelTargets:
    """What ``--remove-models`` deletes from the cache at ``hub``: the
    revisions voiced pinned (:func:`voiced.models.pinned_revisions`), never
    another revision (other tools share the cache), and the bytes that
    frees. ``unpinned``: repositories the config names that voiced does not
    pin, which are kept. ``broken``: voiced's repositories the cache scan
    could not read. ``refused``: why nothing can be deleted."""

    hub: Path
    revisions: list[CachedRevision] = field(default_factory=list)
    freed: int = 0
    unpinned: list[str] = field(default_factory=list)
    broken: list[str] = field(default_factory=list)
    refused: str | None = None

    def notes(self) -> list[str]:
        """What is left alone, and why; the dry run and the removal both say it."""
        if self.refused is not None:
            return [f"not removing models from {self.hub}: {self.refused}"]
        remove = f"(`hf cache rm --cache-dir {q(self.hub)} model/{{}}` removes it)"
        notes = [
            f"kept {repo}: voiced does not pin its revision, so it may be another tool's "
            + remove.format(repo)
            for repo in self.unpinned
        ]
        notes += [
            f"cannot remove {repo}: the cache scan could not read it; run `voiced install` "
            f"again to finish the download, or `rm -r {q(self.hub / repo_folder(repo))}`"
            for repo in self.broken
        ]
        return notes


def model_targets(plan: InstallPlan, settings: Settings) -> ModelTargets:
    """The revisions voiced pinned, this release's and earlier ones, that
    the cache holds."""
    hub = hub_dir(plan.hf_home)
    pins = pinned_revisions()
    pinned_repos = {repo for repo, _ in pins}
    try:
        scan = cached_revisions(hub, pinned_repos | {settings.stt.model, settings.tts.model})
        revisions = [c for c in scan.revisions if (c.repo, c.revision) in pins]
        freed = delete_revisions(hub, revisions, dry_run=True) if revisions else 0
    except ImportError:
        return ModelTargets(hub, refused="huggingface_hub is not installed here")
    return ModelTargets(
        hub,
        revisions=revisions,
        freed=freed,
        unpinned=sorted({c.repo for c in scan.revisions if c.repo not in pinned_repos}),
        broken=[repo for repo in scan.broken if repo in pinned_repos],
    )


def revision_line(revision: CachedRevision) -> str:
    """``repo revision sha``, and the branches pointing at it: another tool
    that downloaded ``main`` shares a pinned revision when ``main`` is it."""
    line = f"{revision.repo} revision {revision.revision}"
    if revision.refs:
        names = ", ".join(f"`{ref}`" for ref in revision.refs)
        line += f" (also {names}, which other tools may use)"
    return line


def remove_models(models: ModelTargets, say: Say) -> int:
    """Delete ``models.revisions``; returns how many revisions were removed."""
    for note in models.notes():
        say(note)
    if models.refused is not None or not models.revisions:
        return 0
    freed = delete_revisions(models.hub, models.revisions)
    for revision in models.revisions:
        say(f"removed {revision_line(revision)}")
    say(f"freed {size_text(freed)} ({freed:,} bytes) of models in {models.hub}")
    return len(models.revisions)


@dataclass(frozen=True)
class UninstallTargets:
    """Directories deleted recursively, and individual files; never a shared parent."""

    trees: list[Path]
    files: list[Path]
    prune_dirs: list[Path]  # removed afterwards, in order, only if empty
    # Why the state directory is left alone (it is not voiced's), or None.
    refused: str | None = None
    manifest: Manifest | None = None
    models: ModelTargets | None = None  # with --remove-models

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
    markers = [off_marker(state_dir, service) for service in OFF_SERVICES]
    files = [entry, state_dir / "control.sock", *markers, state_dir / MANIFEST]
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
    plan: InstallPlan,
    settings: Settings,
    *,
    logs: bool,
    models: bool,
    config: bool,
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
    found_models = model_targets(plan, settings) if models else None
    if config:
        files += [plan.config_file, plan.config_file.parent / "metal-probe.sh"]
        files += plan.legacy_staged_files()
        prune.append(plan.legacy_staging_dir)
    return UninstallTargets(
        trees,
        files,
        prune,
        refused=state.refused,
        manifest=state.manifest,
        models=found_models,
    )


def remove_targets(targets: UninstallTargets, plan: InstallPlan, say: Say) -> int:
    """Delete the targets with the safety rules; returns how many paths and
    model revisions were removed (0: there was nothing to remove)."""
    removed = 0
    if targets.refused is not None:
        say(f"refusing to touch {plan.state_dir}: {targets.refused}")
    if targets.models is not None:
        removed += remove_models(targets.models, say)
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
