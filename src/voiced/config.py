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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

ENV_PREFIX = "VOICED_"
ENV_CONFIG_PATH = "VOICED_CONFIG"
DEFAULT_CONFIG_PATH = Path("~/.config/voiced/config.toml")
# The default log directory, under the home. The plists name it whatever the
# config says (ADR 0018: root renders them from the user name alone); a
# custom ``[server] log_dir`` takes over once the config is read.
LOG_DIR_IN_HOME = Path("Library/Logs/voiced")

# Environment variables the engines read, set from the config file
# (:meth:`Settings.engine_environment`): the plists carry only a fixed
# environment (ADR 0018).
ENV_HF_HOME = "HF_HOME"
ENV_ESPEAK_LIBRARY = "VOICED_ESPEAK_LIBRARY"
ENV_ESPEAK_DATA = "VOICED_ESPEAK_DATA"
ENGINE_ENV: dict[str, tuple[str, str]] = {  # variable -> (section, key) that sets it
    ENV_HF_HOME: ("install", "hf_home"),
    ENV_ESPEAK_LIBRARY: ("tts", "espeak_library"),
    ENV_ESPEAK_DATA: ("tts", "espeak_data"),
}
# Hugging Face variables a shell may export for other tools, which would move
# the cache away from HF_HOME's (the legacy name too) or fetch from a mirror:
# cleared, so the installer and the services agree on one cache and the hub.
CLEARED_HF_ENV = ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "HF_ENDPOINT")

# The launchd label prefix. The privileged script (voiced/launchd/
# voiced-privileged.sh) spells it too, since its only input is the user name;
# a test keeps the two equal. ``[launchd] label_prefix`` changes which labels
# the CLI and the watchdog address, not what the root step installs (ADR 0003).
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


def _absolute(value: Path | None) -> Path | None:
    """``~`` expanded, and refused unless absolute: the services (cwd the
    home), the installer and its children (any cwd) must agree on it."""
    if value is None:
        return None
    path = value.expanduser()
    if not path.is_absolute():
        raise ValueError(f"{value} must be an absolute path, or start with ~")
    return path


class _Section(BaseModel):
    """Unknown keys are errors so typos in the config file do not go unnoticed."""

    model_config = ConfigDict(extra="forbid")


class ServerConfig(_Section):
    bind: str = "127.0.0.1"
    api_key: str = ""
    log_dir: Path = Field(default_factory=lambda: Path.home() / LOG_DIR_IN_HOME)
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

    _absolute_log_dir = field_validator("log_dir", mode="after")(_absolute)


# voiced.engines.base.STT_SAMPLE_RATE and the 16-bit sample width, restated:
# the watchdog imports this module and must not import numpy through engines.
_STT_RATE = 16_000
_PCM16_BYTES = 2


STT_STREAMS = 2
"""Stream sessions within the finalize target for Parakeet on an M1 Max (ADR 0020)."""


class STTConfig(_Section):
    http_port: int = Field(default=8881, ge=1, le=65535)
    wyoming_port: int = Field(default=10300, ge=1, le=65535)
    engine: str = "parakeet"
    model: str = "mlx-community/parakeet-tdt-0.6b-v3"
    languages: list[str] = Field(default_factory=lambda: ["en"], min_length=1)
    vad_silence_ms: int = Field(default=200, ge=0)
    queue_depth: int = Field(default=8, ge=1)
    # Stream sessions served at once; the next waits. See open_streams.
    streams: int = Field(default=STT_STREAMS, ge=1)
    max_upload_mb: int = Field(default=50, ge=1)

    @model_validator(mode="after")
    def _streams_fit_the_queue(self) -> STTConfig:
        # Only a value someone set: the default must not make a smaller
        # queue_depth invalid.
        if "streams" in self.model_fields_set and self.streams > self.queue_depth:
            raise ValueError(
                f"streams ({self.streams}) is more than queue_depth ({self.queue_depth})"
            )
        return self

    def open_streams(self, engine_max: int) -> int:
        """Stream sessions the service opens at once: ``streams``, at most
        ``queue_depth`` and what the engine can interleave (its ``max_streams``)."""
        return min(self.streams, self.queue_depth, engine_max)

    @property
    def max_audio_s(self) -> float:
        """Longest audio one request may carry, on any transport: ``max_upload_mb``
        read as 16-bit samples at 16 kHz (about 26 minutes at the default 50)."""
        return self.max_upload_mb * 1_000_000 / _PCM16_BYTES / _STT_RATE


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
    # espeak-ng for Kokoro: a libespeak-ng.dylib and its data directory, in
    # place of the wheel's copy and Homebrew's (ADR 0009).
    espeak_library: Path | None = None
    espeak_data: Path | None = None

    _absolute_espeak = field_validator("espeak_library", "espeak_data", mode="after")(_absolute)


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

    _absolute_hf_home = field_validator("hf_home", mode="after")(_absolute)

    @field_validator("venv_dir", mode="after")
    @classmethod
    def _expand(cls, value: Path) -> Path:
        return value.expanduser()


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

    def engine_environment(self) -> dict[str, str]:
        """:data:`ENGINE_ENV` as the config sets them; unset keys are left out."""
        values = {env: getattr(getattr(self, s), key) for env, (s, key) in ENGINE_ENV.items()}
        return {env: str(value) for env, value in values.items() if value is not None}

    def ports(self) -> dict[str, int]:
        """The four service ports, by the config key that sets each."""
        return {
            "[stt] http_port": self.stt.http_port,
            "[stt] wyoming_port": self.stt.wyoming_port,
            "[tts] http_port": self.tts.http_port,
            "[tts] wyoming_port": self.tts.wyoming_port,
        }

    def http_port(self, service: ServiceName) -> int:
        return self.stt.http_port if service == "stt" else self.tts.http_port

    def http_url(self, service: ServiceName) -> str:
        """Base URL for talking to a service from this machine.

        ``0.0.0.0`` and ``::`` mean "all interfaces" to the listener but are
        not addresses a client can connect to, so loopback is used instead.
        IPv6 literals are bracketed.
        """
        host = self.server.bind
        if host in ("0.0.0.0", "", "::"):
            host = "127.0.0.1"
        elif ":" in host:
            host = f"[{host}]"  # an IPv6 literal
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


def apply_engine_environment(settings: Settings) -> list[str]:
    """Make :data:`ENGINE_ENV` in this process exactly what the config says,
    for the engines, ``huggingface_hub`` (which reads ``HF_HOME`` on import)
    and every child: one rule for the services, ``doctor`` and the
    installer's model download, whatever the shell exported. The
    :data:`CLEARED_HF_ENV` variables are removed.

    Returns a notice for each ``VOICED_ESPEAK_*`` the shell set otherwise
    (voiced's own variables; ``HF_HOME`` is often exported for other tools).
    """
    wanted = settings.engine_environment()
    notices = [
        f"ignoring {key} from the environment; set [{section}] {name} in the config"
        for key, (section, name) in ENGINE_ENV.items()
        if key != ENV_HF_HOME and os.environ.get(key, wanted.get(key)) != wanted.get(key)
    ]
    for key in (*ENGINE_ENV, *CLEARED_HF_ENV):
        os.environ.pop(key, None)
    os.environ.update(wanted)
    return notices


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
