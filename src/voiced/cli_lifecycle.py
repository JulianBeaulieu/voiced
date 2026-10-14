"""Lifecycle subcommands: install, start, stop, status, logs, uninstall, doctor, watchdog.

Kept apart from ``voiced.cli`` so the command tree there stays small. Nothing
here runs ``sudo``: steps that need root are printed as commands or scripts.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from voiced import doctor
from voiced.config import Settings
from voiced.launchd.control import (
    Runner,
    ServiceStatus,
    bootout,
    bootout_command,
    bootstrap,
    bootstrap_command,
    kickstart,
    plist_path,
    print_service,
    run_command,
)
from voiced.launchd.install import (
    SERVICES,
    InstallError,
    InstallPlan,
    Service,
    is_root,
    plan_install,
    plan_summary,
    run_install,
)
from voiced.launchd.plist import render_plist
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

    start = sub.add_parser("start", help="start (or restart) the daemons")
    start.add_argument("service", nargs="?", choices=SERVICE_CHOICES, default="all")
    start.add_argument("--kill", action="store_true", help="kill a running instance first")

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
        "--remove-config", action="store_true", help="also delete the config dir"
    )
    uninstall.add_argument("--remove-logs", action="store_true", help="also delete the log dir")
    uninstall.add_argument(
        "--remove-models", action="store_true", help="also delete the configured models' cache"
    )
    uninstall.add_argument(
        "--force", action="store_true", help="remove files even if daemons are still loaded"
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
        "install": lambda: cmd_install(args, settings, runner, echo),
        "start": lambda: cmd_start(args.service, settings, runner, echo, kill=args.kill),
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


def cmd_install(args: argparse.Namespace, settings: Settings, runner: Runner, echo: Echo) -> int:
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
        config_state = "exists, kept" if plan.config_file.exists() else "would be created"
        echo(f"  config {plan.config_file}: {config_state}")
        echo(f"  mkdir -p {plan.log_dir}")
        for argv in plan.venv_commands():
            echo("  $ " + " ".join(argv))
        if not args.skip_models:
            echo(f"  $ {plan.python_bin} -c '<download configured models>'")
        for service in SERVICES:
            echo("")
            echo(f"--- {plan.staged_plist(service)}")
            echo(plan.render(service).decode().rstrip())
        echo("")
        echo(f"--- {plan.install_script_path}")
        echo(plan.sudo_install_script().rstrip())
        return 0

    try:
        run_install(plan, runner=runner, skip_models=args.skip_models, say=echo)
    except InstallError as exc:
        echo(f"install failed: {exc}")
        return 1
    echo("")
    echo("User-level install done. The LaunchDaemons need root. Review and run:")
    echo("")
    echo(f"  sudo bash {plan.install_script_path}")
    echo("")
    echo(plan.sudo_install_script().rstrip())
    echo("")
    echo(f"Then: {plan.voiced_bin} status")
    return 0


# start / stop -------------------------------------------------------------


def _privileged(command: str, runner: Runner, echo: Echo, run: Callable[[], Any]) -> bool:
    """Run ``run()`` when root; otherwise print the sudo command. True if done."""
    if is_root():
        result = run()
        if not result.ok:
            echo(f"{command}: {result.output}")
            return False
        return True
    echo(f"  sudo {command}")
    return False


def cmd_start(
    service: str, settings: Settings, runner: Runner, echo: Echo, *, kill: bool = False
) -> int:
    pending: list[str] = []
    failures = 0
    for name in _services(service):
        label = settings.launchd.label(name)
        status = print_service(label, runner)
        if status.loaded:
            result = kickstart(label, kill=kill, runner=runner)
            if result.ok:
                echo(f"{name}: kickstarted {label}")
            else:
                failures += 1
                echo(f"{name}: launchctl kickstart failed: {result.output}")
                pending.append(f"sudo launchctl kickstart{' -k' if kill else ''} system/{label}")
        elif plist_path(label).is_file():
            if is_root():
                result = bootstrap(label, runner)
                echo(f"{name}: {'bootstrapped' if result.ok else result.output}")
                failures += 0 if result.ok else 1
            else:
                pending.append(f"sudo {bootstrap_command(label)}")
        else:
            failures += 1
            echo(f"{name}: {plist_path(label)} is not installed; run voiced install")
    if pending:
        echo("These need root; run:")
        for line in pending:
            echo(f"  {line}")
        return 1
    return 1 if failures else 0


def cmd_stop(service: str, settings: Settings, runner: Runner, echo: Echo) -> int:
    pending: list[str] = []
    for name in _services(service):
        label = settings.launchd.label(name)
        if not print_service(label, runner).loaded:
            echo(f"{name}: not loaded")
            continue
        if is_root():
            result = bootout(label, runner)
            echo(f"{name}: {'stopped' if result.ok else result.output}")
            if not result.ok:
                pending.append(bootout_command(label))
        else:
            pending.append(f"sudo {bootout_command(label)}")
    if pending:
        echo("Stopping a LaunchDaemon needs root. Run:")
        for line in pending:
            echo(f"  {line}")
        echo("(KeepAlive would otherwise restart the process; bootout unloads it until")
        echo(" the next boot or `voiced start`.)")
        return 1
    return 0


# status -------------------------------------------------------------------


def _health_host(settings: Settings) -> str:
    bind = settings.server.bind
    return "127.0.0.1" if bind in ("0.0.0.0", "") else bind


def _service_base_url(settings: Settings, service: str) -> str:
    port = settings.stt.http_port if service == "stt" else settings.tts.http_port
    return f"http://{_health_host(settings)}:{port}"


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
        ls: ServiceStatus = print_service(label, runner)
        row: dict[str, Any] = {
            "service": service,
            "label": label,
            "installed": plist_path(label).is_file(),
            "launchd": ls.summary,
            "pid": ls.pid,
            "last_exit_code": ls.last_exit_code,
        }
        if service != "watchdog":
            obs = poll(f"{_service_base_url(settings, service)}/health")
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
        echo(doctor.render_table(("SERVICE", "LAUNCHD", "HEALTH", "DETAIL"), table))
    healthy = all(r.get("health", "ok") == "ok" and r["pid"] for r in rows)
    return 0 if healthy else 1


# logs ---------------------------------------------------------------------


def log_files(settings: Settings, service: str, *, stderr: bool = False) -> list[Path]:
    files: list[Path] = []
    for name in _services(service):
        files.append(settings.server.log_dir / f"{name}.log")
        if stderr:
            files.append(settings.server.log_dir / f"{name}.stderr.log")
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


def hf_cache_dirs(settings: Settings) -> list[Path]:
    """Hugging Face cache directories for the configured models."""
    home = settings.install.hf_home or Path(
        os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")
    )
    hub = home.expanduser() / "hub"
    return [
        hub / f"models--{m.replace('/', '--')}" for m in (settings.stt.model, settings.tts.model)
    ]


def uninstall_targets(
    args: argparse.Namespace, settings: Settings, plan: InstallPlan
) -> list[Path]:
    targets = [plan.venv_dir]
    if args.remove_logs:
        targets.append(settings.server.log_dir)
    if args.remove_models:
        targets += hf_cache_dirs(settings)
    if args.remove_config:
        targets.append(plan.config_file.parent)
    return targets


def cmd_uninstall(args: argparse.Namespace, settings: Settings, runner: Runner, echo: Echo) -> int:
    plan = plan_install(settings, venv_dir=args.venv)
    script = plan.sudo_uninstall_script()
    try:
        plan.staging_dir.mkdir(parents=True, exist_ok=True)
        plan.uninstall_script_path.write_text(script)
        echo(f"Root step (stop and remove the daemons): sudo bash {plan.uninstall_script_path}")
    except OSError:
        echo("Root step (stop and remove the daemons):")
    echo("")
    echo(script.rstrip())
    echo("")

    loaded = [s for s in SERVICES if print_service(settings.launchd.label(s), runner).loaded]
    if loaded and not args.force:
        echo(f"Daemons still loaded: {', '.join(loaded)}. Run the sudo script first, then")
        echo("re-run `voiced uninstall --yes` to remove the files (or pass --force).")
        return 1

    targets = uninstall_targets(args, settings, plan)
    present = [t for t in targets if t.exists()]
    if not args.yes:
        echo("Would remove (pass --yes to do it):")
        for t in targets:
            echo(f"  {t}{'' if t.exists() else '  (absent)'}")
        return 0
    for t in present:
        if t.is_dir():
            shutil.rmtree(t)
        else:
            t.unlink()
        echo(f"removed {t}")
    if not present:
        echo("nothing to remove")
    return 0


# doctor -------------------------------------------------------------------


def metal_probe_script(
    settings: Settings, plan: InstallPlan, python: Path
) -> tuple[Path, str, bytes]:
    """A self-cleaning sudo script plus the one-shot plist it bootstraps."""
    label = f"{settings.launchd.label_prefix}.metalprobe"
    out = settings.server.log_dir / doctor.METAL_PROBE_FILE
    plist = render_plist(
        label=label,
        program_arguments=[str(python), "-m", "voiced.launchd.metal_probe", "--out", str(out)],
        log_dir=settings.server.log_dir,
        user_name=plan.user,
        working_directory=plan.home,
        environment=plan.environment(),
        keep_alive=False,
    )
    staged = plan.staging_dir / f"{label}.plist"
    script = f"""#!/bin/bash
# voiced: one-shot LaunchDaemon that checks MLX/Metal without a GUI session.
# Installs the job, waits for its result, then removes it again.
set -euo pipefail
LABEL={label}
DST=/Library/LaunchDaemons/$LABEL.plist
OUT={out}
rm -f "$OUT"
launchctl bootout system/$LABEL 2>/dev/null || true
cp {staged} "$DST"
chown root:wheel "$DST"
chmod 644 "$DST"
launchctl bootstrap system "$DST"
for _ in $(seq 1 60); do [ -f "$OUT" ] && break; sleep 1; done
launchctl bootout system/$LABEL 2>/dev/null || true
rm -f "$DST"
if [ -f "$OUT" ]; then cat "$OUT"; else
  echo "no result after 60 s; see {settings.server.log_dir}/metalprobe.stderr.log"; exit 1
fi
"""
    return staged, script, plist


def cmd_doctor(args: argparse.Namespace, settings: Settings, runner: Runner, echo: Echo) -> int:
    if args.metal_probe:
        plan = plan_install(settings)
        python = plan.python_bin if plan.python_bin.exists() else Path(sys.executable)
        staged, script, plist = metal_probe_script(settings, plan, python)
        plan.staging_dir.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(plist)
        script_path = plan.staging_dir / "metal-probe.sh"
        script_path.write_text(script)
        echo(f"staged {staged}")
        echo(f"staged {script_path}")
        echo("")
        echo(f"Run:  sudo bash {script_path}")
        echo("")
        echo(script.rstrip())
        echo("")
        echo(
            "Then `voiced doctor` reads the result from "
            f"{settings.server.log_dir / doctor.METAL_PROBE_FILE}."
        )
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
            base_url=_service_base_url(settings, "stt"),
            api_key=settings.server.api_key,
            probe=ProbeRequest("/v1/audio/transcriptions", stt_body, stt_ctype),
        ),
        ServiceSpec(
            name="tts",
            label=settings.launchd.label("tts"),
            base_url=_service_base_url(settings, "tts"),
            api_key=settings.server.api_key,
            probe=ProbeRequest("/v1/audio/speech", tts_body, "application/json"),
        ),
    ]


def watchdog_policy(settings: Settings) -> Policy:
    w = settings.watchdog
    return Policy(
        interval_s=w.interval_s,
        timeout_s=w.timeout_s,
        failures_before_restart=w.failures_before_restart,
        startup_grace_s=w.startup_grace_s,
        degraded_window_s=w.degraded_window_s,
        restart_cooldown_s=w.restart_cooldown_s,
        probe=w.probe,
        probe_timeout_s=w.probe_timeout_s,
    )


def cmd_watchdog(settings: Settings, echo: Echo, *, once: bool = False) -> int:
    configure_watchdog_logging(settings.server.log_level, settings.server.log_dir)
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
