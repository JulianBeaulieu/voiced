"""Reading and changing the common settings in the config file.

The control channel, the CLI fallback and the status page's settings form all
change the config file through :func:`apply_settings`. It validates with the
section models of ``voiced.config``, edits the file with ``tomlkit`` (only
the changed values are rewritten; comments, key order and every other key
stay as they were) and replaces it atomically. ``voiced.config`` stays the
schema and the loader. See ADR 0013.

Errors are :class:`InvalidSetting` (with the field at fault) and
:class:`ConfigFileError`; each transport maps them to its own error shape.
"""

from __future__ import annotations

import ipaddress
import os
import tempfile
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import tomlkit
from pydantic import BaseModel, ValidationError

from voiced.config import (
    ENV_PREFIX,
    HTTP_SERVICES,
    ServerConfig,
    ServiceName,
    Settings,
    STTConfig,
    TTSConfig,
    config_path,
    load_settings,
)

BindMode = Literal["loopback", "lan"]

LAN_BIND = "0.0.0.0"
LOOPBACK_BIND = "127.0.0.1"

SETTABLE: dict[str, tuple[str, str]] = {
    "bind": ("server", "bind"),
    "api_key": ("server", "api_key"),
    "zeroconf": ("server", "zeroconf"),
    "browser_settings": ("server", "browser_settings"),
    "stt.engine": ("stt", "engine"),
    "stt.model": ("stt", "model"),
    "tts.engine": ("tts", "engine"),
    "tts.model": ("tts", "model"),
    "tts.voice": ("tts", "default_voice"),
    "tts.speed": ("tts", "speed"),
}
"""Settable name (as the control protocol spells it) -> (section, file key)."""

# Which services read a section at startup. browser_settings is read from the
# file by the status page, so changing it needs no restart.
_RESTART_BY_SECTION: dict[str, tuple[ServiceName, ...]] = {
    "server": HTTP_SERVICES,
    "stt": ("stt",),
    "tts": ("tts",),
}
_READ_LIVE = {("server", "browser_settings")}

WARN_LAN_WITHOUT_KEY = "lan_without_api_key"

Engines = Mapping[str, Any]
"""The ``engines.list`` result: ``{"stt": [entry], "tts": [entry]}``."""


class InvalidSetting(ValueError):
    """A change was refused. ``field`` is the settable name (or file key) at fault."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


class ConfigFileError(RuntimeError):
    """The config file cannot be read or written."""


@dataclass(frozen=True)
class SettingsChange:
    """What :func:`apply_settings` did."""

    settings: Settings
    changed: tuple[str, ...]
    restart_required: tuple[ServiceName, ...]
    warnings: tuple[str, ...]


def is_loopback(bind: str) -> bool:
    """True for addresses only this machine can reach. An empty bind is not one."""
    if bind == "localhost":
        return True
    try:
        return ipaddress.ip_address(bind).is_loopback
    except ValueError:
        return False


def bind_mode(bind: str) -> BindMode:
    return "loopback" if is_loopback(bind) else "lan"


# Views ---------------------------------------------------------------------


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


def change_view(change: SettingsChange, config_file: Path) -> dict[str, Any]:
    """The ``settings.set`` result: the new settings plus what the change did."""
    result = settings_view(change.settings, config_file)
    result["changed"] = list(change.changed)
    result["restart_required"] = list(change.restart_required)
    result["warnings"] = list(change.warnings)
    return result


# Validation ----------------------------------------------------------------


def _settable_name(section: str, key: str) -> str:
    for name, target in SETTABLE.items():
        if target == (section, key):
            return name
    return f"{section}.{key}"


def _coerce(name: str, value: object, current: Mapping[str, Any]) -> object:
    """Shape checks the section models would be too lenient about, plus the
    ``bind`` mode mapping. The models validate everything else."""
    if name == "bind":
        if value not in ("loopback", "lan"):
            raise InvalidSetting(name, 'must be "loopback" or "lan"')
        old = str(current.get("bind", LOOPBACK_BIND))
        if bind_mode(old) == value:
            return old  # keep a specific address of the same kind
        return LOOPBACK_BIND if value == "loopback" else LAN_BIND
    if name in ("zeroconf", "browser_settings"):
        if not isinstance(value, bool):
            raise InvalidSetting(name, "must be true or false")
        return value
    if name == "tts.speed":
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise InvalidSetting(name, "must be a number")
        if not 0 < float(value) <= 4:
            raise InvalidSetting(name, "must be greater than 0 and at most 4")
        return float(value)
    if not isinstance(value, str):
        raise InvalidSetting(name, "must be a string")
    if name != "api_key" and not value.strip():
        raise InvalidSetting(name, "must not be empty")
    if "\n" in value or "\r" in value:
        raise InvalidSetting(name, "must be a single line")
    return value if name == "api_key" else value.strip()


def validate_file_data(data: Mapping[str, Any]) -> dict[str, BaseModel]:
    """Validate parsed config file contents with the section models, without
    environment overrides, so an invalid file value cannot hide behind one."""
    sections: dict[str, BaseModel] = {}
    for name in data:
        if name not in Settings.model_fields:
            raise InvalidSetting(name, "unknown section")
    for name, field in Settings.model_fields.items():
        model = field.annotation
        assert isinstance(model, type) and issubclass(model, BaseModel)
        raw = data.get(name, {})
        if not isinstance(raw, Mapping):
            raise InvalidSetting(name, "must be a table")
        try:
            sections[name] = model.model_validate(raw)
        except ValidationError as exc:
            err = exc.errors()[0]
            key = str(err["loc"][0]) if err["loc"] else ""
            raise InvalidSetting(_settable_name(name, key), err["msg"]) from None
    return sections


def _engine_entry(engines: Engines, kind: str, name: object) -> Mapping[str, Any]:
    entries = {e.get("name"): e for e in engines.get(kind, []) if isinstance(e, Mapping)}
    entry = entries.get(name)
    if entry is None:
        installed = ", ".join(sorted(str(n) for n in entries)) or "none"
        raise InvalidSetting(f"{kind}.engine", f"{name!r} is not installed ({installed})")
    return entry


def complete_engine_change(
    changes: dict[str, object], old: Mapping[str, BaseModel], engines: Engines
) -> None:
    """Fill in what an engine switch implies, in place, and check voices.

    A new ``stt.engine`` or ``tts.engine`` without its ``model`` gets the
    engine's first catalogue model (the old engine's model would not load);
    a new ``tts.engine`` without ``tts.voice`` keeps the voice if the new
    engine has it and otherwise gets its first voice. An engine that is not
    installed, or a voice its catalogue lacks, is refused.
    """
    for kind in HTTP_SERVICES:
        engine = changes.get(f"{kind}.engine")
        if engine is None:
            continue
        entry = _engine_entry(engines, kind, engine)
        section = old[kind]
        assert isinstance(section, STTConfig | TTSConfig)
        if engine == section.engine or f"{kind}.model" in changes:
            continue
        models = list(entry.get("models") or [])
        if not models:
            raise InvalidSetting(f"{kind}.model", f"set {kind}.model in the same change")
        changes[f"{kind}.model"] = models[0]
    if not {"tts.engine", "tts.voice"} & set(changes):
        return
    tts = old["tts"]
    assert isinstance(tts, TTSConfig)
    engine = changes.get("tts.engine", tts.engine)
    voices = list(_engine_entry(engines, "tts", engine).get("voices") or [])
    if not voices:
        return
    if "tts.voice" not in changes:
        if tts.default_voice not in voices:
            changes["tts.voice"] = voices[0]
        return
    if changes["tts.voice"] not in voices:
        raise InvalidSetting("tts.voice", f"{engine} has no voice {changes['tts.voice']!r}")


def _env_overrides(section: str, key: str) -> bool:
    wanted = f"{ENV_PREFIX}{section}__{key}".upper()
    return any(k.upper() == wanted for k in os.environ)


# Writing -------------------------------------------------------------------


_ENGINE_KEYS = {"stt.engine", "tts.engine", "tts.voice"}


def apply_settings(
    changes: Mapping[str, object],
    path: Path | None = None,
    *,
    engines: Callable[[], Engines] | None = None,
) -> SettingsChange:
    """Write ``changes`` (settable name -> value) to the config file.

    Nothing is written unless every change is valid. Keys not in ``changes``,
    unknown sections and comments are left alone. ``engines`` supplies the
    ``engines.list`` catalogue for engine and voice changes; it is only
    called when one of those is being changed, and without it they are not
    checked. Returns the effective settings afterwards (environment overrides
    included), the services that must restart for the change to apply, and
    warnings for the operator.
    """
    path = path if path is not None else config_path()
    unknown = sorted(set(changes) - set(SETTABLE))
    if unknown:
        raise InvalidSetting(unknown[0], "not a settable key")
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        data = tomllib.loads(text)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigFileError(f"cannot read {path}: {exc}") from exc
    old_sections = validate_file_data(data)

    wanted: dict[str, object] = {}
    for name, value in changes.items():
        section, _key = SETTABLE[name]
        table = data.get(section, {})
        wanted[name] = _coerce(name, value, table if isinstance(table, Mapping) else {})
    if engines is not None and _ENGINE_KEYS & set(wanted):
        complete_engine_change(wanted, old_sections, engines())

    new_data: dict[str, Any] = {k: dict(v) if isinstance(v, dict) else v for k, v in data.items()}
    for name, value in wanted.items():
        section, key = SETTABLE[name]
        table = new_data.setdefault(section, {})
        if not isinstance(table, dict):
            raise InvalidSetting(section, "must be a table")
        table[key] = value
    sections = validate_file_data(new_data)

    changed: list[tuple[str, str]] = []
    for name in wanted:
        section, key = SETTABLE[name]
        if getattr(sections[section], key) != getattr(old_sections[section], key):
            changed.append((section, key))
    if changed:
        server = sections["server"]
        assert isinstance(server, ServerConfig)
        atomic_write(path, edit_toml(text, new_data, changed), private=bool(server.api_key))

    settings = load_settings(path)
    restart: set[ServiceName] = set()
    for section, key in changed:
        if (section, key) not in _READ_LIVE:
            restart.update(_RESTART_BY_SECTION.get(section, ()))
    warnings: list[str] = []
    if {"bind", "api_key"} & set(wanted) and (
        bind_mode(settings.server.bind) == "lan" and not settings.server.api_key
    ):
        warnings.append(WARN_LAN_WITHOUT_KEY)
    for name in wanted:
        if _env_overrides(*SETTABLE[name]):
            warnings.append(f"env_override:{name}")
    return SettingsChange(
        settings=settings,
        changed=tuple(_settable_name(s, k) for s, k in changed),
        restart_required=tuple(s for s in HTTP_SERVICES if s in restart),
        warnings=tuple(warnings),
    )


def edit_toml(text: str, new_data: Mapping[str, Any], keys: list[tuple[str, str]]) -> str:
    """``text`` with each ``(section, key)`` set to its value in ``new_data``.

    ``tomlkit`` keeps comments, order and formatting of everything else. The
    result must parse back to exactly ``new_data``; if it does not, nothing
    is written.
    """
    doc = tomlkit.parse(text)
    for section, key in keys:
        table = doc.get(section)
        if table is None:
            table = tomlkit.table()
            doc.add(section, table)
        table[key] = new_data[section][key]
    edited = tomlkit.dumps(doc)
    if tomllib.loads(edited) != new_data:
        raise ConfigFileError("the edited config file does not read back as written")
    return edited


def atomic_write(path: Path, text: str, *, private: bool = False) -> None:
    """Replace ``path`` with ``text``: temp file in the same directory, fsync,
    rename.

    A symlinked config file is followed, so the link stays and its target is
    replaced. The old file's mode is kept; a new file is 0600. ``private``
    (the file holds an API key) makes it 0600 whatever it was.
    """
    target = Path(os.path.realpath(path))
    try:
        mode = target.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = 0o600
    if private:
        mode = 0o600
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, mode)
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        dir_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        raise ConfigFileError(f"cannot write {path}: {exc}") from exc
