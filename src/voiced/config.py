"""Configuration model.

One TOML file (``~/.config/voiced/config.toml`` by default) plus environment
variable overrides. Environment variables are named ``VOICED_<SECTION>__<KEY>``
and take precedence over the file. Every value has a default so the services
start with no config file at all.

See ``config.example.toml`` at the repository root for the documented copy.
"""

from __future__ import annotations

import os
import re
from contextvars import ContextVar
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
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

    @field_validator("log_dir", mode="after")
    @classmethod
    def _expand(cls, value: Path) -> Path:
        return value.expanduser()


class STTConfig(_Section):
    http_port: int = Field(default=8881, ge=1, le=65535)
    wyoming_port: int = Field(default=10300, ge=1, le=65535)
    engine: str = "parakeet"
    model: str = "mlx-community/parakeet-tdt-0.6b-v3"
    languages: list[str] = Field(default_factory=lambda: ["en"])
    vad_silence_ms: int = Field(default=200, ge=0)
    queue_depth: int = Field(default=8, ge=1)
    max_upload_mb: int = Field(default=50, ge=1)


class TTSConfig(_Section):
    http_port: int = Field(default=8880, ge=1, le=65535)
    wyoming_port: int = Field(default=10200, ge=1, le=65535)
    engine: str = "kokoro"
    model: str = "mlx-community/Kokoro-82M-bf16"
    default_voice: str = "af_heart"
    languages: list[str] = Field(default_factory=lambda: ["en"])
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
    # Where `voiced install` creates the virtual environment. Keep it short:
    # espeak-ng (Kokoro's phonemizer) fails with long data paths.
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
