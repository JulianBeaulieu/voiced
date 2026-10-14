"""The control operations, independent of the transport.

:class:`Operations` turns ``(op, args)`` into a result dict or an
:class:`~voiced.control.protocol.OpError`. The socket server and the status
page's settings form both call into it, so the rules for reading and
changing settings live in one place. Process control (status and restarts)
goes through a :class:`Supervisor`, which the watchdog provides and tests
fake.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from voiced.config import (
    HTTP_SERVICES,
    SETTABLE,
    ConfigFileError,
    InvalidSetting,
    Settings,
    SettingsChange,
    STTConfig,
    TTSConfig,
    apply_settings,
    bind_mode,
)
from voiced.control.logs import tail_lines
from voiced.control.protocol import (
    BAD_REQUEST,
    INTERNAL,
    INVALID_SETTING,
    NOT_INSTALLED,
    SERVICE_UNAVAILABLE,
    UNKNOWN_OP,
    OpError,
)

log = logging.getLogger("voiced.control")

MAX_LOG_LINES = 1000
DEFAULT_LOG_LINES = 100
LOG_SERVICES = ("stt", "tts", "watchdog")
RESTART_TARGETS = ("stt", "tts", "all")


class NotInstalled(RuntimeError):
    """The service has no launchd job to restart."""


class Supervisor(Protocol):
    """What the control channel needs from the process supervisor.

    Both methods block (they run commands and HTTP requests) and are called
    from a background thread.
    """

    def status(self) -> dict[str, Any]:
        """The ``status`` result: ``installed`` and per-service state."""
        ...

    def restart(self, service: Literal["stt", "tts"]) -> str:
        """Restart one service now; returns a one-line description of what
        was done. Raises :class:`NotInstalled` if there is nothing to restart."""
        ...

    def settings_changed(self, settings: Settings) -> None:
        """The config file changed; refresh anything derived from it."""
        ...


# Settings ------------------------------------------------------------------


def settings_view(settings: Settings, config_file: Path) -> dict[str, Any]:
    """The ``settings.get`` result. Never contains the API key."""
    server = settings.server
    return {
        "bind": bind_mode(server.bind),
        "api_key_set": bool(server.api_key),
        "zeroconf": server.zeroconf,
        "stt": {
            "engine": settings.stt.engine,
            "model": settings.stt.model,
            "language": settings.stt.languages[0],
        },
        "tts": {
            "engine": settings.tts.engine,
            "model": settings.tts.model,
            "voice": settings.tts.default_voice,
            "speed": settings.tts.speed,
        },
        "browser_settings": server.browser_settings,
        "ports": {
            "stt_http": settings.stt.http_port,
            "tts_http": settings.tts.http_port,
            "stt_wyoming": settings.stt.wyoming_port,
            "tts_wyoming": settings.tts.wyoming_port,
        },
        "paths": {
            "config": str(config_file),
            "logs": str(server.log_dir),
            "state": str(settings.install.venv_dir),
        },
    }


def check_engines(sections: dict[str, BaseModel], changing: set[str]) -> None:
    """Refuse engines that are not installed and voices the engine lacks.

    Only the keys being changed are checked, so a hand-edited file with an
    engine this process cannot see does not block unrelated changes.
    """
    from voiced.engines import registry
    from voiced.engines.catalogue import catalogue_of

    stt = sections["stt"]
    tts = sections["tts"]
    assert isinstance(stt, STTConfig) and isinstance(tts, TTSConfig)
    if "stt.engine" in changing and stt.engine not in registry.list_stt_engines():
        installed = ", ".join(sorted(registry.list_stt_engines()))
        raise InvalidSetting("stt.engine", f"{stt.engine!r} is not installed ({installed})")
    tts_engines = registry.list_tts_engines()
    if "tts.engine" in changing and tts.engine not in tts_engines:
        installed = ", ".join(sorted(tts_engines))
        raise InvalidSetting("tts.engine", f"{tts.engine!r} is not installed ({installed})")
    if {"tts.engine", "tts.voice"} & changing and tts.engine in tts_engines:
        voices = catalogue_of(tts_engines[tts.engine]).voices
        if voices and tts.default_voice not in voices:
            hint = "" if "tts.voice" in changing else "; set tts.voice in the same change"
            raise InvalidSetting(
                "tts.voice", f"{tts.engine} has no voice {tts.default_voice!r}{hint}"
            )


def change_settings(changes: Mapping[str, object], config_file: Path) -> SettingsChange:
    """:func:`voiced.config.apply_settings` with the engine checks. Raises
    :class:`OpError`. Shared by the socket and the status page form."""
    try:
        return apply_settings(changes, config_file, check=check_engines)
    except InvalidSetting as exc:
        raise OpError(INVALID_SETTING, exc.message, field=exc.field) from None
    except ConfigFileError as exc:
        raise OpError(INTERNAL, str(exc)) from None


def change_result(change: SettingsChange, config_file: Path) -> dict[str, Any]:
    result = settings_view(change.settings, config_file)
    result["restart_required"] = list(change.restart_required)
    result["warnings"] = list(change.warnings)
    return result


def engines_list() -> dict[str, Any]:
    """The ``engines.list`` result from the registry, without loading weights."""
    from voiced.engines import registry
    from voiced.engines.catalogue import catalogue_of

    stt = []
    for name, cls in sorted(registry.list_stt_engines().items()):
        cat = catalogue_of(cls)
        stt.append({"name": name, "models": cat.models, "languages": cat.languages})
    tts = []
    for name, tts_cls in sorted(registry.list_tts_engines().items()):
        cat = catalogue_of(tts_cls)
        tts.append(
            {"name": name, "models": cat.models, "voices": cat.voices, "languages": cat.languages}
        )
    return {"stt": stt, "tts": tts}


# Dispatch ------------------------------------------------------------------


def _int_arg(args: Mapping[str, Any], name: str, default: int) -> int:
    value = args.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise OpError(BAD_REQUEST, f'"{name}" must be an integer')
    return value


def _choice(args: Mapping[str, Any], name: str, choices: tuple[str, ...]) -> str:
    value = args.get(name)
    if value not in choices:
        raise OpError(BAD_REQUEST, f'"{name}" must be one of {", ".join(choices)}')
    assert isinstance(value, str)
    return value


class Operations:
    """The operations of the control protocol.

    ``load`` reads the effective settings (file plus environment) each time,
    so a change made by hand to the config file shows up without a restart.
    """

    def __init__(
        self,
        supervisor: Supervisor,
        config_file: Path,
        load: Callable[[Path], Settings],
    ) -> None:
        self.supervisor = supervisor
        self.config_file = config_file
        self.load = load
        # Two clients setting at once must not both read the old file.
        self._write_lock = threading.Lock()
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "status": self.op_status,
            "settings.get": self.op_settings_get,
            "settings.set": self.op_settings_set,
            "engines.list": self.op_engines_list,
            "service.restart": self.op_service_restart,
            "logs.tail": self.op_logs_tail,
            "open": self.op_open,
        }

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._handlers)

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

    def _settings(self) -> Settings:
        try:
            return self.load(self.config_file)
        except Exception as exc:
            raise OpError(INTERNAL, f"cannot load {self.config_file}: {exc}") from None

    async def op_status(self, args: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self.supervisor.status)

    async def op_settings_get(self, args: dict[str, Any]) -> dict[str, Any]:
        settings = await asyncio.to_thread(self._settings)
        return settings_view(settings, self.config_file)

    async def op_settings_set(self, args: dict[str, Any]) -> dict[str, Any]:
        if not args:
            raise OpError(BAD_REQUEST, f"nothing to set; settable: {', '.join(SETTABLE)}")
        change = await asyncio.to_thread(self._change, args)
        if change.changed:
            await asyncio.to_thread(self.supervisor.settings_changed, change.settings)
        return change_result(change, self.config_file)

    def _change(self, args: dict[str, Any]) -> SettingsChange:
        with self._write_lock:
            return change_settings(args, self.config_file)

    async def op_engines_list(self, args: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(engines_list)

    async def op_service_restart(self, args: dict[str, Any]) -> dict[str, Any]:
        target = _choice(args, "service", RESTART_TARGETS)
        names = tuple(s for s in HTTP_SERVICES if target in (s, "all"))
        details: dict[str, str] = {}
        for name in names:
            try:
                details[name] = await asyncio.to_thread(self.supervisor.restart, name)
            except NotInstalled as exc:
                raise OpError(NOT_INSTALLED, str(exc)) from None
            except OSError as exc:
                raise OpError(SERVICE_UNAVAILABLE, f"{name}: {exc}") from None
        return {"accepted": True, "detail": details}

    async def op_logs_tail(self, args: dict[str, Any]) -> dict[str, Any]:
        from voiced.launchd.plist import log_paths

        service = _choice(args, "service", LOG_SERVICES)
        lines = _int_arg(args, "lines", DEFAULT_LOG_LINES)
        if not 1 <= lines <= MAX_LOG_LINES:
            raise OpError(BAD_REQUEST, f'"lines" must be between 1 and {MAX_LOG_LINES}')
        settings = await asyncio.to_thread(self._settings)
        path = log_paths(settings.server.log_dir, service).app
        return {"lines": await asyncio.to_thread(tail_lines, path, lines), "path": str(path)}

    async def op_open(self, args: dict[str, Any]) -> dict[str, Any]:
        what = _choice(args, "what", ("status_page", "logs_dir", "config_file"))
        settings = await asyncio.to_thread(self._settings)
        if what == "status_page":
            service = args.get("service", "tts")
            if service not in HTTP_SERVICES:
                raise OpError(BAD_REQUEST, '"service" must be stt or tts')
            return {"target": f"{settings.http_url(service)}/"}
        if what == "logs_dir":
            return {"target": str(settings.server.log_dir)}
        return {"target": str(self.config_file)}
