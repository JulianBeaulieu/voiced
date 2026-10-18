"""Installer: plan, render and execute ``voiced install`` / ``uninstall``.

The plan is a plain value (:class:`InstallPlan`) so ``--dry-run`` and the tests
can render every artifact without touching the machine. Steps that need root
(copying plists into ``/Library/LaunchDaemons`` and bootstrapping them) are
never executed here; they are rendered as a shell script for the user to
review and run with ``sudo``. See ADR 0006 for the layout, and ADR 0017 for
the state directory (``voiced.layout``).
"""

from __future__ import annotations

import json
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
from voiced.config import SERVICES, DaemonName, Settings, config_path
from voiced.engines.kokoro import ENV_ESPEAK_DATA, ENV_ESPEAK_LIBRARY
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
from voiced.layout import (
    ENVS,
    MANIFEST,
    PYTHONS,
    Manifest,
    ManifestError,
    NotVoiceds,
    Origin,
    built_environments,
    claim,
    entry_point,
    install_lock,
    is_legacy_venv,
    legacy_venv_paths,
    link_entry_point,
    same_path,
    sha256_file,
    stable_environment,
    write_manifest,
)

Service = DaemonName
METAL_PROBE_FILE = "metal-probe.json"

# Label prefixes used by earlier releases. ``uninstall`` boots these out too
# (ADR 0003). Empty until the prefix changes for the first time.
LEGACY_LABEL_PREFIXES: tuple[str, ...] = ()

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
    staging_dir: Path
    labels: dict[str, str]  # every DaemonName including "metalprobe"
    environment: Path
    origin: Origin
    source: Path | None = None
    # How the user chose state_dir, for messages.
    state_dir_flag: str = "[install] venv_dir"
    python: str = ""
    uv: str | None = None
    hf_home: Path | None = None
    # VOICED_ESPEAK_LIBRARY / VOICED_ESPEAK_DATA from the installing shell, so the
    # daemons load the same espeak-ng that `voiced doctor` found.
    espeak_env: dict[str, str] = field(default_factory=dict)

    # Paths -------------------------------------------------------------

    @property
    def entry(self) -> Path:
        return entry_point(self.state_dir)

    @property
    def python_bin(self) -> Path:
        """The new environment's interpreter."""
        return self.environment / "bin" / "python"

    @property
    def lock(self) -> Path:
        return self.source / LOCK if self.source is not None else packaged_lock()

    def staged_plist(self, service: str) -> Path:
        return self.staging_dir / f"{self.labels[service]}.plist"

    @property
    def install_script_path(self) -> Path:
        return self.staging_dir / "install-daemons.sh"

    @property
    def uninstall_script_path(self) -> Path:
        return self.staging_dir / "uninstall-daemons.sh"

    # Plists ------------------------------------------------------------

    def environment_variables(self) -> dict[str, str]:
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.entry.parent}:{BASE_PATH}",
            "VOICED_CONFIG": str(self.config_file),
            "PYTHONUNBUFFERED": "1",
        }
        if self.hf_home is not None:
            env["HF_HOME"] = str(self.hf_home)
        env.update(self.espeak_env)
        return env

    def program_arguments(self, service: str) -> list[str]:
        if service == "watchdog":
            return [str(self.entry), "watchdog"]
        return [str(self.entry), "serve", service]

    def render(self, service: str) -> bytes:
        # KeepAlive while the entry point resolves: after `brew uninstall
        # voiced` (or any deleted environment) launchd stops respawning a
        # program that cannot start, and resumes once it is back.
        return render_plist(
            label=self.labels[service],
            program_arguments=self.program_arguments(service),
            log_dir=self.log_dir,
            user_name=self.user,
            working_directory=self.home,
            environment=self.environment_variables(),
            keep_alive={"PathState": {str(self.entry): True}},
        )

    def plists(self) -> dict[str, bytes]:
        return {service: self.render(service) for service in SERVICES}

    # Shell commands ----------------------------------------------------

    def build_commands(self) -> list[list[str]]:
        """With ``source``: create the environment and install the locked
        dependencies, then voiced itself, with hashes throughout. Empty
        otherwise: the environment exists already."""
        if self.source is None:
            return []
        uv = self.uv or "uv"
        python = str(self.python_bin)
        managed = f"UV_PYTHON_INSTALL_DIR={self.state_dir / PYTHONS}"
        version = self.python or DEFAULT_PYTHON_VERSION
        install = [uv, "pip", "install", "--python", python, "--no-deps"]
        return [
            [
                "/usr/bin/env",
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

    def model_download_command(self) -> list[str]:
        """The environment goes in as a JSON argument, not through
        ``/usr/bin/env``, which would take a state path containing ``=``
        for one more assignment."""
        env = {"VOICED_CONFIG": str(self.config_file), **self.espeak_env}
        if self.hf_home is not None:
            env["HF_HOME"] = str(self.hf_home)
        return [str(self.python_bin), "-c", MODEL_DOWNLOAD_SCRIPT, json.dumps(env)]

    def sudo_install_script(self) -> str:
        lines = [
            "#!/bin/bash",
            "# voiced: install the LaunchDaemons. Review, then run with sudo.",
            "# Generated by `voiced install`; safe to re-run after a config change.",
            "set -euo pipefail",
            "",
        ]
        for service in SERVICES:
            lines.append(f"# {service}")
            lines += daemon_install_lines(self.labels[service], self.staged_plist(service))
            lines.append("")
        lines += [
            "echo 'voiced daemons installed. Check with:'",
            f"echo {q(f'  {self.entry} status')}",
            "",
        ]
        return "\n".join(lines)

    def sudo_uninstall_script(self) -> str:
        labels = list(self.labels.values())
        for prefix in LEGACY_LABEL_PREFIXES:
            labels += [f"{prefix}.{service}" for service in SERVICES]
        lines = [
            "#!/bin/bash",
            "# voiced: stop and remove the LaunchDaemons. Review, then run with sudo.",
            "set -uo pipefail",
            "",
        ]
        for label in labels:
            lines += [
                f"{shlex.join(bootout_argv(label))} 2>/dev/null || true",
                f"rm -f {q(plist_path(label))}",
            ]
        lines += ["", "echo 'voiced daemons removed.'", ""]
        return "\n".join(lines)

    # One-shot Metal probe -----------------------------------------------

    def metal_probe(self, python: Path) -> MetalProbe:
        """A self-removing one-shot daemon that runs ``voiced.launchd.metal_probe``."""
        label = self.labels["metalprobe"]
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
        staged = self.staging_dir / f"{label}.plist"
        # Under `set -e` a failing bootout inside the trap would abort before rm.
        cleanup = (
            f"{shlex.join(bootout_argv(label))} 2>/dev/null || true; rm -f {q(plist_path(label))}"
        )
        stderr_log = log_paths(self.log_dir, "metalprobe").stderr
        no_result = f"no result after 60 s; see {stderr_log}"
        lines = [
            "#!/bin/bash",
            "# voiced: one-shot LaunchDaemon that checks MLX/Metal without a GUI session.",
            "# Installs the job, waits for its result, then removes it again (also on error).",
            "set -euo pipefail",
            f"LABEL={q(label)}",
            f"DST={q(plist_path(label))}",
            f"OUT={q(out)}",
            # The trap guarantees no RunAtLoad plist is left behind.
            f"trap {q(cleanup)} EXIT",
            'rm -f "$OUT"',
            *daemon_install_lines(label, staged),
            'for _ in $(seq 1 60); do [ -f "$OUT" ] && break; sleep 1; done',
            'if [ -f "$OUT" ]; then cat "$OUT"; else',
            f"  echo {q(no_result)}; exit 1",
            "fi",
            "",
        ]
        return MetalProbe(
            label=label,
            plist_path=staged,
            script_path=self.staging_dir / "metal-probe.sh",
            plist=plist,
            script="\n".join(lines),
            result_file=out,
        )


@dataclass(frozen=True)
class MetalProbe:
    label: str
    plist_path: Path
    script_path: Path
    plist: bytes
    script: str
    result_file: Path

    def write(self) -> None:
        self.plist_path.parent.mkdir(parents=True, exist_ok=True)
        self.plist_path.write_bytes(self.plist)
        self.script_path.write_text(self.script)


def daemon_install_lines(label: str, staged: Path) -> list[str]:
    """Shell lines that (re)install one plist and bootstrap it.

    ``bootout`` is asynchronous; bootstrapping while the old instance is still
    being torn down fails with error 5, so wait until ``launchctl print`` no
    longer finds the service.
    """
    dst = plist_path(label)
    probe = shlex.join(print_argv(label))
    return [
        f"{shlex.join(bootout_argv(label))} 2>/dev/null || true",
        f"for _ in $(seq 1 30); do {probe} >/dev/null 2>&1 || break; sleep 1; done",
        f"cp {q(staged)} {q(dst)}",
        f"chown root:wheel {q(dst)}",
        f"chmod 644 {q(dst)}",
        shlex.join(bootstrap_argv(label)),
    ]


ENGINE_CHECK = (
    "import mlx.core, parakeet_mlx, mlx_audio, voiced; print('voiced', voiced.__version__)"
)

# Runs inside the venv's interpreter, which has the engine extras installed.
# Loads each configured engine once so the weights land in the Hugging Face
# cache; falls back to a plain snapshot download when the engine plugin is not
# installed (for example a dev install without the MLX extras).
MODEL_DOWNLOAD_SCRIPT = """
import asyncio, json, os, sys
os.environ.update(json.loads(sys.argv[1]))
from voiced.config import load_settings
from voiced.engines.registry import UnknownEngine, get_stt_engine, get_tts_engine

settings = load_settings()
jobs = (
    ("stt", settings.stt.engine, settings.stt.model, get_stt_engine),
    ("tts", settings.tts.engine, settings.tts.model, get_tts_engine),
)

def snapshot(model):
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print(f"  huggingface_hub not installed; skipping download of {model}")
        return
    snapshot_download(model)

for service, engine_name, model, getter in jobs:
    print(f"{service}: engine={engine_name} model={model}", flush=True)
    try:
        cls = getter(engine_name)
    except UnknownEngine as exc:
        print(f"  {exc}; downloading weights only")
        snapshot(model)
        continue
    engine = cls(model)
    asyncio.run(engine.load())
    print(f"  loaded {engine.name} ({engine.model_id})", flush=True)
"""


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
    staging_dir: Path | None = None,
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
    env_hf_home = os.environ.get("HF_HOME")
    hf_home = settings.install.hf_home or (Path(env_hf_home) if env_hf_home else None)
    if hf_home is not None:
        hf_home = hf_home.expanduser().absolute()  # the daemon has a different cwd
    espeak_env = {
        key: str(Path(value).expanduser().absolute())
        for key in (ENV_ESPEAK_LIBRARY, ENV_ESPEAK_DATA)
        if (value := os.environ.get(key))
    }
    return InstallPlan(
        user=installing_user(),
        home=home or Path.home(),
        state_dir=state,
        config_file=config_file,
        log_dir=settings.server.log_dir,
        staging_dir=(staging_dir or config_file.parent / "launchd").expanduser(),
        labels={name: settings.launchd.label(name) for name in (*SERVICES, "metalprobe")},
        environment=environment,
        origin=origin or found,
        source=source,
        state_dir_flag="[install] venv_dir" if state_dir is None else "--state-dir",
        python=python if python is not None else settings.install.python,
        uv=find_uv() if uv == "auto" else uv,
        hf_home=hf_home,
        espeak_env=espeak_env,
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
        f"staging        {plan.staging_dir}",
        f"hf_home        {plan.hf_home or '(Hugging Face default)'}",
    ]
    lines += [f"{key:<14} {value}" for key, value in plan.espeak_env.items()]
    for service in SERVICES:
        lines.append(f"label {service:<9}{plan.labels[service]}")
    return "\n".join(lines)


def write_staging(plan: InstallPlan) -> list[Path]:
    """Write the plists and both sudo scripts to the staging directory."""
    plan.staging_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for service, data in plan.plists().items():
        path = plan.staged_plist(service)
        path.write_bytes(data)
        written.append(path)
    plan.install_script_path.write_text(plan.sudo_install_script())
    plan.uninstall_script_path.write_text(plan.sudo_uninstall_script())
    written += [plan.install_script_path, plan.uninstall_script_path]
    return written


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
    say: Say = print,
) -> list[Step]:
    """Everything ``voiced install`` does, in order. Nothing that the
    running daemons use changes before the models are in: a failed install
    leaves the previous one as it was (ADR 0017)."""
    steps: list[Step] = []
    found: dict[str, str] = {}

    def run(argv: Sequence[str], what: str) -> Callable[[], None]:
        def go() -> None:
            result = runner(argv)
            if not result.ok:
                raise InstallError(
                    f"{what} failed (exit {result.returncode}): {result.output}".rstrip()
                )

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
    steps.append(Step("claim", f"create log dir {plan.log_dir}{claimed}", claim_dir))
    for argv in plan.build_commands():
        what = "uv venv" if "venv" in argv else argv[0]
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
    if skip_models:
        say("skipping model download (--skip-models)")
    else:
        steps.append(
            Step(
                "models",
                f"{plan.python_bin} -c <model download>",
                run(plan.model_download_command(), "model download"),
                command=True,
            )
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
    steps.append(
        Step(
            "stage",
            f"write the plists and scripts to {plan.staging_dir}",
            lambda: write_staging(plan),
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
) -> None:
    """Run (or with ``dry_run`` only show) :func:`install_steps`.

    Long steps (build, model download) run through ``runner``, which by
    default streams output and has no timeout; the engine check through
    ``query``, which captures it. The whole install holds
    :func:`voiced.layout.install_lock`. Raises :class:`InstallError`.
    """
    if plan.source is not None and plan.uv is None:
        raise InstallError("--source builds with uv, which is not on PATH: brew install uv")
    previous = current_manifest(plan)  # refuses before anything is written
    if dry_run:
        for step in install_steps(
            plan, previous, runner=runner, query=query, skip_models=skip_models, say=say
        ):
            say(step.render(dry_run=True))
        return
    plan.state_dir.mkdir(parents=True, exist_ok=True)
    with install_lock(
        plan.state_dir, lambda: say(f"waiting for another install in {plan.state_dir}")
    ):
        previous = current_manifest(plan)  # what the other install left
        steps = install_steps(
            plan, previous, runner=runner, query=query, skip_models=skip_models, say=say
        )
        for step in steps:
            say(step.render(dry_run=False))
            step.run()
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
    it built, and a legacy venv's files. Nothing else in it, and nothing
    outside it (Homebrew's keg is Homebrew's)."""
    try:
        manifest = claim(state_dir)
    except NotVoiceds as exc:
        return UninstallTargets([], [], [], refused=f"not voiced's: {exc}")
    except ManifestError as exc:
        return UninstallTargets([], [], [], refused=str(exc))
    entry = entry_point(state_dir)
    files = [entry, state_dir / "control.sock", state_dir / MANIFEST]
    # Without a manifest, claim has checked every environment and the Python
    # directory are voiced's (what the app builds before `voiced install`).
    trees = [*built_environments(state_dir), state_dir / PYTHONS]
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

    The log dir and the staging dir may be shared with other things
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
        files.append(plan.config_file)
        files += [plan.staged_plist(name) for name in plan.labels]
        files += [
            plan.install_script_path,
            plan.uninstall_script_path,
            plan.staging_dir / "metal-probe.sh",
        ]
        prune.append(plan.staging_dir)
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
