"""Frozen engine interfaces.

Everything that does inference implements one of the two abstract classes
below. The HTTP and Wyoming layers only ever talk to these interfaces, which
is what lets third parties add Whisper, Piper, Pocket TTS and so on without
touching the server (see ``voiced.engines.registry``).

Audio conventions (fixed for the whole project):

* **STT input** is mono float32 PCM at 16 000 Hz in the range [-1.0, 1.0],
  as a one-dimensional ``numpy`` array. Decoding and resampling from whatever
  the client uploaded is the API layer's job; engines never see files.
* **TTS output** is mono signed 16-bit little-endian PCM at 24 000 Hz, as
  ``bytes``. Engines that produce another rate must resample before yielding.
  Encoding to WAV, MP3 or Opus is the API layer's job.

Concurrency: engine methods are ``async`` so the server can await them, but
the GPU work inside is blocking and not thread-safe. Engines run blocking
calls with ``voiced.threads.run_blocking`` (not ``asyncio.to_thread``, which
lets a cancelled caller release the slot while the thread still runs) and
must not assume they are reentrant: the
server serializes calls per engine through ``voiced.queue.InferenceQueue`` and
never calls two inference methods on the same engine at once, with one
exception an engine opts into: an STT engine whose ``max_streams`` is above 1
may have up to that many stream sessions open at once (never alongside a
``transcribe``), and must interleave their work itself. ``load()`` is called
exactly once, before any other method.

Cancellation: the server cancels the awaiting task when a client disconnects.
Engines must release any per-request state on ``CancelledError`` (a ``finally``
block is enough), must not leave the model in a state that breaks the next
call, and must not swallow ``CancelledError``.

Errors: raise :class:`EngineError` or a subclass for anything the client could
have caused (bad voice, unsupported language, text too long). Any other
exception is treated as an engine failure: the server returns 500, records it
in ``/health`` as ``degraded``, and the watchdog may restart the process.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal

import numpy as np
from numpy.typing import NDArray

STT_SAMPLE_RATE = 16_000
TTS_SAMPLE_RATE = 24_000
TTS_SAMPLE_WIDTH = 2  # bytes per sample: int16


class EngineError(Exception):
    """Base for errors caused by the request. Mapped to a 4xx response."""


class EngineNotLoaded(EngineError):
    """A method was called before ``load()`` finished."""


class UnknownVoice(EngineError):
    """The requested voice id is not in ``voices()``."""


class UnsupportedLanguage(EngineError):
    """The requested language is not in ``languages``."""


@dataclass(frozen=True, slots=True)
class AudioInput:
    """Decoded audio ready for an STT engine.

    ``samples`` is mono float32 at ``sample_rate`` Hz. The API layer always
    produces :data:`STT_SAMPLE_RATE`; engines may assert on it.
    """

    samples: NDArray[np.float32]
    sample_rate: int = STT_SAMPLE_RATE

    @property
    def duration_s(self) -> float:
        return len(self.samples) / self.sample_rate


@dataclass(frozen=True, slots=True)
class Segment:
    """A timed span of a transcript. Times are seconds from the start of input."""

    start: float
    end: float
    text: str


@dataclass(frozen=True, slots=True)
class Transcript:
    """Result of transcription.

    ``text`` is the full, whitespace-normalized transcript. ``segments`` may be
    empty if the engine has no timing information. ``language`` is the BCP-47
    code the engine detected or was told to use, or ``None`` if unknown.
    """

    text: str
    language: str | None = None
    duration_s: float = 0.0
    segments: tuple[Segment, ...] = ()


@dataclass(frozen=True, slots=True)
class Voice:
    """A selectable TTS voice.

    ``id`` is what clients pass as ``voice`` (for example ``af_heart``);
    ``language`` is a BCP-47 code such as ``en-US``.
    """

    id: str
    name: str
    language: str
    gender: Literal["female", "male", "neutral"] | None = None


@dataclass(frozen=True, slots=True)
class PCMChunk:
    """A piece of synthesized audio: mono int16 little-endian PCM.

    Chunks are emitted in playback order and can be concatenated directly.
    ``sample_rate`` is always :data:`TTS_SAMPLE_RATE` for engines shipped with
    voiced; the field exists so clients never have to assume.
    """

    data: bytes
    sample_rate: int = TTS_SAMPLE_RATE

    @property
    def duration_s(self) -> float:
        return len(self.data) / TTS_SAMPLE_WIDTH / self.sample_rate


class STTStreamSession(ABC):
    """One live transcription. Obtained from :meth:`STTEngine.stream`.

    Lifecycle: ``add_audio`` any number of times, ``partial`` at any point in
    between, then exactly one ``finalize``. After ``finalize`` the session is
    closed and further calls raise :class:`EngineError`. Use the session as an
    async context manager so it is closed on error or cancellation; exiting
    without ``finalize`` discards the audio.

    The server holds an inference slot for the whole session, so engines may
    keep model state (encoder cache, context) inside the session. With
    :attr:`STTEngine.max_streams` at 1 (the default) that slot is the
    engine's only one; above 1 up to that many sessions share the engine, and
    the state of each must be its own.
    """

    @abstractmethod
    async def add_audio(self, samples: NDArray[np.float32]) -> None:
        """Append mono float32 samples at :data:`STT_SAMPLE_RATE`.

        May run the encoder on the new audio; may also just buffer. Must
        return promptly (tens of milliseconds) so real-time input keeps up.
        """

    @abstractmethod
    def partial(self) -> str:
        """Best current hypothesis for everything added so far. Never blocks.

        Text may change on later calls. Return ``""`` if nothing is decoded.
        """

    @abstractmethod
    async def finalize(self) -> Transcript:
        """Flush, return the final transcript, and close the session.

        Called when the transport signals end of input (the streaming
        WebSocket client's ``end`` message or the Wyoming ``AudioStop``); the
        engine never ends a turn on its own. Target latency: 50 ms after that
        signal, 150 ms at worst (ADR 0008).
        """

    async def close(self) -> None:
        """Release resources without producing a transcript. Idempotent."""
        return None

    async def __aenter__(self) -> STTStreamSession:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()


class _EngineMeta(ABC):
    """Metadata every engine declares as class attributes.

    ``name`` is the registry key and the ``engine`` value in config.
    ``model_id`` is the default model (a Hugging Face repo id or a local path);
    the configured ``model`` overrides it per instance. ``languages`` are
    BCP-47 codes the engine can handle; the server intersects them with the
    configured list. ``license`` names the model weights' license so the
    server can show it in ``/v1/models`` and the status page.
    """

    name: str
    model_id: str
    languages: list[str]
    license: str

    def __init__(self, model_id: str | None = None) -> None:
        cls = type(self)
        missing = [k for k in ("name", "model_id", "languages", "license") if not hasattr(cls, k)]
        if missing:
            raise TypeError(f"{cls.__name__} must declare class attributes: {', '.join(missing)}")
        if model_id:
            self.model_id = model_id


class STTEngine(_EngineMeta):
    """Speech-to-text engine.

    Implementations must declare ``name``, ``model_id``, ``languages`` and
    ``license`` as class attributes, and may accept extra keyword arguments in
    ``__init__`` for engine-specific options (the registry passes ``model_id``
    only).
    """

    max_streams: int = 1
    """Stream sessions the engine can have open at once. The server opens at
    most ``min(stt.streams, max_streams)``; the next waits for one to end. An
    engine that raises it interleaves the sessions' work itself."""

    @abstractmethod
    async def load(self) -> None:
        """Download if needed, load the model, and run a warm-up inference.

        Called once at startup before the service reports healthy. May take
        seconds to minutes; must raise on failure rather than loading lazily.
        Runs while the server is already answering ``/health``, so all
        blocking work goes through ``voiced.threads.run_blocking``; blocking
        the event loop here stalls the health endpoint.
        """

    @abstractmethod
    async def transcribe(self, audio: AudioInput, language: str | None = None) -> Transcript:
        """Transcribe a complete utterance.

        ``language`` is a BCP-47 hint; ``None`` means auto-detect or the
        engine's default. Raise :class:`UnsupportedLanguage` if the hint is not
        in ``languages``. Silence or empty input returns an empty transcript,
        not an error.
        """

    @abstractmethod
    async def stream(self, language: str | None = None) -> STTStreamSession:
        """Open a streaming session. Cheap; must not block on the GPU."""


class TTSEngine(_EngineMeta):
    """Text-to-speech engine.

    Same metadata requirements as :class:`STTEngine`.
    """

    @abstractmethod
    async def load(self) -> None:
        """Download if needed, load the model and voices, and warm up.

        Called once at startup before the service reports healthy. Same
        rule as :meth:`STTEngine.load`: blocking work goes through
        ``voiced.threads.run_blocking``.
        """

    @abstractmethod
    async def voices(self) -> list[Voice]:
        """All selectable voices. Stable after ``load()``; may be cached."""

    @abstractmethod
    def synthesize(self, text: str, voice: str, speed: float = 1.0) -> AsyncIterator[PCMChunk]:
        """Synthesize ``text`` and yield audio as it is produced.

        ``text`` is already normalized by the server (numbers, acronyms,
        markdown stripped) but not split; engines decide chunking (sentence by
        sentence is the norm). The first chunk should arrive within about
        200 ms for a warm engine. ``voice`` must be an id from ``voices()``;
        raise :class:`UnknownVoice` before yielding anything otherwise.
        ``speed`` is a multiplier (1.0 is normal); engines that cannot change
        speed may ignore it.

        Must be an async generator (``async def`` containing ``yield``) so the
        server can stop it early with ``aclose()`` when the client disconnects.
        """
