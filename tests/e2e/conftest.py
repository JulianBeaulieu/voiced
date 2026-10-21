"""End-to-end harness: real engines behind real uvicorn servers on ephemeral ports.

Every service here is the one ``voiced serve`` builds: the same config loader
(:func:`voiced.config.load_settings` over a TOML file), the same logging setup
and app factory (:func:`voiced.cli.build_service_app`, which also attaches the
Wyoming listener), the same uvicorn settings (:func:`voiced.cli.uvicorn_config`)
and the configured real engine. Each runs under
``uvicorn`` in its own thread, as ``voiced serve`` runs it in the main thread,
on an ephemeral HTTP port and an ephemeral Wyoming port so a test never
collides with a running daemon or another test.

One difference from ``voiced serve``: here uvicorn binds the HTTP socket itself
(``port=0``), where ``voiced serve`` binds it before loading and hands uvicorn
the pre-bound socket. That pre-bound path, and the port-clash exit it exists
for, is tested through the real ``voiced serve`` in a subprocess
(``tests/cli/test_serve.py``).

Everything here is used only by ``@pytest.mark.model`` tests, which
``tests/conftest.py`` skips unless ``VOICED_MODEL_TESTS=1``; the service
fixtures therefore never load MLX on CI. The engines (Parakeet, Kokoro,
Soprano, Phonon, Pocket TTS, OmniVoice) load once for this package and are
shared, then are torn down when the e2e tests finish, so a full ``pytest -m
model`` run does not keep the extra models resident through the engine suites
that follow.
"""

from __future__ import annotations

import gc
import logging
import threading
import time
import wave
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import httpx
import numpy as np
import pytest
import uvicorn
from numpy.typing import NDArray

from voiced.cli import GRACEFUL_SHUTDOWN_S, build_service_app, uvicorn_config
from voiced.config import Settings, load_settings
from voiced.engines.base import STT_SAMPLE_RATE
from voiced.threads import ModelThread

if TYPE_CHECKING:
    from fastapi import FastAPI
    from openai import OpenAI

# The fixtures and their known transcripts, shared with the engine tests.
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
EXPECTED = "turn on the kitchen lights and set the thermostat to twenty degrees"
EXPECTED_WORDS = {"kitchen", "lights", "thermostat", "degrees"}
EXPECTED_TRAILING = "what is the weather like tomorrow morning"
TRAILING_WORDS = {"weather", "tomorrow", "morning"}

LOAD_TIMEOUT_S = 120.0  # first run may download weights; later runs hit the cache
SILENCE_WINDOW_S = Settings().stt.vad_silence_ms / 1000  # the endpointer's silence window
# A floor for "real audio was produced": about 42 ms of 24 kHz int16 mono PCM
# (24000 samples/s * 2 bytes * 0.042 s). The shortest sentence any shipped
# engine says is several hundred ms; a stuck engine yields nothing.
MIN_AUDIO_BYTES = 2000
# A reply long enough (18 sentences, several seconds of GPU work) that its
# synthesis is still running when a test acts on it.
LONG_INPUT = (
    "Turn on the kitchen lights. Set the thermostat to twenty degrees. "
    "What is the weather like tomorrow morning? Play some music in the living room. "
    "Lock the front door and arm the alarm. Dim the bedroom lights to fifty percent. "
) * 3


def read_wav_pcm16(name: str) -> tuple[NDArray[np.int16], int]:
    """Return a fixture WAV as mono int16 samples and its sample rate."""
    with wave.open(str(FIXTURES / name)) as wav:
        assert wav.getnchannels() == 1 and wav.getsampwidth() == 2
        rate = wav.getframerate()
        pcm = wav.readframes(wav.getnframes())
    return np.frombuffer(pcm, dtype="<i2").copy(), rate


def wav_bytes(name: str) -> bytes:
    """The raw bytes of a fixture WAV, for an OpenAI/Wyoming upload."""
    return (FIXTURES / name).read_bytes()


def silence_pcm16(seconds: float, rate: int = STT_SAMPLE_RATE) -> bytes:
    return np.zeros(round(seconds * rate), dtype="<i2").tobytes()


def pcm16_to_wav(data: bytes, rate: int) -> bytes:
    """Wrap raw mono int16 PCM in a WAV container, so synthesized audio can be
    uploaded to the STT endpoint (which ffmpeg-decodes and resamples) for a
    text-to-speech-to-text content round trip."""
    import io

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(data)
    return buffer.getvalue()


@dataclass
class LiveService:
    """A running ``stt`` or ``tts`` service and the addresses to reach it."""

    service: Literal["stt", "tts"]
    settings: Settings
    app: FastAPI
    server: uvicorn.Server
    thread: threading.Thread
    http_port: int

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.http_port}"

    @property
    def v1_url(self) -> str:
        return f"{self.base_url}/v1"

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.http_port}/v1/audio/transcriptions/stream"

    @property
    def wyoming_host(self) -> str:
        return "127.0.0.1"

    @property
    def wyoming_port(self) -> int:
        port: int = self.app.state.wyoming.port
        return port

    @property
    def ctx(self) -> Any:
        return self.app.state.ctx

    @contextmanager
    def override(
        self,
        *,
        queue_capacity: int | None = None,
        streams: int | None = None,
        session_idle_s: float | None = None,
        write_timeout_s: float | None = None,
    ) -> Iterator[None]:
        """Temporarily change a live knob (queue depth, the streams served at
        once or a socket bound) on the shared warm service, so the busy and
        timeout paths are exercised without reloading a model. Restored on
        exit; tests run serially."""
        ctx = self.ctx
        saved = (
            ctx.queue.capacity,
            ctx.queue.max_shared,
            ctx.settings.server.session_idle_s,
            ctx.settings.server.write_timeout_s,
        )
        if queue_capacity is not None:
            ctx.queue.capacity = queue_capacity
        if streams is not None:
            ctx.queue.max_shared = streams
        if session_idle_s is not None:
            ctx.settings.server.session_idle_s = session_idle_s
        if write_timeout_s is not None:
            ctx.settings.server.write_timeout_s = write_timeout_s
        try:
            yield
        finally:
            (
                ctx.queue.capacity,
                ctx.queue.max_shared,
                ctx.settings.server.session_idle_s,
                ctx.settings.server.write_timeout_s,
            ) = saved

    def openai_client(self, api_key: str = "voiced-test") -> OpenAI:
        from openai import OpenAI

        return OpenAI(base_url=self.v1_url, api_key=api_key, max_retries=0, timeout=60.0)

    def get(self, path: str) -> httpx.Response:
        return httpx.get(f"{self.base_url}{path}", timeout=30.0)

    def health(self) -> dict[str, Any]:
        return dict(self.get("/health").json())

    def metrics(self) -> dict[str, Any]:
        return dict(self.get("/metrics.json").json())

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=GRACEFUL_SHUTDOWN_S + 10)
        self._release_model()

    def _release_model(self) -> None:
        """Free the engine's MLX memory so a later model test that measures
        ``mx.get_peak_memory()`` (``tests/stt/test_parakeet``) is not charged for
        this service's weights when the whole model tier runs in one process."""
        ctx = self.app.state.ctx
        engine = ctx.engine
        thread = getattr(engine, "_thread", None)  # engines that own a ModelThread
        if isinstance(thread, ModelThread):
            thread.shutdown()  # the thread that holds the MLX arrays exits
        ctx.engine = None  # drop the last strong reference to the model
        del engine
        gc.collect()
        try:
            import mlx.core as mx

            mx.clear_cache()
        except ImportError:
            pass


def _settings_for(service: Literal["stt", "tts"], tmp: Path, **overrides: str) -> Settings:
    """Write a config.toml and load it through the real loader.

    The Wyoming port is set to 0 (ephemeral) afterwards, by attribute
    assignment, because the config schema requires a port >= 1; ``build_*_app``
    then binds a free one that the test reads back from ``app.state.wyoming``.
    The HTTP bind uses uvicorn's own ``port=0`` in :func:`start_service`
    (not ``voiced serve``'s pre-bound socket; see the module docstring).
    """
    tmp.mkdir(parents=True, exist_ok=True)
    lines = [
        "[server]",
        f'log_dir = "{tmp / "logs"}"',
        'bind = "127.0.0.1"',
        "zeroconf = false",
        f"[{service}]",
    ]
    lines += [f"{key} = {value}" for key, value in overrides.items()]
    config = tmp / f"{service}.toml"
    config.write_text("\n".join(lines) + "\n")
    settings = load_settings(config)
    section = settings.stt if service == "stt" else settings.tts
    section.wyoming_port = 0
    return settings


def start_service(
    service: Literal["stt", "tts"], settings: Settings, *, wait: bool = True
) -> LiveService:
    """Build the service app the way ``voiced serve`` does (logging configured
    into the settings' ``log_dir``, the same app factory, the same uvicorn
    settings) and run it on an ephemeral HTTP port.

    ``build_service_app`` configures logging on the root logger, which would
    replace pytest's capture handlers and leave a file handler and the service's
    level on the root logger for the rest of the session. The root handlers and
    level are restored around it, so the services log into pytest's capture.
    """
    app = _build_keeping_root_logging(settings, service)
    server = uvicorn.Server(uvicorn_config(app, settings, port=0))
    failure: list[BaseException] = []

    def run() -> None:
        # uvicorn ends a failed startup with sys.exit(); keep it in this thread
        # and report it below instead of as an unhandled thread exception.
        try:
            server.run()
        except SystemExit as exc:
            failure.append(exc)

    thread = threading.Thread(target=run, name=f"uvicorn-{service}", daemon=True)
    thread.start()
    deadline = time.monotonic() + 15.0
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            server.should_exit = True
            raise RuntimeError(f"{service} server did not start: {failure!r}")
        time.sleep(0.01)
    http_port = server.servers[0].sockets[0].getsockname()[1]
    live = LiveService(service, settings, app, server, thread, http_port)
    if wait:
        try:
            wait_until_loaded(live)
        except BaseException:
            live.stop()  # do not leave the server thread and the engine behind
            raise
    return live


def _build_keeping_root_logging(settings: Settings, service: Literal["stt", "tts"]) -> FastAPI:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        return build_service_app(settings, service)
    finally:
        for handler in list(root.handlers):
            if handler not in handlers:
                root.removeHandler(handler)
                handler.close()
        for handler in handlers:
            if handler not in root.handlers:
                root.addHandler(handler)
        root.setLevel(level)


def wait_until_loaded(live: LiveService, timeout_s: float = LOAD_TIMEOUT_S) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if live.get("/health").status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.05)
    raise RuntimeError(f"{live.service} engine did not warm up within {timeout_s:g}s")


@pytest.fixture(scope="package")
def e2e_tmp(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("voiced-e2e")


@pytest.fixture(scope="package")
def stt_service(e2e_tmp: Path) -> Iterator[LiveService]:
    settings = _settings_for("stt", e2e_tmp, **{"engine": '"parakeet"'})
    service = start_service("stt", settings)
    yield service
    service.stop()


@pytest.fixture(scope="package")
def tts_kokoro_service(e2e_tmp: Path) -> Iterator[LiveService]:
    settings = _settings_for("tts", e2e_tmp / "kokoro", **{"engine": '"kokoro"'})
    service = start_service("tts", settings)
    yield service
    service.stop()


@pytest.fixture(scope="package")
def tts_soprano_service(e2e_tmp: Path) -> Iterator[LiveService]:
    # engine, model and default_voice must change together: the model and voice
    # defaults are Kokoro's, and engine=soprano alone fails at load with an
    # AttributeError (ADR 0015, defect 5).
    settings = _settings_for(
        "tts",
        e2e_tmp / "soprano",
        **{
            "engine": '"soprano"',
            "model": '"mlx-community/Soprano-80M-bf16"',
            "default_voice": '"default"',
        },
    )
    service = start_service("tts", settings)
    yield service
    service.stop()


@pytest.fixture(scope="package")
def tts_omnivoice_service(e2e_tmp: Path) -> Iterator[LiveService]:
    settings = _settings_for(
        "tts",
        e2e_tmp / "omnivoice",
        **{
            "engine": '"omnivoice"',
            "model": '"k2-fsa/OmniVoice"',
            "default_voice": '"en-US-female"',
        },
    )
    service = start_service("tts", settings)
    yield service
    service.stop()


@pytest.fixture(scope="package")
def tts_phonon_service(e2e_tmp: Path) -> Iterator[LiveService]:
    settings = _settings_for(
        "tts",
        e2e_tmp / "phonon",
        **{
            "engine": '"phonon"',
            "model": '"gradium/phonon-31m-en"',
            "default_voice": '"default"',
        },
    )
    service = start_service("tts", settings)
    yield service
    service.stop()


@pytest.fixture(scope="package")
def tts_pocket_service(e2e_tmp: Path) -> Iterator[LiveService]:
    settings = _settings_for(
        "tts",
        e2e_tmp / "pocket",
        **{
            "engine": '"pocket"',
            "model": '"mlx-community/pocket-tts"',
            "default_voice": '"alba"',
        },
    )
    service = start_service("tts", settings)
    yield service
    service.stop()


@pytest.fixture(scope="package")
def tts_marvis_service(e2e_tmp: Path) -> Iterator[LiveService]:
    settings = _settings_for(
        "tts",
        e2e_tmp / "marvis",
        **{
            "engine": '"marvis"',
            "model": '"Marvis-AI/marvis-tts-100m-v0.2-MLX-8bit"',
            "default_voice": '"conversational_a"',
        },
    )
    service = start_service("tts", settings)
    yield service
    service.stop()


@pytest.fixture(scope="package")
def tts_chatterbox_service(e2e_tmp: Path) -> Iterator[LiveService]:
    settings = _settings_for(
        "tts",
        e2e_tmp / "chatterbox",
        **{
            "engine": '"chatterbox"',
            "model": '"mlx-community/chatterbox-turbo-4bit"',
            "default_voice": '"default"',
        },
    )
    service = start_service("tts", settings)
    yield service
    service.stop()


@pytest.fixture(scope="package")
def stt_qwen3_service(e2e_tmp: Path) -> Iterator[LiveService]:
    settings = _settings_for(
        "stt",
        e2e_tmp / "qwen3-asr",
        **{"engine": '"qwen3-asr"', "model": '"mlx-community/Qwen3-ASR-0.6B-8bit"'},
    )
    service = start_service("stt", settings)
    yield service
    service.stop()


@pytest.fixture(scope="package")
def stt_whisper_service(e2e_tmp: Path) -> Iterator[LiveService]:
    settings = _settings_for(
        "stt",
        e2e_tmp / "whisper",
        **{"engine": '"whisper"', "model": '"mlx-community/whisper-large-v3-turbo-asr-8bit"'},
    )
    service = start_service("stt", settings)
    yield service
    service.stop()


@pytest.fixture(scope="package")
def stt_moonshine_service(e2e_tmp: Path) -> Iterator[LiveService]:
    pytest.importorskip("moonshine_voice")  # macOS 15 and the moonshine extra only
    settings = _settings_for(
        "stt",
        e2e_tmp / "moonshine",
        **{"engine": '"moonshine"', "model": '"moonshine-ai/moonshine-streaming-small"'},
    )
    service = start_service("stt", settings)
    yield service
    service.stop()
