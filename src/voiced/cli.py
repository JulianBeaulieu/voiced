"""``voiced`` command line.

``serve`` and ``config`` live here; the lifecycle commands (install, start,
stop, restart, status, settings, logs, uninstall, doctor, watchdog) are in
``voiced.cli_lifecycle``.

    VOICED_TTS__ENGINE=fake voiced serve tts
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import resource
import socket
import sys
from collections.abc import Sequence
from typing import TYPE_CHECKING

from voiced import __version__
from voiced.cli_lifecycle import add_lifecycle_parsers, run_lifecycle
from voiced.config import Settings, config_path, load_settings
from voiced.launchd.stdio import redirect_stdio_from_env

if TYPE_CHECKING:
    import uvicorn
    from fastapi import FastAPI

log = logging.getLogger(__name__)

GRACEFUL_SHUTDOWN_S = 10
"""How long uvicorn waits for open connections on SIGTERM before closing them.

Below launchd's ExitTimeOut (about 20 s), so a stuck client cannot turn a
clean stop into a SIGKILL.
"""

PORT_IN_USE_EXIT = 3
"""``serve`` exit code when a configured port is already bound."""

LIFECYCLE = (
    "install",
    "start",
    "stop",
    "restart",
    "status",
    "settings",
    "logs",
    "uninstall",
    "doctor",
    "watchdog",
)


def uvicorn_config(app: FastAPI, settings: Settings, port: int = 0) -> uvicorn.Config:
    """The uvicorn configuration ``serve`` runs a service app under.

    Shared so the end-to-end tests drive the services with the same settings
    (graceful-shutdown timeout, no log hijacking) instead of copying them.
    ``serve`` passes its pre-bound socket to ``Server.run``, which then ignores
    ``host`` and ``port``; the tests use ``port=0`` for an ephemeral port.
    """
    import uvicorn

    return uvicorn.Config(
        app,
        host=settings.server.bind,
        port=port,
        log_config=None,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
    )


class PortInUse(OSError):
    """A port ``serve`` needs is already bound by another socket."""

    def __init__(self, label: str, bind: str, port: int) -> None:
        super().__init__(
            f"{label} port {port} on {bind} is already in use; is voiced already "
            "running, or is the port taken? Change it in the config."
        )


def _bind(bind: str, port: int) -> socket.socket:
    """Bind ``bind:port`` the way uvicorn's own ``bind_socket`` would (same
    family and ``SO_REUSEADDR``), so the answer matches what it would see."""
    family = socket.AF_INET6 if ":" in bind else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((bind, port))
    except OSError:
        sock.close()
        raise
    sock.set_inheritable(True)
    return sock


def bind_service_ports(settings: Settings, service: str) -> socket.socket:
    """Claim the service's ports before anything is loaded.

    Returns the bound HTTP socket, which ``serve`` hands to uvicorn: uvicorn
    binds its own socket only after the app's lifespan has started the engine
    load, and on a clash it shuts the lifespan down mid-load and exits 0. The
    Wyoming listener binds inside the lifespan, so its port is checked here and
    released; if it is taken in the moment between, the lifespan exits non-zero
    without waiting for the load (``voiced.api.app``). Raises :class:`PortInUse`.
    """
    section = settings.stt if service == "stt" else settings.tts
    bind = settings.server.bind
    try:
        _bind(bind, section.wyoming_port).close()
    except OSError as exc:
        raise PortInUse("Wyoming", bind, section.wyoming_port) from exc
    try:
        return _bind(bind, section.http_port)
    except OSError as exc:
        raise PortInUse("HTTP", bind, section.http_port) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="voiced",
        description="Local speech-to-text and text-to-speech server for Apple Silicon.",
    )
    parser.add_argument("--version", action="version", version=f"voiced {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    add_lifecycle_parsers(sub)

    serve = sub.add_parser("serve", help="run one service in the foreground")
    serve.add_argument("service", choices=("stt", "tts"))

    sub.add_parser("config", help="print the effective configuration as JSON")
    return parser


def cmd_serve(settings: Settings, service: str) -> int:
    import uvicorn

    try:
        http_socket = bind_service_ports(settings, service)
    except PortInUse as exc:
        print(f"voiced serve {service}: {exc}", file=sys.stderr)
        return PORT_IN_USE_EXIT
    with http_socket:
        app = build_service_app(settings, service)
        raise_open_file_limit()  # after logging is configured, before any connection
        request_foreground_role()
        uvicorn.Server(uvicorn_config(app, settings)).run(sockets=[http_socket])
    return 0


OPEN_FILES = 4096
"""Soft limit on open files a service raises itself to. launchd starts
daemons with 256, which every HTTP and Wyoming connection, the model files
and the logs share; uvicorn closes no connection that never sends a request,
so a client holding a couple of hundred of them could otherwise stop the
service accepting anything, ``/health`` included."""


def raise_open_file_limit(target: int = OPEN_FILES) -> None:
    """Raise the soft ``RLIMIT_NOFILE`` towards ``target``: never above the
    hard limit, never lower than it already is."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft == resource.RLIM_INFINITY or soft >= target:
        return
    want = target if hard == resource.RLIM_INFINITY else min(target, hard)
    if want <= soft:
        log.info("open file limit stays at %d: the hard limit is %d", soft, hard)
        return
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    except (ValueError, OSError) as exc:
        log.warning("could not raise the open file limit from %d: %s", soft, exc)
        return
    if want < target:
        log.info("open file limit raised from %d to %d, the hard limit", soft, want)
    else:
        log.info("open file limit raised from %d to %d", soft, want)


PRIO_DARWIN_ROLE = 6
"""``getpriority`` selector for the task role (XNU ``sys/resource.h``; not in
the public SDK header). It reads the role back; ``setpriority`` with it is
refused inside a launchd job (``ENOTSUP``), so the role is set with
``task_policy_set``."""

TASK_CATEGORY_POLICY = 1
TASK_FOREGROUND_APPLICATION = 1
TASK_CATEGORY_POLICY_COUNT = 1  # natural_t words in task_category_policy_data_t


def request_foreground_role() -> None:
    """Ask macOS to schedule this process as a foreground application.

    Under launchd the first burst of work after an idle gap ran much slower
    than from a terminal; this role recovers most of it (ADR 0015, Under
    launchd). A task may set it on itself without privileges. Any failure is
    logged and serving goes on.
    """
    if sys.platform != "darwin":
        return
    import ctypes

    try:
        libc = ctypes.CDLL(None)
        libc.mach_task_self.restype = ctypes.c_uint  # task_t
        libc.task_policy_set.argtypes = [
            ctypes.c_uint,  # task_t
            ctypes.c_uint,  # task_policy_flavor_t
            ctypes.c_void_p,  # task_policy_t
            ctypes.c_uint,  # mach_msg_type_number_t
        ]
    except (OSError, AttributeError) as exc:  # libSystem or a symbol missing
        log.warning("could not set the foreground task role: %s", exc)
        return
    role = ctypes.c_int(TASK_FOREGROUND_APPLICATION)
    kr = libc.task_policy_set(
        libc.mach_task_self(), TASK_CATEGORY_POLICY, ctypes.byref(role), TASK_CATEGORY_POLICY_COUNT
    )
    if kr != 0:
        log.warning("could not set the foreground task role: kern_return %d", kr)
        return
    try:
        read_back = os.getpriority(PRIO_DARWIN_ROLE, 0)
    except OSError as exc:
        log.info("task role set to foreground application; read back failed: %s", exc)
        return
    log.info("task role set to foreground application (darwin role read back: %d)", read_back)


def build_service_app(settings: Settings, service: str) -> FastAPI:
    """Configure logging and build the service's app, exactly as ``serve`` does."""
    from voiced.logging import configure_logging

    configure_logging(service, settings.server.log_level, settings.server.log_dir)
    if service == "stt":
        from voiced.stt import build_stt_app

        return build_stt_app(settings)
    from voiced.tts import build_tts_app

    return build_tts_app(settings)


def cmd_config(settings: Settings) -> int:
    payload = settings.model_dump(mode="json")
    payload["_config_file"] = str(config_path())
    print(json.dumps(payload, indent=2))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    # A no-op after voiced.entry; covers a console script from before it.
    redirect_stdio_from_env()
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings()
    except (OSError, ValueError) as exc:  # TOMLDecodeError, ValidationError
        print(f"config error at {config_path()}: {exc}", file=sys.stderr)
        return 2
    if args.command in LIFECYCLE:
        return run_lifecycle(args, settings)
    if args.command == "serve":
        return cmd_serve(settings, args.service)
    if args.command == "config":
        return cmd_config(settings)
    return 2  # pragma: no cover - argparse rejects unknown commands


if __name__ == "__main__":
    sys.exit(main())
