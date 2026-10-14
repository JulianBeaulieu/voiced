"""Parakeet TDT 0.6B v3 speech-to-text through ``parakeet-mlx``.

Batch: :meth:`ParakeetEngine.transcribe` runs the log-mel front end and the
TDT decoder over the whole utterance in one call and returns sentence
segments with timestamps.

Streaming: :meth:`ParakeetEngine.stream` wraps ``transcribe_stream`` with
:data:`CONTEXT_SIZE`. Three properties of the library, measured in
docs/decisions/0004-stt-engine-parakeet.md, are hidden inside
:class:`_ChunkedStream` and nowhere else:

* Every call to its ``add_audio`` re-encodes and re-decodes the whole retained
  window (up to 20 s), which costs 150 to 300 ms per call regardless of how
  much audio the call adds. Feeding 100 ms chunks one by one runs slower than
  real time.
* The log-mel is normalized per call, so calls with less than about
  :data:`MIN_PASS_S` of audio produce worse text.
* Calls with fewer than :data:`LIBRARY_MIN_SAMPLES` samples read past the
  buffer and produce NaN features.

So the session never calls the encoder from ``add_audio``. It buffers, and one
background task feeds the adapter in a worker thread; the adapter runs encoder
passes of :data:`MIN_PASS_S` to :data:`MAX_PASS_S`. When the speaker goes
quiet (a chunk with no frame above :data:`SILENCE_FLOOR_DBFS` after speech)
the buffered tail is encoded immediately, during the endpointer's silence
window, so ``finalize()`` normally has nothing left to encode. ``partial()``
returns the last decoded hypothesis.

Entering the library's stream context switches the model to local attention
globally; :meth:`ParakeetStreamSession.close` restores it. The engine refuses
a second session or a batch call while one is open, and the server holds the
engine's inference slot until the session's context manager exits.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import numpy as np
from numpy.typing import NDArray

from voiced.engines.base import (
    STT_SAMPLE_RATE,
    AudioInput,
    EngineError,
    EngineNotLoaded,
    Segment,
    STTEngine,
    STTStreamSession,
    Transcript,
    UnsupportedLanguage,
)
from voiced.threads import run_blocking

__all__ = ["PARAKEET_V3_LANGUAGES", "ParakeetEngine", "ParakeetStreamSession"]

log = logging.getLogger(__name__)

# The 25 languages of parakeet-tdt-0.6b-v3 (NVIDIA model card), English first.
PARAKEET_V3_LANGUAGES: list[str] = [
    "en",
    "bg",
    "cs",
    "da",
    "de",
    "el",
    "es",
    "et",
    "fi",
    "fr",
    "hr",
    "hu",
    "it",
    "lt",
    "lv",
    "mt",
    "nl",
    "pl",
    "pt",
    "ro",
    "ru",
    "sk",
    "sl",
    "sv",
    "uk",
]

# All of these are measurements, not derivations: see ADR 0004 before changing.
CONTEXT_SIZE: tuple[int, int] = (256, 256)
"""Local-attention context in encoder frames (80 ms each). Smaller is not faster."""
CONTEXT_DEPTH = 1
MIN_PASS_S = 0.32
"""Shortest encoder pass; shorter calls degrade the library's per-call mel normalization."""
MAX_PASS_S = 0.64
"""Longest encoder pass. At least ``2 * MIN_PASS_S`` so a backlog splits into even passes."""
WARMUP_S = 1.0
LIBRARY_MIN_SAMPLES = 257
"""``n_fft // 2 + 1``: parakeet-mlx's reflect padding reads out of bounds below this."""
SILENCE_FLOOR_DBFS = -60.0
"""Input with no 32 ms frame above this RMS is silence: nothing to transcribe, and
the model hallucinates short words ("Yeah.") on near-silent noise."""
SILENCE_FLOOR = 10 ** (SILENCE_FLOOR_DBFS / 20)
GATE_FRAME_SAMPLES = 512

FLUSH_TAIL_S = 0.096
"""A chunk whose last 96 ms (three gate frames) are silent flushes the buffered speech."""

_MIN_PASS = int(MIN_PASS_S * STT_SAMPLE_RATE)
_FLUSH_TAIL = int(FLUSH_TAIL_S * STT_SAMPLE_RATE)
_MAX_PASS = int(MAX_PASS_S * STT_SAMPLE_RATE)
if _MAX_PASS < 2 * _MIN_PASS:
    raise AssertionError("MAX_PASS_S must be at least 2 * MIN_PASS_S")

ToArray = Callable[[NDArray[np.float32]], Any]


class ParakeetEngine(STTEngine):
    name = "parakeet"
    model_id = "mlx-community/parakeet-tdt-0.6b-v3"
    languages = list(PARAKEET_V3_LANGUAGES)
    license = "CC-BY-4.0"

    def __init__(self, model_id: str | None = None) -> None:
        super().__init__(model_id)
        self._model: Any = None
        self._to_array: ToArray = _identity
        self._session: ParakeetStreamSession | None = None
        self.load_seconds = 0.0
        self.warmup_seconds = 0.0

    @classmethod
    def normalize_language(cls, language: str | None) -> str | None:
        """``"en-US"`` to ``"en"``; raise :class:`UnsupportedLanguage` if not supported."""
        if language is None:
            return None
        code = language.strip().lower().replace("_", "-").split("-")[0]
        if code not in cls.languages:
            raise UnsupportedLanguage(language)
        return code

    async def load(self) -> None:
        await run_blocking(self._load_blocking)

    def _load_blocking(self) -> None:
        import mlx.core as mx
        from parakeet_mlx import from_pretrained

        t0 = time.perf_counter()
        log.info("loading STT model %s", self.model_id)
        model = from_pretrained(self.model_id)
        t1 = time.perf_counter()
        # Warm up both paths with a second of silence so the first real
        # request does not pay for Metal kernel compilation, and so a broken
        # GPU path fails here, before the service reports healthy.
        silence = np.zeros(int(WARMUP_S * STT_SAMPLE_RATE), dtype=np.float32)
        _generate(model, silence)
        warm = _ChunkedStream(model, mx.array)
        try:
            warm.warm_up(silence)
        finally:
            warm.close()
        self._model = model
        self._to_array = mx.array
        self.load_seconds = t1 - t0
        self.warmup_seconds = time.perf_counter() - t1
        log.info(
            "STT model ready: load %.1fs, warm-up %.1fs", self.load_seconds, self.warmup_seconds
        )

    def _require_idle(self) -> Any:
        if self._model is None:
            raise EngineNotLoaded(self.name)
        if self._session is not None:
            raise EngineError("a stream session is open on this engine")
        return self._model

    async def transcribe(self, audio: AudioInput, language: str | None = None) -> Transcript:
        model = self._require_idle()
        lang = self.normalize_language(language)
        if audio.sample_rate != STT_SAMPLE_RATE:
            raise EngineError(f"expected {STT_SAMPLE_RATE} Hz audio, got {audio.sample_rate} Hz")
        samples = _check_samples(audio.samples)
        duration = len(samples) / STT_SAMPLE_RATE
        if len(samples) < LIBRARY_MIN_SAMPLES or _is_silent(samples):
            return Transcript(text="", language=lang, duration_s=duration)
        result = await run_blocking(_generate, model, samples)
        return _to_transcript(result, lang, duration)

    async def stream(self, language: str | None = None) -> STTStreamSession:
        model = self._require_idle()
        lang = self.normalize_language(language)
        session = ParakeetStreamSession(self, _ChunkedStream(model, self._to_array), lang)
        self._session = session
        return session

    def _session_closed(self, session: ParakeetStreamSession) -> None:
        if self._session is session:
            self._session = None


class _ChunkedStream:
    """Synchronous adapter over the library's ``StreamingParakeet``.

    Owns the audio buffer, pass sizing, zero padding and the silence flush, so
    this class is the only thing that changes when ``parakeet-mlx`` changes
    its streaming code. ``feed``, ``flush``, ``warm_up`` and ``close`` take
    one lock: ``close`` therefore waits for a pass that is still running in
    another thread instead of exiting the stream context underneath it.
    """

    def __init__(self, model: Any, to_array: ToArray) -> None:
        self._model = model
        self._to_array = to_array
        self._stream: Any = None
        self._lock = threading.Lock()
        self._buffer: list[NDArray[np.float32]] = []
        self._buffered = 0
        self._buffer_has_speech = False
        self._speech_seen = False
        self.text = ""
        self.result: Any = None
        self.passes = 0

    @property
    def buffered_s(self) -> float:
        return self._buffered / STT_SAMPLE_RATE

    def feed(self, samples: NDArray[np.float32]) -> bool:
        """Buffer ``samples`` and run the encoder passes that are due.

        A pass runs for every :data:`MIN_PASS_S` to :data:`MAX_PASS_S` of
        buffered speech. A chunk that ends in silence (its last
        :data:`FLUSH_TAIL_S`) flushes the buffered speech at once, zero-padded
        to the minimum, so the hypothesis is complete by the time the
        endpointer's silence window elapses. Pure silence is never encoded.
        Returns whether a pass ran.
        """
        with self._lock:
            if len(samples) == 0:
                return False
            self._buffer.append(samples)
            self._buffered += len(samples)
            if not _is_silent(samples):
                self._speech_seen = True
                self._buffer_has_speech = True
            if not self._buffer_has_speech:
                # Silence is never encoded on its own; keep at most one pass of
                # it as lead-in so an open microphone cannot grow the buffer.
                self._trim_buffer(_MAX_PASS)
                return False
            if self._buffered >= _MIN_PASS:
                self._run_passes(flush=False)
                return True
            if _is_silent(samples[-_FLUSH_TAIL:]):
                self._run_passes(flush=True)
                return True
            return False

    def flush(self) -> Any:
        """Encode what is still buffered, unless it is all silence.

        Returns the library's ``AlignedResult`` for everything decoded, or
        ``None`` if no speech was ever heard.
        """
        with self._lock:
            if self._buffer_has_speech:
                self._run_passes(flush=True)
            self._buffer = []
            self._buffered = 0
            return self.result if self._speech_seen else None

    def warm_up(self, samples: NDArray[np.float32]) -> None:
        """One pass through the streaming path regardless of the silence gate."""
        with self._lock:
            self._encode(samples)

    def close(self) -> None:
        """Exit the library's stream context, restoring the model's attention mode."""
        with self._lock:
            stream, self._stream = self._stream, None
            if stream is not None:
                stream.__exit__(None, None, None)
            log.debug("stream closed after %d encoder passes", self.passes)

    def _trim_buffer(self, keep: int) -> None:
        if self._buffered <= keep:
            return
        buf = np.concatenate(self._buffer) if len(self._buffer) > 1 else self._buffer[0]
        self._buffer = [buf[-keep:]]
        self._buffered = keep

    def _run_passes(self, *, flush: bool) -> None:
        while self._buffered >= _MIN_PASS or (flush and self._buffered):
            self._encode(self._take())

    def _take(self) -> NDArray[np.float32]:
        """Pop the next pass: between the minimum and maximum, a backlog split
        evenly, and a final short tail zero-padded to the minimum."""
        n = self._buffered
        buf = np.concatenate(self._buffer) if len(self._buffer) > 1 else self._buffer[0]
        take = n if n <= _MAX_PASS else math.ceil(n / math.ceil(n / _MAX_PASS))
        window, rest = buf[:take], buf[take:]
        self._buffer = [rest] if len(rest) else []
        self._buffered = len(rest)
        self._buffer_has_speech = len(rest) > 0 and not _is_silent(rest)
        if len(window) < _MIN_PASS:
            window = np.concatenate([window, np.zeros(_MIN_PASS - len(window), dtype=np.float32)])
        return window

    def _encode(self, window: NDArray[np.float32]) -> None:
        if self._stream is None:
            self._stream = self._model.transcribe_stream(
                context_size=CONTEXT_SIZE, depth=CONTEXT_DEPTH
            )
            self._stream.__enter__()
        t0 = time.perf_counter()
        self._stream.add_audio(self._to_array(window))
        self.result = self._stream.result
        self.text = _normalize_text(self.result.text)
        self.passes += 1
        log.debug(
            "encoder pass %d: %d samples in %.0f ms",
            self.passes,
            len(window),
            (time.perf_counter() - t0) * 1000,
        )


class ParakeetStreamSession(STTStreamSession):
    """One streaming transcription: a buffer, a consumer task and the adapter.

    ``finalize()`` returns the transcript; the model's attention mode is
    restored by ``close()`` (the context manager's exit), which is when the
    server releases the engine's inference slot. Use ``async with``.
    """

    def __init__(
        self, engine: ParakeetEngine, adapter: _ChunkedStream, language: str | None
    ) -> None:
        self._engine = engine
        self._adapter = adapter
        self._language = language
        # Streaming timestamps are relative to the retained window, which only
        # starts at zero while the utterance fits in the left context.
        self._timestamp_horizon_s = CONTEXT_SIZE[0] * float(adapter._model.time_ratio)
        self._pending: list[NDArray[np.float32]] = []
        self._consumer: asyncio.Task[None] | None = None
        self._failure: BaseException | None = None
        self._closed = False
        self.samples_added = 0

    @property
    def passes(self) -> int:
        return self._adapter.passes

    @property
    def backlog_s(self) -> float:
        """Seconds of audio added but not yet encoded (pending plus buffered)."""
        pending = sum(len(a) for a in self._pending) / STT_SAMPLE_RATE
        return pending + self._adapter.buffered_s

    async def add_audio(self, samples: NDArray[np.float32]) -> None:
        self._check_open()
        arr = _check_samples(samples)
        if len(arr) == 0:
            return
        self._pending.append(arr)
        self.samples_added += len(arr)
        if self._consumer is None or self._consumer.done():
            self._consumer = asyncio.create_task(self._consume())

    async def _consume(self) -> None:
        try:
            while self._pending:
                chunk = (
                    np.concatenate(self._pending) if len(self._pending) > 1 else self._pending[0]
                )
                self._pending = []
                await run_blocking(self._adapter.feed, chunk)
        except Exception as exc:
            self._failure = exc
            raise

    def partial(self) -> str:
        return self._adapter.text

    async def finalize(self) -> Transcript:
        self._check_open()
        self._closed = True
        await self._drain()
        result = await run_blocking(self._adapter.flush)
        duration = self.samples_added / STT_SAMPLE_RATE
        if result is None:
            return Transcript(text="", language=self._language, duration_s=duration)
        transcript = _to_transcript(result, self._language, duration)
        if duration > self._timestamp_horizon_s:
            transcript = replace(transcript, segments=())
        return transcript

    async def close(self) -> None:
        self._closed = True
        try:
            task = self._consumer
            if task is not None:
                task.cancel()  # pending audio is discarded; the running pass completes
                await asyncio.wait({task})
        finally:
            try:
                await run_blocking(self._adapter.close)
            finally:
                self._engine._session_closed(self)

    def _check_open(self) -> None:
        if self._failure is not None:
            raise self._failure
        if self._closed:
            raise EngineError("stream session is closed")

    async def _drain(self) -> None:
        """Wait for the consumer; re-raise an encoder failure, but never swallow a cancel."""
        task = self._consumer
        if task is None:
            return
        await asyncio.wait({task})
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                raise exc


def _generate(model: Any, samples: NDArray[np.float32]) -> Any:
    """Blocking batch transcription. Returns the library's ``AlignedResult``."""
    import mlx.core as mx
    from parakeet_mlx.audio import get_logmel

    mel = get_logmel(mx.array(samples), model.preprocessor_config)
    return model.generate(mel)[0]


def _identity(samples: NDArray[np.float32]) -> Any:
    return samples


def _peak_frame_rms(samples: NDArray[np.float32]) -> float:
    """The loudest 32 ms frame, so long silence cannot average quiet speech away."""
    n = len(samples)
    if n == 0:
        return 0.0
    full = n // GATE_FRAME_SAMPLES * GATE_FRAME_SAMPLES
    peak = 0.0
    if full:
        frames = samples[:full].reshape(-1, GATE_FRAME_SAMPLES)
        peak = float(np.sqrt(np.mean(np.square(frames, dtype=np.float32), axis=1)).max())
    if full < n:
        tail = samples[full:]
        peak = max(peak, float(np.sqrt(np.mean(np.square(tail, dtype=np.float32)))))
    return peak


def _is_silent(samples: NDArray[np.float32]) -> bool:
    return _peak_frame_rms(samples) < SILENCE_FLOOR


def _check_samples(samples: object) -> NDArray[np.float32]:
    arr = np.asarray(samples)
    if arr.ndim != 1:
        raise EngineError(f"expected mono 1-D samples, got shape {arr.shape}")
    if not np.issubdtype(arr.dtype, np.floating):
        raise EngineError(f"expected float32 samples, got {arr.dtype}")
    return np.ascontiguousarray(arr, dtype=np.float32)


def _normalize_text(text: str) -> str:
    return " ".join(text.split())


def _to_transcript(result: Any, language: str | None, duration_s: float) -> Transcript:
    segments: list[Segment] = []
    for sentence in result.sentences:
        text = _normalize_text(sentence.text)
        if not text:
            continue
        start = max(0.0, float(sentence.start))
        end = max(start, float(sentence.end))
        if duration_s > 0:
            end = min(end, duration_s)
            start = min(start, end)
        segments.append(Segment(start=round(start, 3), end=round(end, 3), text=text))
    return Transcript(
        text=_normalize_text(result.text),
        language=language,
        duration_s=duration_s,
        segments=tuple(segments),
    )
