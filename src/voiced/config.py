"""Configuration model.

One TOML file (``~/.config/voiced/config.toml`` by default) plus environment
variable overrides. Environment variables are named ``VOICED_<SECTION>__<KEY>``
and take precedence over the file. Every value has a default so the services
start with no config file at all.

See ``config.example.toml`` at the repository root for the documented copy.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
import tomllib
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

ENV_PREFIX = "VOICED_"
ENV_CONFIG_PATH = "VOICED_CONFIG"
DEFAULT_CONFIG_PATH = Path("~/.config/voiced/config.toml")

# Single source of truth for the launchd label prefix. Changing this constant
# (or the ``[launchd] label_prefix`` config key) is all that is needed to move
# to a different reverse-DNS prefix such as ``computer.voiced`` later.
DEFAULT_LAUNCHD_LABEL_PREFIX = "io.github.julianbeaulieu.voiced"

LogLevel = Literal["debug", "info", "warning", "error"]
ServiceName = Literal["stt", "tts"]
DaemonName = Literal["stt", "tts", "watchdog", "metalprobe"]

# The two inference services, and the daemons the installer manages.
HTTP_SERVICES: tuple[ServiceName, ...] = ("stt", "tts")
SERVICES: tuple[DaemonName, ...] = ("stt", "tts", "watchdog")

# Labels end up in launchd plists and in shell scripts the user runs with sudo.
_LABEL_PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*$")

# The TOML file for the Settings instance currently being built. Set by
# load_settings() so that Settings() itself stays usable (env and defaults only).
_toml_path: ContextVar[Path | None] = ContextVar("voiced_toml_path", default=None)


class _Section(BaseModel):
    """Unknown keys are errors so typos in the config file do not go unnoticed."""

    model_config = ConfigDict(extra="forbid")


class ServerConfig(_Section):
    bind: str = "127.0.0.1"
    api_key: str = ""
    log_dir: Path = Field(default_factory=lambda: Path("~/Library/Logs/voiced").expanduser())
    log_level: LogLevel = "info"
    # Advertise the Wyoming listeners over mDNS so Home Assistant discovers
    # them. Only takes effect when ``bind`` is not a loopback address.
    zeroconf: bool = True
    # Show a settings form on each service's status page. Off by default; see
    # ADR 0013 for the rules that apply when it is on.
    browser_settings: bool = False
    # Bounds on socket awaits for transports holding the inference slot, so a
    # half-open peer cannot pin the GPU (voiced.inference.bounded).
    session_idle_s: float = Field(default=30.0, gt=0)
    write_timeout_s: float = Field(default=10.0, gt=0)

    @field_validator("log_dir", mode="after")
    @classmethod
    def _expand(cls, value: Path) -> Path:
        return value.expanduser()


class STTConfig(_Section):
    http_port: int = Field(default=8881, ge=1, le=65535)
    wyoming_port: int = Field(default=10300, ge=1, le=65535)
    engine: str = "parakeet"
    model: str = "mlx-community/parakeet-tdt-0.6b-v3"
    languages: list[str] = Field(default_factory=lambda: ["en"], min_length=1)
    vad_silence_ms: int = Field(default=200, ge=0)
    queue_depth: int = Field(default=8, ge=1)
    max_upload_mb: int = Field(default=50, ge=1)


class TTSConfig(_Section):
    http_port: int = Field(default=8880, ge=1, le=65535)
    wyoming_port: int = Field(default=10200, ge=1, le=65535)
    engine: str = "kokoro"
    model: str = "mlx-community/Kokoro-82M-bf16"
    default_voice: str = "af_heart"
    languages: list[str] = Field(default_factory=lambda: ["en"], min_length=1)
    speed: float = Field(default=1.0, gt=0)
    queue_depth: int = Field(default=8, ge=1)
    max_input_chars: int = Field(default=4096, ge=1)


class LaunchdConfig(_Section):
    label_prefix: str = DEFAULT_LAUNCHD_LABEL_PREFIX

    @field_validator("label_prefix", mode="after")
    @classmethod
    def _safe_label(cls, value: str) -> str:
        if not _LABEL_PREFIX_RE.fullmatch(value):
            raise ValueError("label_prefix may contain only letters, digits, '.' and '-'")
        return value

    def label(self, service: DaemonName) -> str:
        return f"{self.label_prefix}.{service}"


class WatchdogConfig(_Section):
    interval_s: float = Field(default=15.0, gt=0)
    failures_before_restart: int = Field(default=3, ge=1)
    # Per-request timeout for the /health poll.
    timeout_s: float = Field(default=5.0, gt=0)
    # A service may report "starting" for this long (measured by its own
    # uptime) before the watchdog treats the model load as hung.
    startup_grace_s: float = Field(default=300.0, gt=0)
    # Continuous "degraded" for longer than this triggers a restart.
    degraded_window_s: float = Field(default=120.0, gt=0)
    # Minimum time between two restarts of the same service.
    restart_cooldown_s: float = Field(default=120.0, ge=0)
    # A service whose queue is non-empty but has not completed an inference
    # for this long is considered stalled (the GPU is wedged).
    stall_after_s: float = Field(default=60.0, gt=0)
    # Optional small inference request per poll (only while the queue is
    # empty); a timeout counts as a stall.
    probe: bool = False
    probe_timeout_s: float = Field(default=20.0, gt=0)


class InstallConfig(_Section):
    # Where `voiced install` creates the virtual environment.
    venv_dir: Path = Field(default_factory=lambda: Path("~/.voiced").expanduser())
    # Interpreter or version spec for the venv ("3.12", or a path). Empty = auto.
    python: str = ""
    # Hugging Face cache for model downloads; empty = the Hugging Face default.
    hf_home: Path | None = None

    @field_validator("venv_dir", "hf_home", mode="after")
    @classmethod
    def _expand(cls, value: Path | None) -> Path | None:
        return value.expanduser() if value is not None else None


class Settings(BaseSettings):
    """Top-level config. Build with :func:`load_settings`, not directly."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter="__",
        extra="forbid",
    )

    server: ServerConfig = Field(default_factory=ServerConfig)
    stt: STTConfig = Field(default_factory=STTConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    launchd: LaunchdConfig = Field(default_factory=LaunchdConfig)
    watchdog: WatchdogConfig = Field(default_factory=WatchdogConfig)
    install: InstallConfig = Field(default_factory=InstallConfig)

    def http_port(self, service: ServiceName) -> int:
        return self.stt.http_port if service == "stt" else self.tts.http_port

    def http_url(self, service: ServiceName) -> str:
        """Base URL for talking to a service from this machine.

        ``0.0.0.0`` means "all interfaces" to the listener but is not an
        address a client can connect to, so loopback is used instead.
        """
        host = "127.0.0.1" if self.server.bind in ("0.0.0.0", "") else self.server.bind
        return f"http://{host}:{self.http_port(service)}"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Precedence, highest first: explicit kwargs, environment, TOML file, defaults.
        # .env files are deliberately not a source.
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
        path = _toml_path.get()
        if path is not None and path.is_file():
            sources.append(TomlConfigSettingsSource(settings_cls, toml_file=path))
        return tuple(sources)


def config_path() -> Path:
    """The config file in use: ``$VOICED_CONFIG`` or the default location."""
    override = os.environ.get(ENV_CONFIG_PATH)
    return Path(override).expanduser() if override else DEFAULT_CONFIG_PATH.expanduser()


def load_settings(path: Path | None = None) -> Settings:
    """Load settings from ``path`` (or :func:`config_path`) with env overrides.

    A missing file is not an error: defaults apply. A malformed file raises
    ``tomllib.TOMLDecodeError``; unknown keys raise ``pydantic.ValidationError``.
    """
    token = _toml_path.set(path if path is not None else config_path())
    try:
        return Settings()
    finally:
        _toml_path.reset(token)


# Changing settings ---------------------------------------------------------
#
# The control channel and the status page's settings form change a few keys
# of the config file. Both go through apply_settings(), which validates with
# the section models above, edits only the lines it changes (comments,
# unknown-to-us formatting and every other key stay as they were) and
# replaces the file atomically. See ADR 0013.

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
# file on every request by the status page, so changing it needs no restart.
_RESTART_BY_SECTION: dict[str, tuple[ServiceName, ...]] = {
    "server": HTTP_SERVICES,
    "stt": ("stt",),
    "tts": ("tts",),
}
_READ_LIVE = {("server", "browser_settings")}

WARN_LAN_WITHOUT_KEY = "lan_without_api_key"


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
    comments_preserved: bool = True


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


def _env_overrides(section: str, key: str) -> bool:
    wanted = f"{ENV_PREFIX}{section}__{key}".upper()
    return any(k.upper() == wanted for k in os.environ)


Check = Callable[[dict[str, BaseModel], set[str]], None]
"""Extra validation of the would-be file (for example: is the engine
installed?). Gets the validated sections and the settable names being
changed; raises :class:`InvalidSetting`."""


def apply_settings(
    changes: Mapping[str, object], path: Path | None = None, *, check: Check | None = None
) -> SettingsChange:
    """Write ``changes`` (settable name -> value) to the config file.

    Nothing is written unless every change is valid. Keys not in ``changes``,
    unknown sections and comments are left alone. Returns the effective
    settings afterwards (environment overrides included), the services that
    must restart for the change to apply, and warnings for the operator.
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

    new_data: dict[str, Any] = {k: dict(v) if isinstance(v, dict) else v for k, v in data.items()}
    targets: list[tuple[str, str]] = []
    for name, value in changes.items():
        section, key = SETTABLE[name]
        table = new_data.setdefault(section, {})
        if not isinstance(table, dict):
            raise InvalidSetting(section, "must be a table")
        table[key] = _coerce(name, value, table)
        targets.append((section, key))

    sections = validate_file_data(new_data)
    if check is not None:
        check(sections, set(changes))

    old_sections = validate_file_data(data)
    changed = [
        (section, key)
        for section, key in dict.fromkeys(targets)
        if getattr(sections[section], key) != getattr(old_sections[section], key)
    ]

    preserved = True
    if changed:
        new_text = text
        for section, key in changed:
            edited = _set_toml_line(new_text, section, key, new_data[section][key])
            if edited is None:
                break
            new_text = edited
        else:
            edited = new_text
        if edited is None or _parse_or_none(new_text) != new_data:
            new_text, preserved = dump_toml(new_data), False
        atomic_write(path, new_text)

    settings = load_settings(path)
    restart: set[ServiceName] = set()
    for section, key in changed:
        if (section, key) not in _READ_LIVE:
            restart.update(_RESTART_BY_SECTION.get(section, ()))
    warnings: list[str] = []
    if {"bind", "api_key"} & set(changes) and (
        bind_mode(settings.server.bind) == "lan" and not settings.server.api_key
    ):
        warnings.append(WARN_LAN_WITHOUT_KEY)
    for section, key in targets:
        if _env_overrides(section, key):
            warnings.append(f"env_override:{_settable_name(section, key)}")
    return SettingsChange(
        settings=settings,
        changed=tuple(_settable_name(s, k) for s, k in changed),
        restart_required=tuple(s for s in HTTP_SERVICES if s in restart),
        warnings=tuple(warnings),
        comments_preserved=preserved,
    )


def atomic_write(path: Path, text: str) -> None:
    """Replace ``path`` with ``text``: temp file in the same directory, fsync,
    rename. Keeps the old file's mode; a new file is 0600 (it may hold the
    API key)."""
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = 0o600
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, mode)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        raise ConfigFileError(f"cannot write {path}: {exc}") from exc


# Minimal TOML writing (tomllib only reads) ---------------------------------


def toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value) if value == value and abs(value) != float("inf") else str(value)
    if isinstance(value, str | Path):
        return json.dumps(str(value), ensure_ascii=False)
    if isinstance(value, list | tuple):
        return "[" + ", ".join(toml_value(v) for v in value) + "]"
    if isinstance(value, Mapping):
        return "{" + ", ".join(f"{_toml_key(k)} = {toml_value(v)}" for k, v in value.items()) + "}"
    raise TypeError(f"cannot write {type(value).__name__} to TOML")


_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_key(key: str) -> str:
    return key if _BARE_KEY.match(key) else json.dumps(key)


def dump_toml(data: Mapping[str, Any]) -> str:
    """Serialize nested tables of plain values. Comments are not kept: the
    fallback when :func:`_set_toml_line` cannot edit the file in place."""
    lines: list[str] = []

    def table(prefix: str, body: Mapping[str, Any]) -> None:
        scalars = [(k, v) for k, v in body.items() if not isinstance(v, Mapping)]
        nested = [(k, v) for k, v in body.items() if isinstance(v, Mapping)]
        if prefix and (scalars or not nested):
            if lines:
                lines.append("")
            lines.append(f"[{prefix}]")
        lines.extend(f"{_toml_key(k)} = {toml_value(v)}" for k, v in scalars)
        for k, v in nested:
            table(f"{prefix}.{_toml_key(k)}" if prefix else _toml_key(k), v)

    table("", data)
    return "\n".join(lines) + "\n"


def _parse_or_none(text: str) -> dict[str, Any] | None:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None


def _header(line: str) -> str | None:
    """The table name if ``line`` is a plain ``[table]`` header."""
    stripped = line.strip()
    if not stripped.startswith("[") or stripped.startswith("[["):
        return None
    end = stripped.find("]")
    if end < 0:
        return None
    rest = stripped[end + 1 :].strip()
    if rest and not rest.startswith("#"):
        return None
    return stripped[1:end].strip()


def _split_value_comment(rest: str) -> tuple[str, str] | None:
    """Split ``value  # comment`` by trying each ``#`` as the comment start."""
    for i, ch in enumerate(rest):
        if ch == "#" and _parse_or_none(f"x = {rest[:i].strip()}") is not None:
            return rest[:i], rest[i:]
    if _parse_or_none(f"x = {rest.strip()}") is not None:
        return rest, ""
    return None


def _set_toml_line(text: str, section: str, key: str, value: object) -> str | None:
    """``text`` with ``[section] key`` set to ``value``, changing one line.

    Returns None when the file's layout is not the plain one this handles
    (dotted keys, inline tables, multi-line values); the caller then falls
    back to :func:`dump_toml`.
    """
    lines = text.splitlines(keepends=True)
    key_re = re.compile(rf"^(\s*{re.escape(key)}\s*=\s*)(.*?)(\r?\n)?$")
    start: int | None = None
    end = len(lines)
    for i, line in enumerate(lines):
        name = _header(line)
        if name is None:
            continue
        if start is not None:
            end = i
            break
        if name == section:
            start = i
    rendered = toml_value(value)
    if start is None:
        sep = "" if not text or text.endswith("\n") else "\n"
        blank = "\n" if text.strip() else ""
        return f"{text}{sep}{blank}[{section}]\n{key} = {rendered}\n"
    last_pair = start
    for i in range(start + 1, end):
        match = key_re.match(lines[i])
        if match:
            split = _split_value_comment(match.group(2))
            if split is None:
                return None
            old, comment = split
            spacing = old[len(old.rstrip()) :] if comment else ""
            lines[i] = f"{match.group(1)}{rendered}{spacing}{comment}{match.group(3) or ''}"
            return "".join(lines)
        stripped = lines[i].strip()
        if stripped and not stripped.startswith("#"):
            last_pair = i
    if not lines[last_pair].endswith("\n"):
        lines[last_pair] += "\n"
    lines.insert(last_pair + 1, f"{key} = {rendered}\n")
    return "".join(lines)
