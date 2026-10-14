"""Installer: plan, render and (optionally) execute ``voiced install``.

The plan is a plain value (:class:`InstallPlan`) so ``--dry-run`` and the tests
can render every artifact without touching the machine. Steps that need root
(copying plists into ``/Library/LaunchDaemons`` and bootstrapping them) are
never executed here; they are rendered as a shell script for the user to
review and run with ``sudo``. See ADR 0006 for the layout.
"""

from __future__ import annotations

import getpass
import os
import shlex
import shutil
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import voiced
from voiced.config import Settings, config_path
from voiced.launchd.control import (
    LAUNCH_DAEMONS_DIR,
    CommandResult,
    Runner,
    bootout_command,
    bootstrap_command,
    plist_path,
    run_command,
)
from voiced.launchd.plist import render_plist

Service = Literal["stt", "tts", "watchdog"]
SERVICES: tuple[Service, ...] = ("stt", "tts", "watchdog")
EXTRAS: tuple[str, ...] = ("stt", "tts")

# Label prefixes used by earlier releases. ``uninstall`` boots these out too
# (ADR 0003). Empty until the prefix changes for the first time.
LEGACY_LABEL_PREFIXES: tuple[str, ...] = ()

# Homebrew (both architectures) first so espeak-ng and ffmpeg are found, then
# the system paths. The venv's bin directory is prepended per plan.
BASE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
DEFAULT_PYTHON_VERSION = "3.12"


def repo_root() -> Path | None:
    """The checkout this package was imported from, if it is a dev checkout."""
    candidate = Path(voiced.__file__).resolve().parents[2]
    pyproject = candidate / "pyproject.toml"
    if pyproject.is_file() and 'name = "voiced"' in pyproject.read_text(errors="replace"):
        return candidate
    return None


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


@dataclass(frozen=True)
class InstallPlan:
    """Everything ``voiced install`` is going to do, as data."""

    user: str
    home: Path
    venv_dir: Path
    config_file: Path
    log_dir: Path
    staging_dir: Path
    labels: dict[str, str]
    source: str
    python: str = ""
    uv: str | None = None
    hf_home: Path | None = None
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
        extras = ",".join(self.extras)
        return f"{self.source}[{extras}]" if extras else self.source

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
        return [*env, str(self.python_bin), "-c", MODEL_DOWNLOAD_SCRIPT]

    def sudo_install_script(self) -> str:
        lines = [
            "#!/bin/bash",
            "# voiced: install the LaunchDaemons. Review, then run with sudo.",
            "# Generated by `voiced install`; safe to re-run after a config change.",
            "set -euo pipefail",
            f"DAEMONS={LAUNCH_DAEMONS_DIR}",
            f"STAGING={shlex.quote(str(self.staging_dir))}",
            "",
        ]
        for service in SERVICES:
            label = self.labels[service]
            lines += [
                f"# {service}",
                f"{bootout_command(label)} 2>/dev/null || true",
                f'cp "$STAGING/{label}.plist" "$DAEMONS/{label}.plist"',
                f'chown root:wheel "$DAEMONS/{label}.plist"',
                f'chmod 644 "$DAEMONS/{label}.plist"',
                bootstrap_command(label),
                "",
            ]
        lines += [
            "echo 'voiced daemons installed. Check with:'",
            f"echo '  {self.voiced_bin} status'",
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
                f"{bootout_command(label)} 2>/dev/null || true",
                f"rm -f {plist_path(label)}",
            ]
        lines += ["", "echo 'voiced daemons removed.'", ""]
        return "\n".join(lines)


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
    return InstallPlan(
        user=user or getpass.getuser(),
        home=home or Path.home(),
        venv_dir=(venv_dir or settings.install.venv_dir).expanduser(),
        config_file=config_file,
        log_dir=settings.server.log_dir,
        staging_dir=(staging_dir or config_file.parent / "launchd").expanduser(),
        labels={service: settings.launchd.label(service) for service in SERVICES},
        source=source or default_source(),
        python=python if python is not None else settings.install.python,
        uv=find_uv() if uv == "auto" else uv,
        hf_home=settings.install.hf_home,
        extras=tuple(extras),
    )


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
    """Create the config file from the example if it does not exist."""
    if config_file.exists():
        return False
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(example_config_text())
    return True


class InstallError(RuntimeError):
    pass


def _check(step: str, result: CommandResult) -> None:
    if not result.ok:
        raise InstallError(f"{step} failed (exit {result.returncode}):\n{result.output}")


def run_install(
    plan: InstallPlan,
    *,
    runner: Runner = run_command,
    skip_models: bool = False,
    say: Callable[[str], None] = print,
) -> None:
    """Execute the user-level steps of the plan. Raises :class:`InstallError`."""

    if write_default_config(plan.config_file):
        say(f"wrote default config to {plan.config_file}")
    else:
        say(f"keeping existing config {plan.config_file}")

    plan.log_dir.mkdir(parents=True, exist_ok=True)
    say(f"log dir {plan.log_dir}")

    for argv in plan.venv_commands():
        say("$ " + shlex.join(argv))
        _check(argv[0], runner(argv))
    if not plan.voiced_bin.exists() and runner is run_command:
        raise InstallError(f"{plan.voiced_bin} missing after install")

    if skip_models:
        say("skipping model download (--skip-models)")
    else:
        say(f"$ {plan.python_bin} -c '<download configured models>'")
        _check("model download", runner(plan.model_download_command()))

    for path in write_staging(plan):
        say(f"staged {path}")


def plan_summary(plan: InstallPlan) -> str:
    """Human-readable plan, used by ``--dry-run`` and before the real run."""
    lines = [
        f"user           {plan.user}",
        f"venv           {plan.venv_dir}",
        f"installer      {'uv (' + plan.uv + ')' if plan.uv else 'pip (' + plan.pip_python + ')'}",
        f"package        {plan.requirement}",
        f"config         {plan.config_file}",
        f"log dir        {plan.log_dir}",
        f"staging        {plan.staging_dir}",
        f"hf_home        {plan.hf_home or '(Hugging Face default)'}",
    ]
    for service in SERVICES:
        lines.append(f"label {service:<9}{plan.labels[service]}")
    return "\n".join(lines)


def is_root() -> bool:
    return os.geteuid() == 0
