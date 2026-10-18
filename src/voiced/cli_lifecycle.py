"""Lifecycle subcommands: install, start, stop, restart, status, settings, logs,
uninstall, doctor, watchdog.

Parsing and printing live here; file writes and script generation live in
``voiced.launchd``. Nothing here runs ``sudo``: steps that need root are
printed as commands or scripts for the user.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import json
import logging
import os
import shlex
import shutil
import signal
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from voiced import __version__
from voiced.config import (
    HTTP_SERVICES,
    SERVICES,
    DaemonName,
    Settings,
    config_path,
    load_settings,
)
from voiced.control import NOT_SERVED, socket_path, state_dir
from voiced.control.client import (
    DEFAULT_TIMEOUT_S,
    ControlClient,
    ControlError,
    ControlUnavailable,
    request,
)
from voiced.control.logs import tail_lines
from voiced.control.ops import NoSupervisor, Operations, engines_in_subprocess
from voiced.control.protocol import OpError
from voiced.launchd.control import (
    Runner,
    StreamingRunner,
    bootout_argv,
    bootstrap_argv,
    kickstart_argv,
    plist_path,
    print_service,
    run_command,
    run_streaming,
)
from voiced.launchd.plist import log_paths
from voiced.layout import (
    ORIGINS,
    RunningEnvironment,
    entry_point,
    install_lock,
    installed_manifest,
)
from voiced.settings_store import SETTABLE
from voiced.table import render_table
from voiced.watchdog import (
    Observation,
    ServiceSpec,
    Watchdog,
    configure_watchdog_logging,
    default_probe,
    poll_health,
    restart_service,
)

if TYPE_CHECKING:
    from voiced.launchd.install import InstallPlan, UninstallTargets
    from voiced.launchd.progress import Progress

# voiced.launchd.install (and voiced.doctor) import the engine modules, and
# with them numpy; they are imported inside the commands that need them so
# that `voiced watchdog` never loads them (ADR 0012, ADR 0013).

Echo = Callable[[str], None]
SERVICE_CHOICES = (*SERVICES, "all")


def add_lifecycle_parsers(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    install = sub.add_parser(
        "install", help="link the environment, download models, stage the plists"
    )
    install.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    install.add_argument(
        "--state-dir",
        "--venv",
        dest="state_dir",
        type=Path,
        help="state directory (default: [install] venv_dir; set that key too so doctor, "
        "uninstall and the daemons agree)",
    )
    install.add_argument(
        "--source",
        type=Path,
        help="checkout to build a new environment from, with uv and the shipped lock "
        "(default: the environment this voiced runs from)",
    )
    install.add_argument("--python", help='Python version for a --source build, e.g. "3.12"')
    install.add_argument("--staging-dir", type=Path, help="where plists and scripts are written")
    install.add_argument("--skip-models", action="store_true", help="do not download models")
    install.add_argument(
        "--origin",
        choices=ORIGINS,
        help="who made the environment, for install.json (default: homebrew for a keg, "
        "else cli; the menu bar app passes app)",
    )
    install.add_argument(
        "--json-progress",
        action="store_true",
        help="progress as JSON lines on stdout (ADR 0018); human output goes to stderr",
    )

    start = sub.add_parser("start", help="start the daemons")
    start.add_argument("service", nargs="?", choices=SERVICE_CHOICES, default="all")

    stop = sub.add_parser("stop", help="stop the daemons (prints the sudo command)")
    stop.add_argument("service", nargs="?", choices=SERVICE_CHOICES, default="all")

    restart = sub.add_parser(
        "restart",
        help="restart a service now (no root needed); all also restarts the watchdog, last",
    )
    restart.add_argument(
        "service", nargs="?", choices=(*HTTP_SERVICES, "watchdog", "all"), default="all"
    )

    status = sub.add_parser("status", help="launchd state and /health of each service")
    status.add_argument("--json", action="store_true", help="machine-readable output")

    settings = sub.add_parser("settings", help="show or change the common settings")
    settings.set_defaults(settings_command="get", json=False, pairs=[])
    settings_sub = settings.add_subparsers(dest="settings_command")
    get = settings_sub.add_parser("get", help="show the settings (never the API key)")
    get.add_argument("--json", action="store_true", help="machine-readable output")
    put = settings_sub.add_parser(
        "set",
        help="change settings: KEY=VALUE ...",
        description=f"Settable keys: {', '.join(SETTABLE)}. "
        "Use api_key=- to read the key from stdin, api_key= to clear it.",
    )
    put.add_argument("pairs", nargs="+", metavar="KEY=VALUE")
    put.add_argument("--json", action="store_true", help="machine-readable output")

    logs = sub.add_parser("logs", help="show the service logs")
    logs.add_argument("service", nargs="?", choices=SERVICE_CHOICES, default="all")
    logs.add_argument("-n", "--lines", type=int, default=50)
    logs.add_argument("-f", "--follow", action="store_true", help="keep printing (tail -F)")
    logs.add_argument("--stderr", action="store_true", help="also show launchd's stderr capture")

    uninstall = sub.add_parser(
        "uninstall", help="print the sudo script and remove what voiced installed"
    )
    uninstall.add_argument(
        "--state-dir",
        "--venv",
        dest="state_dir",
        type=Path,
        help="state directory if it differs from [install] venv_dir",
    )
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
        "restart": lambda: cmd_restart(args.service, settings, runner, echo),
        "status": lambda: cmd_status(settings, runner, echo, as_json=args.json),
        "settings": lambda: cmd_settings(args, settings, echo),
        "logs": lambda: cmd_logs(args, settings, echo),
        "uninstall": lambda: cmd_uninstall(args, settings, runner, echo),
        "doctor": lambda: cmd_doctor(args, settings, runner, echo),
        "watchdog": lambda: cmd_watchdog(settings, echo, once=args.once),
    }
    return handlers[args.command]()


def _services(name: str) -> tuple[DaemonName, ...]:
    if name == "all":
        return SERVICES
    for service in SERVICES:
        if service == name:
            return (service,)
    raise ValueError(f"unknown service {name!r}")


# install ------------------------------------------------------------------


def cmd_install(
    args: argparse.Namespace,
    settings: Settings,
    echo: Echo,
    runner: StreamingRunner = run_streaming,
) -> int:
    """With ``--json-progress``, stdout carries only the events of
    :mod:`voiced.launchd.progress`: human output, the steps' own output
    included, goes to stderr, and that output is sent as ``log`` events too.
    SIGTERM ends the install with an ``error``, its child stopped."""
    from voiced.launchd.install import InstallError
    from voiced.launchd.progress import Progress, exit_on_sigterm

    progress = Progress(sys.stdout if args.json_progress else None)
    run: Runner = runner
    if args.json_progress:
        echo = functools.partial(print, file=sys.stderr)

        def tee(line: str) -> None:
            if progress.child_line(line):  # the download's byte counts
                return
            echo(line)
            progress.log(line)

        run = functools.partial(runner, on_line=tee)
    with exit_on_sigterm():  # from the first line on, SIGTERM still ends with error
        progress.hello()
        try:
            with progress.reporting():
                if is_root():
                    refusal = (
                        "Run `voiced install` as the user the services should run as, not "
                        "with sudo. It prints the one step that needs root."
                    )
                    echo(refusal)
                    progress.error(refusal)
                    return 2
                plan = _install(args, settings, echo, run, progress)
                _show_staged(plan, echo, dry_run=args.dry_run)
                progress.done(plan.staging_dir)  # last, after the human output
        except InstallError as exc:
            echo(f"install failed: {exc}")
            return 1
    return 0


def _show_staged(plan: InstallPlan, echo: Echo, *, dry_run: bool) -> None:
    if dry_run:
        for service in SERVICES:
            echo("")
            echo(f"--- {plan.staged_plist(service)} (would be written)")
            echo(plan.render(service).decode().rstrip())
        echo("")
        echo(f"--- {plan.install_script_path} (would be written)")
        echo(plan.sudo_install_script().rstrip())
        return
    echo("")
    echo("User-level install done. The LaunchDaemons need root. Review and run:")
    echo("")
    echo(f"  sudo bash {shlex.quote(str(plan.install_script_path))}")
    echo("")
    echo(plan.sudo_install_script().rstrip())
    echo("")
    echo(f"Then: {plan.entry} status")


def _install(
    args: argparse.Namespace, settings: Settings, echo: Echo, runner: Runner, progress: Progress
) -> InstallPlan:
    from voiced.launchd.install import plan_install, plan_summary, run_install

    plan = plan_install(
        settings,
        state_dir=args.state_dir,
        python=args.python,
        source=args.source,
        staging_dir=args.staging_dir,
        origin=args.origin,
    )
    # Piped (the app, a log), the build's and our own lines stay in order.
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]
    echo(plan_summary(plan))
    echo("")
    if args.dry_run:
        echo("dry run: nothing is written. Steps that would run:")
    run_install(
        plan,
        runner=runner,
        dry_run=args.dry_run,
        skip_models=args.skip_models,
        say=echo,
        progress=progress,
    )
    return plan


# start / stop -------------------------------------------------------------


def is_root() -> bool:
    return os.geteuid() == 0


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


# restart ------------------------------------------------------------------


def _via_control(
    settings: Settings,
    op: str,
    args: dict[str, Any] | None = None,
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> dict[str, Any] | None:
    """``op`` over the control socket, or None when nothing answers there
    (the caller falls back). An error answer, or a watchdog that stops
    answering mid-request, raises :class:`ControlError`."""
    try:
        return request(socket_path(settings), op, args, timeout_s)
    except ControlUnavailable:
        return None
    except (OSError, ValueError) as exc:
        raise ControlError("internal", f"watchdog did not answer: {exc}") from exc


def _in_process(op: str, args: dict[str, Any]) -> dict[str, Any]:
    """``op`` run here, against the config file, by the same code the
    watchdog runs. Errors are :class:`ControlError` as over the socket."""
    ops = Operations(NoSupervisor(), config_path())
    try:
        return asyncio.run(ops.dispatch(op, args))
    except OpError as exc:
        raise ControlError(exc.code, exc.message, dict(exc.extra)) from None


def _error_text(exc: ControlError) -> str:
    field = exc.data.get("field")
    return f"{field}: {exc.message}" if field else exc.message


def cmd_restart(service: str, settings: Settings, runner: Runner, echo: Echo) -> int:
    """Restart stt and tts, then, for ``all`` and ``watchdog``, the watchdog.

    The watchdog goes last: it serves the control socket the service
    restarts were asked through, and on SIGTERM it finishes the restarts it
    accepted before it exits. It is restarted even when a service restart
    failed, so that new code (after an upgrade) always reaches it.
    """
    ok = True
    if service != "watchdog":
        ok = _restart_services(service, settings, runner, echo)
    if service in ("all", "watchdog"):
        ok = restart_watchdog(settings, runner, echo) and ok
    return 0 if ok else 1


def _restart_services(service: str, settings: Settings, runner: Runner, echo: Echo) -> bool:
    """Through the watchdog when its control socket answers (it then knows
    about the restart and applies its startup grace); otherwise the same
    SIGKILL-and-kickstart directly."""
    try:
        result = _via_control(settings, "service.restart", {"service": service})
    except ControlError as exc:
        echo(f"restart failed: {exc.message}")
        return False
    if result is not None:
        for name, detail in (result.get("detail") or {}).items():
            echo(f"{name}: {detail}")
        echo("`voiced status` shows when the model is loaded again.")
        return True
    echo(f"no control socket at {socket_path(settings)}; restarting with launchctl directly")
    failures = 0
    for name in HTTP_SERVICES:
        if service not in (name, "all"):
            continue
        spec = ServiceSpec(name, settings.launchd.label(name), settings.http_url(name))
        outcome = restart_service(spec, runner)
        echo(f"{name}: {outcome.detail}")
        failures += 0 if outcome.done else 1
    return not failures


WATCHDOG_RESTART_TIMEOUT_S = 30.0
"""How long ``voiced restart`` waits for the old watchdog to exit and, again
counted from the SIGTERM, for the new one to answer on the control socket."""
WATCHDOG_POLL_S = 0.2


def control_hello(settings: Settings) -> dict[str, Any] | None:
    """The control socket's greeting, or None while nothing answers there."""
    try:
        with ControlClient.connect(socket_path(settings), timeout_s=2.0) as client:
            return client.hello
    except ControlUnavailable:
        return None


def restart_watchdog(settings: Settings, runner: Runner, echo: Echo) -> bool:
    """SIGTERM the watchdog, wait for it to exit, ``launchctl kickstart`` it
    and wait until the new process answers on the control socket.

    ``kickstart -k`` needs root while the job runs (ADR 0004), but the
    watchdog runs as this user, so the CLI may signal it. SIGTERM, not
    SIGKILL: the watchdog then closes the socket after finishing the restarts
    it accepted. KeepAlive would start it again after ``ThrottleInterval``;
    the plain kickstart does it at once.
    """
    label = settings.launchd.label("watchdog")
    status = print_service(label, runner)
    if not status.loaded:
        echo(f"watchdog: {label} is not loaded in launchd; nothing to restart")
        return False
    deadline = time.monotonic() + WATCHDOG_RESTART_TIMEOUT_S
    old = status.pid
    if old is not None:
        try:
            os.kill(old, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as exc:
            echo(f"watchdog: kill {old} failed: {exc}")
            return False
        while print_service(label, runner).pid == old:
            if time.monotonic() >= deadline:
                echo(f"watchdog: pid {old} did not exit within {WATCHDOG_RESTART_TIMEOUT_S:.0f}s")
                return False
            time.sleep(WATCHDOG_POLL_S)
    result = runner(kickstart_argv(label))
    if not result.ok:
        echo(f"watchdog: launchctl kickstart failed: {result.output}")
        return False
    while (hello := control_hello(settings)) is None:
        if time.monotonic() >= deadline:
            echo(
                "watchdog: restarted, but its control socket did not answer within "
                f"{WATCHDOG_RESTART_TIMEOUT_S:.0f}s; see `voiced logs watchdog`"
            )
            return False
        time.sleep(WATCHDOG_POLL_S)
    new = print_service(label, runner).pid
    before = f"pid {old} -> " if old is not None else ""
    version = hello.get("version", "?")
    echo(f"watchdog: restarted ({before}pid {new}), voiced {version} answering on the socket")
    return True


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


def control_status(settings: Settings) -> dict[str, Any] | None:
    """The watchdog's ``status`` over the control socket, or None without it.
    Raises :class:`ControlError` when the watchdog answers badly."""
    return _via_control(settings, "status", timeout_s=10.0)


def collect_status(
    settings: Settings,
    runner: Runner,
    poll: Callable[[str], Observation] | None = None,
    control: dict[str, Any] | None = None,
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
        for http_service in HTTP_SERVICES:
            if http_service == service:
                obs = poll(f"{settings.http_url(http_service)}/health")
                row["health"] = obs.kind
                row["health_detail"] = _health_detail(obs)
                row["health_payload"] = obs.payload
        if control is not None:
            row["control"] = (control.get("services") or {}).get(service)
        rows.append(row)
    return rows


def _uptime(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def cmd_status(
    settings: Settings,
    runner: Runner,
    echo: Echo,
    *,
    as_json: bool = False,
    control: Callable[[Settings], dict[str, Any] | None] = control_status,
) -> int:
    problem = ""
    try:
        via_socket = control(settings)
    except ControlError as exc:
        via_socket, problem = None, exc.message
    rows = collect_status(settings, runner, control=via_socket)
    if as_json:
        echo(json.dumps(rows, indent=2))
    else:
        headers: tuple[str, ...] = ("SERVICE", "LAUNCHD", "HEALTH", "DETAIL")
        table: list[tuple[str, ...]] = []
        for r in rows:
            cells: tuple[str, ...] = (
                r["service"],
                r["launchd"] + (f" (pid {r['pid']})" if r["pid"] else ""),
                str(r.get("health", "-")),
                str(r.get("health_detail", "")),
            )
            if via_socket is not None:
                c = r.get("control") or {}
                cells += (
                    str(c.get("state", "-")),
                    _uptime(c.get("uptime_s")),
                    str(c.get("restarts", "-")),
                )
            table.append(cells)
        if via_socket is not None:
            headers += ("STATE", "UPTIME", "RESTARTS")
        echo(render_table(headers, table))
        path = socket_path(settings)
        watchdog_pid = next((r["pid"] for r in rows if r["service"] == "watchdog"), None)
        if via_socket is not None:
            echo(f"control socket: {path}")
        elif watchdog_pid:
            echo(f"control socket: {NOT_SERVED}" + (f" ({problem})" if problem else ""))
        else:
            echo(f"control socket: not answering at {path} (watchdog not running)")
        if not any(r.get("installed") for r in rows):
            echo(_not_installed_hint(settings))
    healthy = all(r.get("health", "ok") == "ok" and r["pid"] for r in rows)
    return 0 if healthy else 1


def _not_installed_hint(settings: Settings) -> str:
    from voiced.launchd.install import plan_install

    script = plan_install(settings).install_script_path
    if script.is_file():
        return (
            f"The daemons are not installed. Review and run: sudo bash {shlex.quote(str(script))}"
        )
    return "The daemons are not installed. Run `voiced install` first."


# settings -----------------------------------------------------------------

_TRUE = ("true", "yes", "on", "1")
_FALSE = ("false", "no", "off", "0")


def parse_setting(pair: str, stdin: Callable[[], str] = sys.stdin.readline) -> tuple[str, object]:
    """``KEY=VALUE`` from the command line to a typed value for ``settings.set``."""
    key, sep, raw = pair.partition("=")
    key = key.strip()
    if not sep:
        raise ValueError(f"{pair!r}: expected KEY=VALUE")
    if key not in SETTABLE:
        raise ValueError(f"{key!r} is not settable; settable: {', '.join(SETTABLE)}")
    if key in ("zeroconf", "browser_settings"):
        if raw.lower() in _TRUE:
            return key, True
        if raw.lower() in _FALSE:
            return key, False
        raise ValueError(f"{key}: expected true or false, got {raw!r}")
    if key == "tts.speed":
        try:
            return key, float(raw)
        except ValueError:
            raise ValueError(f"{key}: expected a number, got {raw!r}") from None
    if key == "api_key" and raw == "-":
        return key, stdin().rstrip("\r\n")
    return key, raw


def _flatten(value: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for key, item in value.items():
        name = f"{prefix}{key}"
        if isinstance(item, dict):
            rows.extend(_flatten(item, f"{name}."))
        elif isinstance(item, bool):
            rows.append((name, "true" if item else "false"))
        else:
            rows.append((name, str(item)))
    return rows


_WARNINGS = {
    "lan_without_api_key": "bind is lan and no api_key is set: the HTTP API is open to your "
    "network. Set one with `voiced settings set api_key=-`.",
}


# A settable name and the settings.get row that shows its value.
_SHOWN_AS = {"api_key": "api_key_set"}


def _echo_settings(result: dict[str, Any], echo: Echo, *, as_json: bool) -> None:
    """The settings table; after a change, only the changed rows."""
    if as_json:
        echo(json.dumps(result, indent=2))
        return
    result = dict(result)
    changed = result.pop("changed", None)
    restart = result.pop("restart_required", None)
    warnings = result.pop("warnings", None) or []
    rows = _flatten(result)
    if changed is None:
        echo(render_table(("SETTING", "VALUE"), rows))
    elif not changed:
        echo("Nothing changed.")
    else:
        shown = {_SHOWN_AS.get(name, name) for name in changed}
        echo(render_table(("SETTING", "VALUE"), [r for r in rows if r[0] in shown]))
    if restart:
        target = restart[0] if len(restart) == 1 else "all"
        echo(f"Restart to apply: voiced restart {target}")
    for warning in warnings:
        echo(f"warning: {_WARNINGS.get(warning, warning)}")


def cmd_settings(
    args: argparse.Namespace, settings: Settings, echo: Echo, stdin: Callable[[], str] | None = None
) -> int:
    """Through the control socket when it answers (the watchdog then tells
    its other clients); otherwise here, by the same operations."""
    changes: dict[str, object] = {}
    if args.settings_command == "set":
        try:
            for pair in args.pairs:
                key, value = parse_setting(pair, stdin or sys.stdin.readline)
                changes[key] = value
        except ValueError as exc:
            echo(str(exc))
            return 2
    op, op_args = ("settings.set", changes) if changes else ("settings.get", {})
    try:
        result = _via_control(settings, op, op_args)
        if result is None:
            result = _in_process(op, op_args)
    except ControlError as exc:
        echo(_error_text(exc))
        return 1
    _echo_settings(result, echo, as_json=args.json)
    return 0


# logs ---------------------------------------------------------------------


def log_files(settings: Settings, service: str, *, stderr: bool = False) -> list[Path]:
    files: list[Path] = []
    for name in _services(service):
        paths = log_paths(settings.server.log_dir, name)
        files.append(paths.app)
        if stderr:
            files.append(paths.stderr)
    return files


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
    from voiced.launchd.install import plan_install, remove_targets, uninstall_targets

    plan = plan_install(settings, state_dir=args.state_dir)
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

    def targets() -> UninstallTargets:
        return uninstall_targets(
            plan,
            settings,
            logs=args.remove_logs,
            models=args.remove_models,
            config=args.remove_config,
        )

    if not args.yes:
        found = targets()
        if found.refused is not None:
            echo(f"Leaving {plan.state_dir} alone: {found.refused}.")
        present = [t for t in found.all() if t.exists() or t.is_symlink()]
        if not present:
            echo("nothing to remove")
            return 0
        echo("Would remove (pass --yes to do it):")
        for t in present:
            echo(f"  {t}")
        return 0
    # Not while an install (the app's or another terminal's) is running.
    waiting = lambda: echo(f"waiting for an install in {plan.state_dir}")  # noqa: E731
    held = (
        install_lock(plan.state_dir, waiting)
        if plan.state_dir.is_dir()
        else contextlib.nullcontext()
    )
    with held:
        found = targets()
        if remove_targets(found, plan, echo) == 0:
            echo("nothing to remove")
    if found.manifest is not None and found.manifest.source == "homebrew":
        echo(
            f"voiced itself ({found.manifest.environment}) is Homebrew's; "
            "remove it with `brew uninstall voiced`."
        )
    return 0


# doctor -------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace, settings: Settings, runner: Runner, echo: Echo) -> int:
    from voiced import doctor
    from voiced.launchd.install import plan_install

    if args.metal_probe:
        from voiced.layout import environment_python

        plan = plan_install(settings)
        probe = plan.metal_probe(environment_python(plan.entry))
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
    return [
        ServiceSpec(
            name="stt",
            label=settings.launchd.label("stt"),
            base_url=settings.http_url("stt"),
            api_key=settings.server.api_key,
            probe=default_probe("stt", settings.stt.model),
            engine=settings.stt.engine,
            model=settings.stt.model,
        ),
        ServiceSpec(
            name="tts",
            label=settings.launchd.label("tts"),
            base_url=settings.http_url("tts"),
            api_key=settings.server.api_key,
            probe=default_probe("tts", settings.tts.model, settings.tts.default_voice),
            engine=settings.tts.engine,
            model=settings.tts.model,
        ),
    ]


def spec_from_file(spec: ServiceSpec) -> ServiceSpec:
    """``spec`` as the config file says now (the watchdog's ``respec``)."""
    fresh = {s.name: s for s in watchdog_specs(load_settings(config_path()))}
    return fresh.get(spec.name, spec)


def cmd_watchdog(settings: Settings, echo: Echo, *, once: bool = False) -> int:
    log_file = None if once else log_paths(settings.server.log_dir, "watchdog").app
    configure_watchdog_logging(settings.server.log_level, log_file)
    dog = Watchdog(
        services=watchdog_specs(settings),
        policy=settings.watchdog,
        respec=spec_from_file,
        environment=None if once else RunningEnvironment.current(entry_point(state_dir(settings))),
    )
    if once:
        for name, decision in dog.tick().items():
            echo(f"{name}: {decision.action}: {decision.reason}")
        return 0
    return asyncio.run(_run_watchdog(dog, settings))


async def _run_watchdog(dog: Watchdog, settings: Settings) -> int:
    """The watchdog loop plus the control socket, until SIGTERM or SIGINT.

    A control socket that cannot start (another watchdog holds it, the state
    directory is not ours) is logged; the watchdog runs without it.
    """
    from voiced.control.server import ControlServer
    from voiced.control.supervisor import WatchdogSupervisor

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    entry = entry_point(state_dir(settings))
    ops = Operations(
        WatchdogSupervisor(dog), config_path(), engines=lambda: engines_in_subprocess(entry=entry)
    )
    server = ControlServer(
        socket_path(settings),
        ops,
        version=__version__,
        install=lambda: installed_manifest(state_dir(settings)),
    )
    try:
        await server.start()
        serving = True
    except (OSError, RuntimeError) as exc:
        logging.getLogger("voiced.control").error("control socket not started: %s", exc)
        print(f"voiced watchdog: control socket not started: {exc}", file=sys.stderr)
        serving = False
    try:
        replaced = await dog.run_async(stop)
    finally:
        if serving:
            await server.close()
    if replaced is not None:
        print(f"voiced watchdog: environment replaced ({replaced}); exiting", file=sys.stderr)
    return 0
