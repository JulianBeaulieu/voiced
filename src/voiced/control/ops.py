"""The control operations, independent of the transport.

:class:`Operations` turns ``(op, args)`` into a result dict or an
:class:`~voiced.control.protocol.OpError`. The settings rules live in
``voiced.settings_store``; this module only dispatches and maps their errors
to protocol codes. Process control (status and restarts) goes through a
:class:`Supervisor`, which the watchdog provides and tests fake; ``tts.speak``
goes through ``speak`` (``voiced.control.speech``), likewise.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from voiced.config import HTTP_SERVICES, ServiceName, Settings, load_settings
from voiced.control.logs import tail_lines
from voiced.control.protocol import (
    BAD_REQUEST,
    INTERNAL,
    INVALID_SETTING,
    MODEL_NOT_DOWNLOADED,
    NOT_INSTALLED,
    SERVICE_UNAVAILABLE,
    UNKNOWN_OP,
    OpError,
)
from voiced.control.speech import MAX_TEXT_CHARS, SpeechRefused, SpeechUnavailable, no_speech
from voiced.layout import environment_python
from voiced.settings_store import (
    SETTABLE,
    ConfigFileError,
    FileStamp,
    InvalidSetting,
    ModelMissing,
    SettingsChange,
    apply_settings,
    change_view,
    file_stamp,
    settings_view,
)
from voiced.validate import is_int

log = logging.getLogger("voiced.control")

MAX_LOG_LINES = 1000
DEFAULT_LOG_LINES = 100
LOG_SERVICES = ("stt", "tts", "watchdog")
RESTART_TARGETS = ("stt", "tts", "all")
CATALOGUE_TIMEOUT_S = 8.0
"""The catalogue child process imports the engine plugins, not their weights
(a fraction of a second); below the CLI client's wait, so the client sees
this error rather than its own timeout."""
CATALOGUE_TTL_S = 60.0


class NotInstalled(RuntimeError):
    """The service has no launchd plist: there is nothing to restart."""


class ServiceUnavailable(RuntimeError):
    """The service is installed but cannot be restarted now (not loaded)."""


class CatalogueUnavailable(RuntimeError):
    """The engine catalogue could not be read."""


class Supervisor(Protocol):
    """What the control channel needs from the process supervisor.

    All three methods block (they run commands and HTTP requests) and are
    called from background threads.
    """

    def status(self) -> dict[str, Any]:
        """The ``status`` result: ``installed`` and per-service state."""
        ...

    def check_restart(self, service: ServiceName) -> None:
        """Raise :class:`NotInstalled` or :class:`ServiceUnavailable` if
        ``service`` cannot be restarted now. Quick: no lock, no waiting."""
        ...

    def restart(self, service: ServiceName) -> str:
        """Restart one service; returns a one-line description of what was
        done. May wait for the supervisor's current health poll."""
        ...


class NoSupervisor:
    """For running the settings operations without a watchdog (the CLI
    fallback): nothing is supervised."""

    def status(self) -> dict[str, Any]:
        raise OpError(SERVICE_UNAVAILABLE, "the watchdog is not running")

    def check_restart(self, service: ServiceName) -> None:
        raise ServiceUnavailable("the watchdog is not running")

    def restart(self, service: ServiceName) -> str:
        raise ServiceUnavailable("the watchdog is not running")


def engines_in_subprocess(
    timeout_s: float = CATALOGUE_TIMEOUT_S, entry: Path | None = None
) -> dict[str, Any]:
    """``engines.list`` from ``python -m voiced.engines.catalogue``.

    Engine plugins are third-party code that imports numpy and more; running
    them in a child process keeps them out of the watchdog, which must not be
    wedged by them (ADR 0012, ADR 0013). With ``entry`` (the state
    directory's ``bin/voiced``) the child runs that environment's
    interpreter, looked up on every call: the watchdog's own may have been
    deleted by an upgrade (ADR 0017).
    """
    python = sys.executable if entry is None else str(environment_python(entry))
    # -P: `-m` would otherwise put the working directory (the user's home,
    # for the daemon) first on sys.path, ahead of the standard library.
    argv = [python, "-P", "-m", "voiced.engines.catalogue"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired:
        raise CatalogueUnavailable(f"engine catalogue took longer than {timeout_s:g}s") from None
    except OSError as exc:
        raise CatalogueUnavailable(f"engine catalogue: {exc}") from exc
    if proc.returncode != 0:
        lines = proc.stderr.strip().splitlines()
        reason = lines[-1] if lines else f"exit code {proc.returncode}"
        raise CatalogueUnavailable(f"engine catalogue failed: {reason}")
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        raise CatalogueUnavailable("engine catalogue printed no JSON") from None
    if not isinstance(data, dict):
        raise CatalogueUnavailable("engine catalogue printed no JSON object")
    return data


# Dispatch ------------------------------------------------------------------


def _int_arg(args: Mapping[str, Any], name: str, default: int) -> int:
    value = args.get(name, default)
    if not is_int(value):
        raise OpError(BAD_REQUEST, f'"{name}" must be an integer')
    return value


def _text_arg(args: Mapping[str, Any], name: str, limit: int) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise OpError(BAD_REQUEST, f'"{name}" must be a non-empty string')
    if len(value) > limit:
        raise OpError(BAD_REQUEST, f'"{name}" must be at most {limit} characters')
    return value


def _choice(args: Mapping[str, Any], name: str, choices: tuple[str, ...]) -> str:
    value = args.get(name)
    if value not in choices:
        raise OpError(BAD_REQUEST, f'"{name}" must be one of {", ".join(choices)}')
    assert isinstance(value, str)
    return value


class Operations:
    """The operations of the control protocol.

    ``speak`` answers ``tts.speak`` with a WAV file
    (:class:`voiced.control.speech.Speech`).
    ``load`` reads the effective settings (file plus environment) each time,
    so a change made by hand to the config file shows up without a restart.
    ``engines`` supplies the engine catalogue; it is cached for
    :data:`CATALOGUE_TTL_S`.
    """

    def __init__(
        self,
        supervisor: Supervisor,
        config_file: Path,
        load: Callable[[Path], Settings] = load_settings,
        engines: Callable[[], dict[str, Any]] = engines_in_subprocess,
        clock: Callable[[], float] = time.monotonic,
        speak: Callable[[str, str], Awaitable[bytes]] = no_speech,
    ) -> None:
        self.supervisor = supervisor
        self.speak = speak
        self.config_file = config_file
        self.load = load
        self._engines_source = engines
        self._engines_cache: tuple[float, dict[str, Any]] | None = None
        self._clock = clock
        # Two clients setting at once must not both read the old file.
        self._write_lock = threading.Lock()
        self._engines_lock = threading.Lock()
        self._restarts: set[asyncio.Task[None]] = set()
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "status": self.op_status,
            "settings.get": self.op_settings_get,
            "settings.set": self.op_settings_set,
            "engines.list": self.op_engines_list,
            "service.restart": self.op_service_restart,
            "logs.tail": self.op_logs_tail,
            "tts.speak": self.op_tts_speak,
            "open": self.op_open,
        }

    async def dispatch(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        handler = self._handlers.get(op)
        if handler is None:
            raise OpError(UNKNOWN_OP, f"unknown op {op!r}")
        try:
            result: dict[str, Any] = await handler(args)
        except OpError:
            raise
        except Exception as exc:
            log.exception("control op %s failed", op)
            raise OpError(INTERNAL, f"{type(exc).__name__}: {exc}") from None
        return result

    def settings(self) -> Settings:
        """The effective settings, or ``internal`` naming the config file."""
        try:
            return self.load(self.config_file)
        except (OSError, ValueError) as exc:  # TOMLDecodeError, ValidationError
            raise OpError(INTERNAL, f"config error at {self.config_file}: {exc}") from None

    def engines(self) -> dict[str, Any]:
        """The engine catalogue (blocking, cached)."""
        with self._engines_lock:
            now = self._clock()
            if self._engines_cache is not None and now - self._engines_cache[0] < CATALOGUE_TTL_S:
                return self._engines_cache[1]
            try:
                data = self._engines_source()
            except CatalogueUnavailable as exc:
                raise OpError(INTERNAL, str(exc)) from None
            self._engines_cache = (now, data)
            return data

    async def op_status(self, args: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self.supervisor.status)

    async def op_settings_get(self, args: dict[str, Any]) -> dict[str, Any]:
        settings = await asyncio.to_thread(self.settings)
        return settings_view(settings, self.config_file)

    async def op_settings_set(self, args: dict[str, Any]) -> dict[str, Any]:
        if not args:
            raise OpError(BAD_REQUEST, f"nothing to set; settable: {', '.join(SETTABLE)}")
        change = await asyncio.to_thread(self._change, args)
        return change_view(change, self.config_file)

    def _change(self, args: dict[str, Any]) -> SettingsChange:
        with self._write_lock:
            try:
                return apply_settings(args, self.config_file, engines=self.engines)
            except ModelMissing as exc:
                raise OpError(
                    MODEL_NOT_DOWNLOADED, exc.message, field=exc.field, model=exc.model
                ) from None
            except InvalidSetting as exc:
                raise OpError(INVALID_SETTING, exc.message, field=exc.field) from None
            except ConfigFileError as exc:
                raise OpError(INTERNAL, str(exc)) from None

    async def op_engines_list(self, args: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self.engines)

    async def op_service_restart(self, args: dict[str, Any]) -> dict[str, Any]:
        """Accept at once and restart in the background; completion shows in
        ``status`` events. ``all`` tries both services and is accepted if
        either can restart; ``detail`` has the outcome per service."""
        target = _choice(args, "service", RESTART_TARGETS)
        names = [s for s in HTTP_SERVICES if target in (s, "all")]
        detail: dict[str, str] = {}
        refused: dict[ServiceName, OpError] = {}
        for name in names:
            try:
                await asyncio.to_thread(self.supervisor.check_restart, name)
            except NotInstalled as exc:
                refused[name] = OpError(NOT_INSTALLED, f"{name}: {exc}")
            except (ServiceUnavailable, OSError) as exc:
                refused[name] = OpError(SERVICE_UNAVAILABLE, f"{name}: {exc}")
            else:
                detail[name] = "restarting"
        if not detail:
            codes = {e.code for e in refused.values()}
            code = NOT_INSTALLED if codes == {NOT_INSTALLED} else SERVICE_UNAVAILABLE
            raise OpError(code, "; ".join(e.message for e in refused.values()))
        for name, err in refused.items():
            detail[name] = f"{err.code}: {err.message}"
        for name in names:
            if detail[name] == "restarting":
                task = asyncio.create_task(asyncio.to_thread(self._restart, name))
                self._restarts.add(task)
                task.add_done_callback(self._restarts.discard)
        return {"accepted": True, "detail": {n: detail[n] for n in names}}

    def _restart(self, name: ServiceName) -> None:
        try:
            log.info("%s: %s", name, self.supervisor.restart(name))
        except ServiceUnavailable as exc:
            # Expected: it became unavailable after check_restart (Homebrew
            # took its lock in between, say).
            log.warning("%s: requested restart not done: %s", name, exc)
        except Exception:
            log.exception("%s: requested restart failed", name)

    async def wait_restarts(self) -> None:
        """Wait for the restarts accepted so far (tests and shutdown)."""
        if self._restarts:
            await asyncio.gather(*self._restarts, return_exceptions=True)

    async def op_logs_tail(self, args: dict[str, Any]) -> dict[str, Any]:
        from voiced.launchd.plist import log_paths

        service = _choice(args, "service", LOG_SERVICES)
        lines = _int_arg(args, "lines", DEFAULT_LOG_LINES)
        if not 1 <= lines <= MAX_LOG_LINES:
            raise OpError(BAD_REQUEST, f'"lines" must be between 1 and {MAX_LOG_LINES}')
        settings = await asyncio.to_thread(self.settings)
        path = log_paths(settings.server.log_dir, service).app
        return {"lines": await asyncio.to_thread(tail_lines, path, lines), "path": str(path)}

    async def op_open(self, args: dict[str, Any]) -> dict[str, Any]:
        what = _choice(args, "what", ("status_page", "logs_dir", "config_file"))
        settings = await asyncio.to_thread(self.settings)
        if what == "status_page":
            service = args.get("service", "tts")
            if service not in HTTP_SERVICES:
                raise OpError(BAD_REQUEST, '"service" must be stt or tts')
            return {"target": f"{settings.http_url(service)}/"}
        if what == "logs_dir":
            return {"target": str(settings.server.log_dir)}
        return {"target": str(self.config_file)}

    async def op_tts_speak(self, args: dict[str, Any]) -> dict[str, Any]:
        """One short line in a voice, from the running tts service at its
        default speed, so a client can hear the voice before choosing it.
        The server answers it in its own task (``CONCURRENT_OPS``)."""
        text = _text_arg(args, "text", MAX_TEXT_CHARS)
        voice = _text_arg(args, "voice", MAX_TEXT_CHARS)
        try:
            audio = await self.speak(text, voice)
        except SpeechUnavailable as exc:
            raise OpError(SERVICE_UNAVAILABLE, str(exc)) from None
        except SpeechRefused as exc:
            raise OpError(BAD_REQUEST, str(exc)) from None
        return {"format": "wav", "audio": base64.b64encode(audio).decode("ascii")}

    def config_stamp(self) -> FileStamp | None:
        return file_stamp(self.config_file)
