"""Lifecycle subcommands: install, start, stop, status, logs, uninstall, doctor, watchdog.

Parsing and printing live here; file writes and script generation live in
``voiced.launchd``. Nothing here runs ``sudo``: steps that need root are
printed as commands or scripts for the user.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from voiced import doctor
from voiced.config import Settings
from voiced.launchd.control import (
    Runner,
    bootout_argv,
    bootstrap_argv,
    kickstart_argv,
    plist_path,
    print_service,
    run_command,
    run_streaming,
)
from voiced.launchd.install import (
    SERVICES,
    InstallError,
    Service,
    is_root,
    plan_install,
    plan_summary,
    remove_targets,
    run_install,
    uninstall_targets,
)
from voiced.launchd.plist import log_paths
from voiced.table import render_table
from voiced.watchdog import (
    Observation,
    Policy,
    ProbeRequest,
    ServiceSpec,
    Watchdog,
    configure_watchdog_logging,
    multipart,
    poll_health,
    silent_wav,
)

Echo = Callable[[str], None]
SERVICE_CHOICES = (*SERVICES, "all")


def add_lifecycle_parsers(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    install = sub.add_parser("install", help="create the venv, download models, stage the plists")
    install.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    install.add_argument("--venv", type=Path, help="venv location (default: [install] venv_dir)")
    install.add_argument("--python", help='interpreter or version for the venv, e.g. "3.12"')
    install.add_argument("--source", help="pip requirement or path to install (default: auto)")
    install.add_argument("--user", help="UserName for the daemons (default: current user)")
    install.add_argument("--staging-dir", type=Path, help="where plists and scripts are written")
    install.add_argument("--skip-models", action="store_true", help="do not download models")
    install.add_argument("--no-uv", action="store_true", help="use python -m venv and pip")

    start = sub.add_parser("start", help="start the daemons")
    start.add_argument("service", nargs="?", choices=SERVICE_CHOICES, default="all")

    stop = sub.add_parser("stop", help="stop the daemons (prints the sudo command)")
    stop.add_argument("service", nargs="?", choices=SERVICE_CHOICES, default="all")

    status = sub.add_parser("status", help="launchd state and /health of each service")
    status.add_argument("--json", action="store_true", help="machine-readable output")

    logs = sub.add_parser("logs", help="show the service logs")
    logs.add_argument("service", nargs="?", choices=SERVICE_CHOICES, default="all")
    logs.add_argument("-n", "--lines", type=int, default=50)
    logs.add_argument("-f", "--follow", action="store_true", help="keep printing (tail -F)")
    logs.add_argument("--stderr", action="store_true", help="also show launchd's stderr capture")

    uninstall = sub.add_parser("uninstall", help="print the sudo script and remove the venv")
    uninstall.add_argument("--venv", type=Path, help="venv location (default: [install] venv_dir)")
    uninstall.add_argument("--yes", action="store_true", help="actually delete files")
    uninstall.add_argument(
        "--remove-config", action="store_true", help="also delete the config file and staging dir"
    )
    uninstall.add_argument("--remove-logs", action="store_true", help="also delete the log dir")
    uninstall.add_argument(
        "--remove-models", action="store_true", help="also delete the configured models' cache"
    )

    doc = sub.add_parser("doctor", help="check this machine")
    doc.add_argument("--json", action="store_true", help="machine-readable output")
    doc.add_argument(
        "--metal-probe",
        action="store_true",
        help="stage a one-shot LaunchDaemon that tests Metal and print the sudo script",
    )

    wd = sub.add_parser("watchdog", help="run the health watchdog in the foreground")
    wd.add_argument("--once", action="store_true", help="poll once, print decisions, exit")


def run_lifecycle(
    args: argparse.Namespace, settings: Settings, runner: Runner = run_command, echo: Echo = print
) -> int:
    handlers: dict[str, Callable[[], int]] = {
        "install": lambda: cmd_install(args, settings, echo),
        "start": lambda: cmd_start(args.service, settings, runner, echo),
        "stop": lambda: cmd_stop(args.service, settings, runner, echo),
        "status": lambda: cmd_status(settings, runner, echo, as_json=args.json),
        "logs": lambda: cmd_logs(args, settings, echo),
        "uninstall": lambda: cmd_uninstall(args, settings, runner, echo),
        "doctor": lambda: cmd_doctor(args, settings, runner, echo),
        "watchdog": lambda: cmd_watchdog(settings, echo, once=args.once),
    }
    return handlers[args.command]()


def _services(name: str) -> tuple[Service, ...]:
    if name == "all":
        return SERVICES
    for service in SERVICES:
        if service == name:
            return (service,)
    raise ValueError(f"unknown service {name!r}")


# install ------------------------------------------------------------------


def cmd_install(
    args: argparse.Namespace, settings: Settings, echo: Echo, runner: Runner = run_streaming
) -> int:
    plan = plan_install(
        settings,
        venv_dir=args.venv,
        python=args.python,
        source=args.source,
        user=args.user,
        staging_dir=args.staging_dir,
        uv=None if args.no_uv else "auto",
    )
    echo(plan_summary(plan))
    echo("")
    if args.dry_run:
        echo("dry run: nothing is written. Steps that would run:")
    try:
        run_install(
            plan, runner=runner, dry_run=args.dry_run, skip_models=args.skip_models, say=echo
        )
    except InstallError as exc:
        echo(f"install failed: {exc}")
        return 1
    if args.dry_run:
        for service in SERVICES:
            echo("")
            echo(f"--- {plan.staged_plist(service)} (would be written)")
            echo(plan.render(service).decode().rstrip())
        echo("")
        echo(f"--- {plan.install_script_path} (would be written)")
        echo(plan.sudo_install_script().rstrip())
        return 0
    echo("")
    echo("User-level install done. The LaunchDaemons need root. Review and run:")
    echo("")
    echo(f"  sudo bash {shlex.quote(str(plan.install_script_path))}")
    echo("")
    echo(plan.sudo_install_script().rstrip())
    echo("")
    echo(f"Then: {plan.voiced_bin} status")
    return 0


# start / stop -------------------------------------------------------------


def as_root_or_print(
    argv: Sequence[str], runner: Runner, echo: Echo, pending: list[str], done: str
) -> bool:
    """Run a root-only launchctl command if we are root, else queue it for printing."""
    if not is_root():
        pending.append(f"sudo {shlex.join(argv)}")
        return False
    result = runner(argv)
    echo(done if result.ok else f"{shlex.join(argv)}: {result.output}")
    return result.ok


def _print_pending(pending: list[str], echo: Echo, why: str) -> None:
    echo(f"{why} Run:")
    for line in pending:
        echo(f"  {line}")


def cmd_start(service: str, settings: Settings, runner: Runner, echo: Echo) -> int:
    pending: list[str] = []
    failures = 0
    for name in _services(service):
        label = settings.launchd.label(name)
        if print_service(label, runner).loaded:
            result = runner(kickstart_argv(label))
            if result.ok:
                echo(f"{name}: kickstarted {label}")
            else:
                failures += 1
                echo(f"{name}: launchctl kickstart failed: {result.output}")
        elif plist_path(label).is_file():
            if not as_root_or_print(
                bootstrap_argv(label), runner, echo, pending, f"{name}: bootstrapped {label}"
            ):
                failures += 1
        else:
            failures += 1
            echo(f"{name}: {plist_path(label)} is not installed; run voiced install")
    if pending:
        _print_pending(pending, echo, "Loading a LaunchDaemon needs root.")
    return 1 if failures else 0


def cmd_stop(service: str, settings: Settings, runner: Runner, echo: Echo) -> int:
    pending: list[str] = []
    failures = 0
    for name in _services(service):
        label = settings.launchd.label(name)
        if not print_service(label, runner).loaded:
            echo(f"{name}: not loaded")
            continue
        if not as_root_or_print(bootout_argv(label), runner, echo, pending, f"{name}: stopped"):
            failures += 1
    if pending:
        _print_pending(pending, echo, "Stopping a LaunchDaemon needs root.")
        echo("(KeepAlive would otherwise restart the process; bootout unloads it until")
        echo(" the next boot or `voiced start`.)")
    return 1 if failures else 0


# status -------------------------------------------------------------------


def _ago(ts: float | None) -> str:
    if ts is None:
        return "never"
    delta = max(0.0, time.time() - ts)
    if delta < 90:
        return f"{delta:.0f}s ago"
    if delta < 5400:
        return f"{delta / 60:.0f}m ago"
    return f"{delta / 3600:.1f}h ago"


def _health_detail(obs: Observation) -> str:
    p = obs.payload
    if p is None or obs.kind in ("unreachable", "error"):
        return obs.detail
    parts = [str(p.get("model", ""))]
    parts.append(f"queue {p.get('queue_depth', '?')}/{p.get('queue_capacity', '?')}")
    if p.get("last_inference_ms") is not None:
        parts.append(f"last {p['last_inference_ms']:.0f} ms {_ago(p.get('last_inference_at'))}")
    if p.get("last_error"):
        parts.append(f"error: {p['last_error']}")
    return ", ".join(part for part in parts if part)


def collect_status(
    settings: Settings,
    runner: Runner,
    poll: Callable[[str], Observation] | None = None,
) -> list[dict[str, Any]]:
    poll = poll or (lambda url: poll_health(url, timeout_s=3.0, api_key=settings.server.api_key))
    rows: list[dict[str, Any]] = []
    for service in SERVICES:
        label = settings.launchd.label(service)
        ls = print_service(label, runner)
        row: dict[str, Any] = {
            "service": service,
            "label": label,
            "installed": plist_path(label).is_file(),
            "launchd": ls.summary,
            "pid": ls.pid,
            "last_exit_code": ls.last_exit_code,
        }
        if service != "watchdog":
            obs = poll(f"{settings.http_url(service)}/health")
            row["health"] = obs.kind
            row["health_detail"] = _health_detail(obs)
            row["health_payload"] = obs.payload
        rows.append(row)
    return rows


def cmd_status(settings: Settings, runner: Runner, echo: Echo, *, as_json: bool = False) -> int:
    rows = collect_status(settings, runner)
    if as_json:
        echo(json.dumps(rows, indent=2))
    else:
        table = [
            (
                r["service"],
                r["launchd"] + (f" (pid {r['pid']})" if r["pid"] else ""),
                str(r.get("health", "-")),
                str(r.get("health_detail", "")),
            )
            for r in rows
        ]
        echo(render_table(("SERVICE", "LAUNCHD", "HEALTH", "DETAIL"), table))
    healthy = all(r.get("health", "ok") == "ok" and r["pid"] for r in rows)
    return 0 if healthy else 1


# logs ---------------------------------------------------------------------


def log_files(settings: Settings, service: str, *, stderr: bool = False) -> list[Path]:
    files: list[Path] = []
    for name in _services(service):
        paths = log_paths(settings.server.log_dir, name)
        files.append(paths.app)
        if stderr:
            files.append(paths.stderr)
    return files


def tail_lines(path: Path, n: int) -> list[str]:
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            block = 8192
            data = b""
            while size > 0 and data.count(b"\n") <= n:
                step = min(block, size)
                size -= step
                fh.seek(size)
                data = fh.read(step) + data
    except OSError:
        return []
    return data.decode("utf-8", "replace").splitlines()[-n:]


def cmd_logs(args: argparse.Namespace, settings: Settings, echo: Echo) -> int:
    files = log_files(settings, args.service, stderr=args.stderr)
    existing = [f for f in files if f.exists()]
    if not existing:
        echo(f"no log files yet under {settings.server.log_dir}")
        return 1
    if args.follow:
        tail = shutil.which("tail") or "/usr/bin/tail"
        os.execv(tail, [tail, "-n", str(args.lines), "-F", *map(str, existing)])
    for path in existing:
        if len(existing) > 1:
            echo(f"==> {path} <==")
        for line in tail_lines(path, args.lines):
            echo(line)
    return 0


# uninstall ----------------------------------------------------------------


def cmd_uninstall(args: argparse.Namespace, settings: Settings, runner: Runner, echo: Echo) -> int:
    plan = plan_install(settings, venv_dir=args.venv)
    script = plan.sudo_uninstall_script()
    try:
        plan.staging_dir.mkdir(parents=True, exist_ok=True)
        plan.uninstall_script_path.write_text(script)
        echo(
            "Root step (stop and remove the daemons): "
            f"sudo bash {shlex.quote(str(plan.uninstall_script_path))}"
        )
    except OSError:
        echo("Root step (stop and remove the daemons):")
    echo("")
    echo(script.rstrip())
    echo("")

    loaded = [s for s in SERVICES if print_service(settings.launchd.label(s), runner).loaded]
    if loaded:
        echo(f"Daemons still loaded: {', '.join(loaded)}. Run the sudo script first, then")
        echo("re-run `voiced uninstall --yes` to remove the files.")
        return 1

    targets = uninstall_targets(
        plan,
        settings,
        logs=args.remove_logs,
        models=args.remove_models,
        config=args.remove_config,
    )
    if not args.yes:
        echo("Would remove (pass --yes to do it):")
        for t in targets:
            echo(f"  {t}{'' if t.exists() else '  (absent)'}")
        return 0
    if remove_targets(targets, plan, echo) == 0:
        echo("nothing to remove")
    return 0


# doctor -------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace, settings: Settings, runner: Runner, echo: Echo) -> int:
    if args.metal_probe:
        plan = plan_install(settings)
        python = plan.python_bin if plan.python_bin.exists() else Path(sys.executable)
        probe = plan.metal_probe(python)
        probe.write()
        echo(f"staged {probe.plist_path}")
        echo(f"staged {probe.script_path}")
        echo("")
        echo(f"Run:  sudo bash {shlex.quote(str(probe.script_path))}")
        echo("")
        echo(probe.script.rstrip())
        echo("")
        echo(f"Then `voiced doctor` reads the result from {probe.result_file}.")
        return 0

    checks = doctor.run_all(settings, runner)
    if args.json:
        echo(json.dumps([c.__dict__ for c in checks], indent=2))
    else:
        echo(doctor.render_checks(checks))
        echo("")
        echo(f"overall: {doctor.worst(checks)}")
    return 1 if doctor.worst(checks) == "fail" else 0


# watchdog -----------------------------------------------------------------


def watchdog_specs(settings: Settings) -> list[ServiceSpec]:
    wav = silent_wav()
    stt_body, stt_ctype = multipart({"model": settings.stt.model}, "probe.wav", wav)
    tts_body = json.dumps(
        {
            "model": settings.tts.model,
            "input": "ok",
            "voice": settings.tts.default_voice,
            "response_format": "pcm",
        }
    ).encode()
    return [
        ServiceSpec(
            name="stt",
            label=settings.launchd.label("stt"),
            base_url=settings.http_url("stt"),
            api_key=settings.server.api_key,
            probe=ProbeRequest("/v1/audio/transcriptions", stt_body, stt_ctype),
        ),
        ServiceSpec(
            name="tts",
            label=settings.launchd.label("tts"),
            base_url=settings.http_url("tts"),
            api_key=settings.server.api_key,
            probe=ProbeRequest("/v1/audio/speech", tts_body, "application/json"),
        ),
    ]


def watchdog_policy(settings: Settings) -> Policy:
    return Policy(**settings.watchdog.model_dump())


def cmd_watchdog(settings: Settings, echo: Echo, *, once: bool = False) -> int:
    log_file = None if once else log_paths(settings.server.log_dir, "watchdog").app
    configure_watchdog_logging(settings.server.log_level, log_file)
    dog = Watchdog(services=watchdog_specs(settings), policy=watchdog_policy(settings))
    if once:
        for name, decision in dog.tick().items():
            echo(f"{name}: {decision.action}: {decision.reason}")
        return 0
    stopping = False

    def _stop(signum: int, frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    dog.run_forever(stop=lambda: stopping)
    return 0
