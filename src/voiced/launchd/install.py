"""Installer: plan, render and execute ``voiced install`` / ``uninstall``.

The plan is a plain value (:class:`InstallPlan`) so ``--dry-run`` and the tests
can render every artifact without touching the machine. Steps that need root
(copying plists into ``/Library/LaunchDaemons`` and bootstrapping them) are
never executed here; they are rendered as a shell script for the user to
review and run with ``sudo``. See ADR 0006 for the layout.
"""

from __future__ import annotations

import getpass
import os
import re
import shlex
import shutil
import stat
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import voiced
from voiced.config import HTTP_SERVICES, SERVICES, DaemonName, Settings, config_path
from voiced.engines.kokoro import ENV_ESPEAK_DATA, ENV_ESPEAK_LIBRARY
from voiced.launchd.control import (
    Runner,
    bootout_argv,
    bootstrap_argv,
    plist_path,
    print_argv,
    run_streaming,
)
from voiced.launchd.plist import log_paths, render_plist

Service = DaemonName
EXTRAS: tuple[str, ...] = HTTP_SERVICES  # one package extra per inference service
METAL_PROBE_FILE = "metal-probe.json"

# Label prefixes used by earlier releases. ``uninstall`` boots these out too
# (ADR 0003). Empty until the prefix changes for the first time.
LEGACY_LABEL_PREFIXES: tuple[str, ...] = ()

# Homebrew (both architectures) first so espeak-ng and ffmpeg are found, then
# the system paths. The venv's bin directory is prepended per plan.
BASE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
DEFAULT_PYTHON_VERSION = "3.12"

Say = Callable[[str], None]

# A pip source that is a distribution name followed by a version specifier
# (``voiced==0.0.1``, ``voiced>=1``), as opposed to a path (``.``,
# ``/Users/me/voiced``) or a bare name (``voiced``).
_VERSIONED_SOURCE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)([<>=!~].*)$")


def requirement_with_extras(source: str, extras: Sequence[str]) -> str:
    """Add ``extras`` to a pip ``source`` as a valid PEP 508 requirement.

    Extras belong after the distribution name but before any version
    specifier: a pinned release becomes ``voiced[stt,tts]==0.0.1``, never
    ``voiced==0.0.1[stt,tts]`` (which is not valid PEP 508 and pip rejects, so
    a bare ``voiced install`` would fail after the first PyPI release). A path
    or bare name has no specifier and takes the extras at the end
    (``/path/to/voiced[stt,tts]``, ``.[stt,tts]``, ``voiced[stt,tts]``).

    Handles what ``default_source`` produces and the usual ``--source`` forms: a
    name with an unspaced specifier, a path, or a bare name. A source that
    already has extras (``name[x]==1``), a direct reference (``name @ url``) or
    a specifier with spaces is not supported.
    """
    if not extras:
        return source
    joined = ",".join(extras)
    match = _VERSIONED_SOURCE.match(source)
    if match:
        name, specifier = match.groups()
        return f"{name}[{joined}]{specifier}"
    return f"{source}[{joined}]"


def repo_root() -> Path | None:
    """The checkout this package was imported from, if it is a dev checkout."""
    candidate = Path(voiced.__file__).resolve().parents[2]
    pyproject = candidate / "pyproject.toml"
    if pyproject.is_file() and 'name = "voiced"' in pyproject.read_text(errors="replace"):
        return candidate
    return None


PUBLISHED = False
"""Whether ``voiced==<version>`` is on PyPI. Until it is, ``voiced install``
outside a checkout needs ``--source``: installing an unpublished name from
PyPI would install whatever someone else registers under it."""


def needs_source() -> bool:
    """True when ``install`` has nothing safe to install without ``--source``."""
    return not PUBLISHED and repo_root() is None


def default_source() -> str:
    """What to ``pip install``: the dev checkout if there is one, else the release."""
    root = repo_root()
    if root is not None:
        return str(root)
    return f"voiced=={voiced.__version__}"


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


def find_uv() -> str | None:
    return shutil.which("uv")


def is_root() -> bool:
    return os.geteuid() == 0


def q(path: Path | str) -> str:
    """Shell-quote a path for the generated scripts."""
    return shlex.quote(str(path))


@dataclass(frozen=True)
class InstallPlan:
    """Everything ``voiced install`` is going to do, as data."""

    user: str
    home: Path
    venv_dir: Path
    config_file: Path
    log_dir: Path
    staging_dir: Path
    labels: dict[str, str]  # every DaemonName including "metalprobe"
    source: str
    python: str = ""
    uv: str | None = None
    hf_home: Path | None = None
    # VOICED_ESPEAK_LIBRARY / VOICED_ESPEAK_DATA from the installing shell, so the
    # daemons load the same espeak-ng that `voiced doctor` found.
    espeak_env: dict[str, str] = field(default_factory=dict)
    extras: tuple[str, ...] = EXTRAS
    pip_python: str = field(default_factory=lambda: sys.executable)

    # Paths -------------------------------------------------------------

    @property
    def python_bin(self) -> Path:
        return self.venv_dir / "bin" / "python"

    @property
    def voiced_bin(self) -> Path:
        return self.venv_dir / "bin" / "voiced"

    def staged_plist(self, service: str) -> Path:
        return self.staging_dir / f"{self.labels[service]}.plist"

    @property
    def install_script_path(self) -> Path:
        return self.staging_dir / "install-daemons.sh"

    @property
    def uninstall_script_path(self) -> Path:
        return self.staging_dir / "uninstall-daemons.sh"

    @property
    def requirement(self) -> str:
        """The pip requirement to install: the source plus the engine extras,
        kept valid PEP 508 (see :func:`requirement_with_extras`)."""
        return requirement_with_extras(self.source, self.extras)

    # Plists ------------------------------------------------------------

    def environment(self) -> dict[str, str]:
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.venv_dir / 'bin'}:{BASE_PATH}",
            "VOICED_CONFIG": str(self.config_file),
            "PYTHONUNBUFFERED": "1",
        }
        if self.hf_home is not None:
            env["HF_HOME"] = str(self.hf_home)
        env.update(self.espeak_env)
        return env

    def program_arguments(self, service: str) -> list[str]:
        if service == "watchdog":
            return [str(self.voiced_bin), "watchdog"]
        return [str(self.voiced_bin), "serve", service]

    def render(self, service: str) -> bytes:
        return render_plist(
            label=self.labels[service],
            program_arguments=self.program_arguments(service),
            log_dir=self.log_dir,
            user_name=self.user,
            working_directory=self.home,
            environment=self.environment(),
        )

    def plists(self) -> dict[str, bytes]:
        return {service: self.render(service) for service in SERVICES}

    # Shell commands ----------------------------------------------------

    def venv_commands(self) -> list[list[str]]:
        """Commands that create the venv and install the package into it."""
        if self.uv:
            python = self.python or DEFAULT_PYTHON_VERSION
            return [
                [self.uv, "venv", "--allow-existing", "--python", python, str(self.venv_dir)],
                [
                    self.uv,
                    "pip",
                    "install",
                    "--python",
                    str(self.python_bin),
                    "--upgrade",
                    self.requirement,
                ],
            ]
        python = self.python or self.pip_python
        return [
            [python, "-m", "venv", str(self.venv_dir)],
            [str(self.python_bin), "-m", "pip", "install", "--upgrade", "pip"],
            [str(self.python_bin), "-m", "pip", "install", "--upgrade", self.requirement],
        ]

    def model_download_command(self) -> list[str]:
        env = ["/usr/bin/env", f"VOICED_CONFIG={self.config_file}"]
        if self.hf_home is not None:
            env.append(f"HF_HOME={self.hf_home}")
        env += [f"{key}={value}" for key, value in self.espeak_env.items()]
        return [*env, str(self.python_bin), "-c", MODEL_DOWNLOAD_SCRIPT]

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
            f"echo {q(f'  {self.voiced_bin} status')}",
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
            environment=self.environment(),
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


# Runs inside the venv's interpreter, which has the engine extras installed.
# Loads each configured engine once so the weights land in the Hugging Face
# cache; falls back to a plain snapshot download when the engine plugin is not
# installed (for example a dev install without the MLX extras).
MODEL_DOWNLOAD_SCRIPT = """
import asyncio, sys
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


def plan_install(
    settings: Settings,
    *,
    venv_dir: Path | None = None,
    python: str | None = None,
    source: str | None = None,
    user: str | None = None,
    home: Path | None = None,
    staging_dir: Path | None = None,
    config_file: Path | None = None,
    uv: str | Literal["auto"] | None = "auto",
    extras: Sequence[str] = EXTRAS,
) -> InstallPlan:
    """Resolve CLI flags, config and defaults into an :class:`InstallPlan`."""
    config_file = config_file or config_path()
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
        user=user or getpass.getuser(),
        home=home or Path.home(),
        venv_dir=(venv_dir or settings.install.venv_dir).expanduser(),
        config_file=config_file,
        log_dir=settings.server.log_dir,
        staging_dir=(staging_dir or config_file.parent / "launchd").expanduser(),
        labels={name: settings.launchd.label(name) for name in (*SERVICES, "metalprobe")},
        source=source or default_source(),
        python=python if python is not None else settings.install.python,
        uv=find_uv() if uv == "auto" else uv,
        hf_home=hf_home,
        espeak_env=espeak_env,
        extras=tuple(extras),
    )


def plan_summary(plan: InstallPlan) -> str:
    """Human-readable plan, printed before both the dry and the real run."""
    installer = f"uv ({plan.uv})" if plan.uv else f"pip ({plan.pip_python})"
    lines = [
        f"user           {plan.user}",
        f"venv           {plan.venv_dir}",
        f"installer      {installer}",
        f"package        {plan.requirement}",
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


def run_install(
    plan: InstallPlan,
    *,
    runner: Runner = run_streaming,
    dry_run: bool = False,
    skip_models: bool = False,
    say: Say = print,
) -> None:
    """The user-level steps, in order. ``dry_run`` reports each step and does nothing.

    Long steps (venv, pip, model download) run through ``runner``, which by
    default streams output and has no timeout. Raises :class:`InstallError`.
    """
    would = "would " if dry_run else ""

    def step(argv: Sequence[str], what: str, shown: str | None = None) -> None:
        say(f"$ {shown or shlex.join(argv)}")
        if dry_run:
            return
        result = runner(argv)
        if not result.ok:
            raise InstallError(
                f"{what} failed (exit {result.returncode}): {result.output}".rstrip()
            )

    if plan.config_file.exists():
        say(f"config {plan.config_file}: exists, kept")
        # Installs before 0600 became the default left it readable by every
        # local account; it is where api_key goes.
        mode = stat.S_IMODE(plan.config_file.stat().st_mode)
        if mode & 0o077:
            say(f"{would}tighten {plan.config_file} from {mode:o} to 600")
            if not dry_run:
                os.chmod(plan.config_file, mode & 0o700 | 0o600)
    elif dry_run:
        say(f"config {plan.config_file}: would be created from config.example.toml")
    else:
        write_default_config(plan.config_file)
        say(f"config {plan.config_file}: created from config.example.toml")

    say(f"{would}create log dir {plan.log_dir}")
    if not dry_run:
        plan.log_dir.mkdir(parents=True, exist_ok=True)

    for argv in plan.venv_commands():
        step(argv, argv[0])
    if not dry_run and not plan.voiced_bin.exists():
        raise InstallError(f"{plan.voiced_bin} missing after install")

    if skip_models:
        say("skipping model download (--skip-models)")
    else:
        step(
            plan.model_download_command(),
            "model download",
            shown=f"{plan.python_bin} -c <model download>",
        )

    for service in SERVICES:
        say(f"{would}write {plan.staged_plist(service)}")
    say(f"{would}write {plan.install_script_path}")
    say(f"{would}write {plan.uninstall_script_path}")
    if not dry_run:
        write_staging(plan)


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
    prune_dirs: list[Path]  # removed afterwards only if empty

    def all(self) -> list[Path]:
        return [*self.trees, *self.files]


def uninstall_targets(
    plan: InstallPlan, settings: Settings, *, logs: bool, models: bool, config: bool
) -> UninstallTargets:
    """What ``uninstall`` deletes.

    The log dir and the staging dir may be shared with other things
    (``log_dir = "~/Library/Logs"`` is a legal config), so only the files
    voiced wrote are removed there, and the directory itself only if that
    leaves it empty.
    """
    trees = [plan.venv_dir]
    files: list[Path] = []
    prune: list[Path] = []
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
    return UninstallTargets(trees=trees, files=files, prune_dirs=prune)


def remove_targets(targets: UninstallTargets, plan: InstallPlan, say: Say) -> int:
    """Delete the targets with the safety rules; returns how many were removed."""
    removed = 0
    for tree in targets.trees:
        if not tree.exists() and not tree.is_symlink():
            continue
        reason = removal_refusal(tree, plan.home)
        if reason is not None:
            say(f"refusing to remove {tree}: {reason}")
            continue
        if tree == plan.venv_dir and not (tree / "pyvenv.cfg").is_file():
            say(f"refusing to remove {tree}: not a virtual environment (no pyvenv.cfg)")
            continue
        shutil.rmtree(tree)
        say(f"removed {tree}")
        removed += 1
    for file in targets.files:
        if file.is_symlink() or file.is_file():
            file.unlink()
            say(f"removed {file}")
            removed += 1
    for directory in targets.prune_dirs:
        if directory.is_dir() and not any(directory.iterdir()):
            if removal_refusal(directory, plan.home) is None:
                directory.rmdir()
                say(f"removed empty {directory}")
    return removed
