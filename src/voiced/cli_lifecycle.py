"""Lifecycle subcommands: install, start, stop, restart, status, settings, logs,
uninstall, doctor, watchdog.

Parsing and printing live here; file writes and launchd details live in
``voiced.launchd``. The services are LaunchAgents in the user's own domain
(ADR 0035), so nothing here needs root; the one exception is printed, never
run: the line that removes LaunchDaemons an earlier release installed.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import json
import logging
import os
import pwd
import shutil
import signal
import sys
import time
from collections.abc import Callable
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
from voiced.health_status import HEALTHY
from voiced.launchd import parked
from voiced.launchd.control import (
    Runner,
    StreamingRunner,
    bootout_argv,
    bootstrap_argv,
    kickstart_argv,
    legacy_daemons,
    legacy_message,
    plist_path,
    print_service,
    run_command,
    run_streaming,
    wait_unloaded,
)
from voiced.launchd.plist import log_paths
from voiced.layout import (
    ORIGINS,
    RunningEnvironment,
    entry_point,
    install_lock,
    installed_manifest,
    off_marker,
)
from voiced.settings_store import (
    INSTALL_SETTABLE,
    SETTABLE,
    parse_setting,
)
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
        "install", help="link the environment, download models, load the LaunchAgents"
    )
    install.add_argument("--dry-run", action="store_true", help="show the plan, change nothing")
    install.add_argument(
        "--state-dir",
        "--venv",
        dest="state_dir",
        type=Path,
        help="state directory (default: [install] venv_dir; set that key too so doctor, "
        "uninstall and the services agree)",
    )
    install.add_argument(
        "--source",
        type=Path,
        help="checkout to build a new environment from, with uv and the shipped lock "
        "(default: the environment this voiced runs from)",
    )
    install.add_argument("--python", help='Python version for a --source build, e.g. "3.12"')
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
    install.add_argument(
        "--prepare",
        action="store_true",
        help="only build and check the environment and download the models, changing nothing "
        "that runs (the app's first half of a move from LaunchDaemons, ADR 0035)",
    )
    install.add_argument(
        "--set",
        dest="pairs",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="run with a setting of stt.enabled, stt.engine, stt.model or the same for tts "
        "(the app's first-run choices): the models step downloads for it and the config "
        "gets it once the models are in, as `voiced settings set` writes; repeatable",
    )

    start = sub.add_parser("start", help="start the services")
    start.add_argument("service", nargs="?", choices=SERVICE_CHOICES, default="all")

    stop = sub.add_parser("stop", help="stop the services until the next login or start")
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
        "uninstall", help="stop the services and remove what voiced installed"
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
        "--remove-config", action="store_true", help="also delete the config file"
    )
    uninstall.add_argument("--remove-logs", action="store_true", help="also delete the log dir")
    uninstall.add_argument(
        "--remove-models",
        action="store_true",
        help="also delete the models voiced pinned from the Hugging Face cache",
    )

    doc = sub.add_parser("doctor", help="check this machine")
    doc.add_argument("--json", action="store_true", help="machine-readable output")
    doc.add_argument(
        "--metal-probe",
        action="store_true",
        help="run a one-shot LaunchAgent that tests Metal as the services run",
    )

    wd = sub.add_parser("watchdog", help="run the health watchdog in the foreground")
    wd.add_argument("--once", action="store_true", help="poll once, print decisions, exit")


def run_lifecycle(
    args: argparse.Namespace, settings: Settings, runner: Runner = run_command, echo: Echo = print
) -> int:
    handlers: dict[str, Callable[[], int]] = {
        "install": lambda: cmd_install(args, settings, echo, query=runner),
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
    if args.command in AS_THE_USER and args.command != "install" and is_root():
        # launchctl would address root's own domain, gui/0: `sudo voiced stop`
        # would find nothing loaded and leave the user's services running.
        echo(
            f"Run `voiced {args.command}` as the user the services run as, not with sudo: "
            "they are that user's LaunchAgents."
        )
        return 2
    return handlers[args.command]()


AS_THE_USER = frozenset(
    {"install", "start", "stop", "restart", "status", "settings", "uninstall", "doctor"}
)
"""The commands that talk to launchd or to the watchdog about the user's
agents (``settings`` would write the user's config as root). ``install``
refuses root itself, inside its progress stream."""


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
    query: Runner = run_command,
) -> int:
    """``runner`` runs the long steps, ``query`` the short ones (the engine
    check, launchctl). With ``--json-progress``, stdout carries only the events of
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
                        "with sudo: they are that user's LaunchAgents."
                    )
                    echo(refusal)
                    progress.error(refusal)
                    return 2
                plan = _install(args, settings, echo, run, query, progress)
                echo("")
                if args.dry_run:
                    echo("dry run done; nothing was written.")
                else:
                    echo(
                        f"Installed. The services run while you are logged in: {plan.entry} status"
                    )
                progress.done()  # last, after the human output
        except InstallError as exc:
            echo(f"install failed: {exc}")
            return 1
    return 0


def _install(
    args: argparse.Namespace,
    settings: Settings,
    echo: Echo,
    runner: Runner,
    query: Runner,
    progress: Progress,
) -> InstallPlan:
    from voiced.launchd.install import InstallError, plan_install, plan_summary, run_install

    changes: dict[str, object] = {}
    for pair in args.pairs:
        # The key first, so no other setting is parsed (api_key=- reads stdin).
        if (key := pair.partition("=")[0].strip()) not in INSTALL_SETTABLE:
            raise InstallError(
                f"--set {key}: install sets only {', '.join(INSTALL_SETTABLE)}; "
                "use voiced settings set for the rest"
            )
        try:
            changes.update([parse_setting(pair)])
        except ValueError as exc:
            raise InstallError(f"--set {exc}") from None
    for kind in HTTP_SERVICES:
        # The models step downloads the model named, so a switch names it.
        if f"{kind}.engine" in changes and f"{kind}.model" not in changes:
            raise InstallError(f"--set {kind}.engine needs {kind}.model in the same install")
    plan = plan_install(
        settings,
        state_dir=args.state_dir,
        python=args.python,
        source=args.source,
        origin=args.origin,
        changes=changes,
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
        query=query,
        dry_run=args.dry_run,
        skip_models=args.skip_models,
        prepare=args.prepare,
        say=echo,
        progress=progress,
    )
    return plan


# start / stop -------------------------------------------------------------


def is_root() -> bool:
    return os.geteuid() == 0


def legacy_note(settings: Settings, runner: Runner, echo: Echo) -> bool:
    """Say so, and how to move on, when LaunchDaemons of 0.4.0 or earlier
    are still there: these commands address the LaunchAgents only
    (ADR 0035). True when there are some."""
    found = legacy_daemons(settings.launchd.service_labels(), runner)
    if found:
        echo(legacy_message(found, pwd.getpwuid(os.getuid()).pw_name))
    return bool(found)


def cmd_start(service: str, settings: Settings, runner: Runner, echo: Echo) -> int:
    if legacy_note(settings, runner, echo):
        return 1
    failures = 0
    for name in _services(service):
        if name in HTTP_SERVICES and not settings.service(name).enabled:
            # Started as any other: its process parks (voiced.launchd.parked,
            # ADR 0033), and turning it on is a restart.
            echo(f"{name}: {parked.off_detail(name)}")
        label = settings.launchd.label(name)
        plist = plist_path(label)
        if print_service(label, runner).loaded:
            result, done = runner(kickstart_argv(label)), f"kickstarted {label}"
        elif plist.is_file():
            result, done = runner(bootstrap_argv(plist)), f"loaded {plist}"
        else:
            failures += 1
            echo(f"{name}: {plist} is not installed; run voiced install")
            continue
        if result.ok:
            echo(f"{name}: {done}")
        else:
            failures += 1
            echo(f"{name}: launchctl failed: {result.output}")
    return 1 if failures else 0


def cmd_stop(service: str, settings: Settings, runner: Runner, echo: Echo) -> int:
    """Boot out: KeepAlive would restart a killed process. The agent loads
    again at the next login or with `voiced start`."""
    if legacy_note(settings, runner, echo):
        return 1
    failures = 0
    for name in _services(service):
        label = settings.launchd.label(name)
        if not print_service(label, runner).loaded:
            echo(f"{name}: not loaded")
            continue
        result = runner(bootout_argv(label))
        if not result.ok:
            failures += 1
            echo(f"{name}: launchctl bootout failed: {result.output}")
        elif not wait_unloaded(label, runner):
            failures += 1
            echo(f"{name}: still unloading; try again in a moment")
        else:
            echo(f"{name}: stopped until the next login or `voiced start`")
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

    SIGTERM, not ``kickstart -k``'s SIGKILL: the watchdog then closes the
    socket after finishing the restarts it accepted. KeepAlive would start
    it again after ``ThrottleInterval``; the plain kickstart does it at once.
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
                if parked.is_parked(off_marker(state_dir(settings), http_service), ls.pid):
                    row["health"] = "off"
                    row["health_detail"] = parked.off_detail(
                        http_service, enabled_now=settings.service(http_service).enabled
                    )
                    row["health_payload"] = None
                    continue
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


FINE = HEALTHY | {"off"}
"""Health that ``voiced status`` reports as fine: serving, a model unloaded
while idle or loading again, or switched off on purpose (ADR 0033)."""


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
        if not legacy_note(settings, runner, echo) and not any(r.get("installed") for r in rows):
            echo("The services are not installed. Run `voiced install`.")
    healthy = all(r.get("health", "ok") in FINE and r["pid"] for r in rows)
    return 0 if healthy else 1


# settings -----------------------------------------------------------------


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
    from voiced.launchd.install import (
        InstallError,
        plan_install,
        remove_agents,
        remove_targets,
        revision_line,
        uninstall_targets,
    )
    from voiced.models import size_text

    plan = plan_install(settings, state_dir=args.state_dir)
    if legacy_note(settings, runner, echo):
        echo("Then `voiced uninstall` removes the rest.")
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
        models = found.models
        for note in models.notes() if models is not None else []:
            echo(note)
        revisions = models.revisions if models is not None else []
        agents = [
            f"{plan.agent_path(s)} ({plan.labels[s]}, stopped first)"
            for s in SERVICES
            if plan.agent_path(s).is_file() or print_service(plan.labels[s], runner).loaded
        ]
        if not agents and not present and not revisions:
            echo("nothing to remove")
            return 0
        echo("Would remove (pass --yes to do it):")
        for t in [*agents, *present]:
            echo(f"  {t}")
        for r in revisions:
            echo(f"  {revision_line(r)}")
        if models is not None and revisions:
            echo(f"Removing those models frees {size_text(models.freed)} ({models.freed:,} bytes).")
        return 0
    # Not while an install (the app's or another terminal's) is running.
    waiting = lambda: echo(f"waiting for an install in {plan.state_dir}")  # noqa: E731
    held = (
        install_lock(plan.state_dir, waiting)
        if plan.state_dir.is_dir()
        else contextlib.nullcontext()
    )
    with held:
        try:
            stopped = remove_agents(plan, runner, echo)
        except InstallError as exc:
            echo(str(exc))
            return 1
        found = targets()
        if remove_targets(found, plan, echo) == 0 and not stopped:
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
        from voiced.launchd.install import InstallError, run_metal_probe
        from voiced.layout import environment_python

        plan = plan_install(settings)
        python = environment_python(plan.entry)
        echo(f"running {plan.labels['metalprobe']} once as a LaunchAgent ...")
        try:
            echo(run_metal_probe(plan, python, runner).rstrip())
        except InstallError as exc:
            echo(f"metal probe failed: {exc}")
            return 1
        echo("")
        echo("`voiced doctor` now reads this result.")
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
    """A service with ``idle_unload_s`` set gets no probe: each probe is a
    request, and would keep its model loaded (ADR 0033)."""
    specs = []
    for name in HTTP_SERVICES:
        section = settings.service(name)
        probe = default_probe(name, section.model, settings.tts.default_voice)
        specs.append(
            ServiceSpec(
                name=name,
                label=settings.launchd.label(name),
                base_url=settings.http_url(name),
                api_key=settings.server.api_key,
                probe=None if section.idle_unload_s else probe,
                engine=section.engine,
                model=section.model,
                off_marker=off_marker(state_dir(settings), name),
            )
        )
    return specs


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
    from voiced.control.speech import Speech
    from voiced.control.supervisor import WatchdogSupervisor

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    entry = entry_point(state_dir(settings))
    ops = Operations(
        WatchdogSupervisor(dog),
        config_path(),
        engines=lambda: engines_in_subprocess(entry=entry),
        speak=Speech(lambda: dog.spec("tts")),
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
