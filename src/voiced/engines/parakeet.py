"""Parakeet TDT 0.6B v3 speech-to-text through ``parakeet-mlx``.

Batch: :meth:`ParakeetEngine.transcribe` runs the log-mel front end and the
TDT decoder over the whole utterance in one call and returns sentence
segments with timestamps.

Streaming: :meth:`ParakeetEngine.stream` wraps ``transcribe_stream`` with
:data:`CONTEXT_SIZE`. Three properties of the library, measured in
docs/decisions/0008-stt-engine-parakeet.md, are hidden inside
:class:`_ChunkedStream` and nowhere else:

* Every call to its ``add_audio`` re-encodes and re-decodes the whole retained
  window (up to 20 s), which costs 150 to 300 ms per call regardless of how
  much audio the call adds. Feeding 100 ms chunks one by one runs slower than
  real time.
* The log-mel is normalized per call, so short calls produce worse text, very
  short ones corrupt the encoder cache, and the transcript depends on where
  the call boundaries fall.
* Calls with fewer than :data:`LIBRARY_MIN_SAMPLES` samples read past the
  buffer and produce NaN features.

So the session never calls the encoder from ``add_audio``. It buffers, and one
background task feeds the adapter in a worker thread. Nothing is encoded until
the speech-start detector fires (then from :data:`PRE_ROLL_S` before it), and
after that the adapter runs encoder calls of exactly :data:`PASS_S` on a fixed
grid. When the speaker goes quiet (the incoming audio drops
:data:`END_OF_SPEECH_DROP_DB` below the loudest speech heard) everything
buffered is encoded at once, speculatively, during the endpointer's silence
window, so ``finalize()`` normally has nothing left to encode; if speech
resumes, the speculative call is undone. ``partial()`` returns the last
decoded hypothesis.

Entering the library's stream context switches the model to local attention
globally; :meth:`ParakeetStreamSession.close` restores it. The engine refuses
a second session or a batch call while one is open, and the server holds the
engine's inference slot until the session's context manager exits.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray

from voiced.audio import dbfs_to_amplitude, frame_rms
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
from voiced.vad import Endpointer

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

# All of these are measurements, not derivations: see ADR 0008 before changing.
CONTEXT_SIZE: tuple[int, int] = (256, 256)
"""Local-attention context in encoder frames (80 ms each). Smaller is not faster."""
CONTEXT_DEPTH = 1
PASS_S = 0.40
"""Every regular encoder call is exactly this much audio, at fixed offsets from the
start of speech, so the pass boundaries (and with them the transcript, since the
library normalizes the log-mel per call) do not depend on how audio was chunked or
on how busy the GPU was. Calls shorter than about 320 ms corrupt the encoder cache;
speculative windows shorter than this are zero-padded to it."""
WARMUP_S = 1.0
LIBRARY_MIN_SAMPLES = 257
"""``n_fft // 2 + 1``: parakeet-mlx's reflect padding reads out of bounds below this,
so a flush never encodes a window shorter than this."""
SILENCE_FLOOR_DBFS = -60.0
"""Absolute floor. Input with no 32 ms frame above this RMS is silence: nothing to
transcribe, and the model hallucinates short words ("Yeah.") on near-silent noise."""
SILENCE_FLOOR = dbfs_to_amplitude(SILENCE_FLOOR_DBFS)
END_OF_SPEECH_DROP_DB = 35.0
"""A tail this far below the loudest speech frame of the session counts as a pause,
floored at :data:`SILENCE_FLOOR_DBFS`. Relative so a real microphone noise floor
(around -55 dBFS) still triggers the end-of-turn flush."""
END_OF_SPEECH_DROP = dbfs_to_amplitude(-END_OF_SPEECH_DROP_DB)
FLUSH_TAIL_S = 0.032
"""One 32 ms gate frame: a chunk whose final frame is quiet flushes the buffered
speech. One frame keeps the worst-case finalize latency low across chunk alignments."""
PRE_ROLL_S = 0.3
"""Audio kept from before the speech-start detector fires. Nothing earlier in the
session is encoded: a pass of room noise alone looks like speech to the library's
per-call mel normalization and adds invented words."""
MAX_BACKLOG_S = 30.0
"""A client streaming faster than real time must not grow the buffer without bound
while holding the only inference slot; ``add_audio`` rejects audio past this."""

HOLD_MAX_S = 2.5
"""Quiet audio held after a speculative flush before it is replayed anyway."""

_PASS = int(PASS_S * STT_SAMPLE_RATE)
_FLUSH_TAIL = int(FLUSH_TAIL_S * STT_SAMPLE_RATE)
_MAX_BACKLOG = int(MAX_BACKLOG_S * STT_SAMPLE_RATE)
_PRE_ROLL = int(PRE_ROLL_S * STT_SAMPLE_RATE)
_ONSET_HISTORY = _PRE_ROLL + int(0.2 * STT_SAMPLE_RATE)  # covers min_speech_ms
_MAX_HELD = int(HOLD_MAX_S * STT_SAMPLE_RATE)

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
        self._onset: Endpointer | None = None

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
        load_s = time.perf_counter() - t0
        # Warm up both paths with a second of silence so the first real
        # request does not pay for Metal kernel compilation, and so a broken
        # GPU path fails here, before the service reports healthy.
        t1 = time.perf_counter()
        silence = np.zeros(int(WARMUP_S * STT_SAMPLE_RATE), dtype=np.float32)
        _generate(model, silence)
        warm = _ChunkedStream(model, mx.array)
        try:
            warm.warm_up(silence)
        finally:
            warm.close()
        self._model = model
        self._to_array = mx.array
        log.info("STT model ready: load %.1fs, warm-up %.1fs", load_s, time.perf_counter() - t1)

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
        onset = self._onset_detector()
        onset.reset()
        adapter = _ChunkedStream(model, self._to_array, onset=onset)
        session = ParakeetStreamSession(self, adapter, lang)
        self._session = session
        return session

    def _onset_detector(self) -> Endpointer:
        """The speech-start detector streams are gated on: Silero, reused across
        sessions (the engine runs one at a time) and reset for each."""
        if self._onset is None:
            self._onset = Endpointer(silence_ms=200)
        return self._onset

    def _session_closed(self, session: ParakeetStreamSession) -> None:
        if self._session is session:
            self._session = None


class _ChunkedStream:
    """Synchronous adapter over the library's ``StreamingParakeet``.

    Owns the audio buffer, pass sizing, the end-of-speech flush and the
    timestamp horizon, so this class is the only thing that changes when
    ``parakeet-mlx`` changes its streaming code. ``feed``, ``flush``,
    ``warm_up`` and ``close`` take one lock, so ``close`` waits for a pass
    still running in another thread instead of exiting the stream context
    underneath it.

    The end-of-speech flush is speculative. On a pause the adapter snapshots
    the library's stream state, then encodes everything buffered in one call,
    zero-padded to :data:`PASS_S` if shorter, so ``flush()`` usually has
    nothing left to do. If speech resumes instead, the snapshot is restored
    and the held audio replayed on the regular grid, so the padding never
    becomes part of the transcript and the state is exactly what it would
    have been with no flush.
    """

    def __init__(self, model: Any, to_array: ToArray, onset: Endpointer | None = None) -> None:
        self._model = model
        self._to_array = to_array
        self._horizon_s = CONTEXT_SIZE[0] * float(model.time_ratio)
        self._stream: Any = None
        self._lock = threading.Lock()
        self._buffer = np.zeros(0, dtype=np.float32)
        self._buffer_has_speech = False
        self._speech_seen = False
        self._loudest = 0.0
        self._speculation: _Speculation | None = None
        self.text = ""
        self.result: Any = None
        self.passes = 0
        self.rollbacks = 0
        # Speech-start gate: ``None`` means the stream starts at once.
        self._onset = onset
        self._started = onset is None
        self._history = np.zeros(0, dtype=np.float32)
        self._history_start = 0

    @property
    def buffered_s(self) -> float:
        return len(self._buffer) / STT_SAMPLE_RATE

    def feed(self, samples: NDArray[np.float32]) -> bool:
        """Buffer ``samples`` and run the encoder passes that are due.

        A regular call runs for every :data:`PASS_S` of buffered speech. When
        the incoming audio drops to a pause (its last :data:`FLUSH_TAIL_S`
        falls :data:`END_OF_SPEECH_DROP_DB` below the loudest speech seen) the
        whole buffer is flushed speculatively in one call, so the hypothesis
        is complete by the time the endpointer's silence window elapses.
        Silence is never encoded on its own. Returns whether a call ran.
        """
        with self._lock:
            return self._feed(samples, replay=False)

    def _feed(self, samples: NDArray[np.float32], *, replay: bool) -> bool:
        if len(samples) == 0:
            return False
        if not self._started:
            gated = self._await_speech_start(samples)
            if gated is None:
                return False
            samples = gated
        peak = _loudest_frame(samples)
        self._loudest = max(self._loudest, peak)
        speech = peak >= self._speech_floor()
        spec = self._speculation
        if spec is not None:
            if not speech:
                # The pause goes on: hold the quiet audio in case speech resumes.
                spec.held.append(samples)
                spec.held_samples += len(samples)
                if spec.held_samples <= _MAX_HELD:
                    return False
            # Speech resumed (or the pause outgrew what is worth holding): undo
            # the speculative pass and replay the real audio exactly as it would
            # have been fed with no flush, so the pass boundaries, and with them
            # the transcript, are the same.
            for chunk in self._rollback():
                self._feed(chunk, replay=True)
            if not speech:
                return True
        self._buffer = np.concatenate([self._buffer, samples])
        if speech:
            self._speech_seen = True
            self._buffer_has_speech = True
        if not self._buffer_has_speech:
            # A long pause after speech: keep at most one pass of it, so an
            # open microphone cannot grow the buffer.
            self._buffer = self._buffer[-_PASS:]
            return False
        if len(self._buffer) >= _PASS:
            self._encode_due()
            return True
        if not replay and self._tail_is_pause(samples):
            self._speculate()
            return True
        return False

    def flush(self) -> Any:
        """Encode whatever speech is still buffered and return the trimmed result.

        After a speculative flush that no speech followed, the speculative
        pass already is the final hypothesis and the held pause is dropped.
        Returns the library's ``AlignedResult``, with segments past the
        timestamp horizon dropped, or ``None`` if no speech was ever heard.
        """
        with self._lock:
            if self._speculation is not None:
                self._speculation = None
            elif self._buffer_has_speech:
                self._encode_due()
                if len(self._buffer):
                    self._encode(_pad(self._buffer))
            self._buffer = np.zeros(0, dtype=np.float32)
            self._buffer_has_speech = False
            if not self._speech_seen or self.result is None:
                return None
            return self._trimmed(self.result)

    def warm_up(self, samples: NDArray[np.float32]) -> None:
        """One pass through the streaming path regardless of the silence gate."""
        with self._lock:
            self._encode(samples)

    def close(self) -> None:
        """Exit the library's stream context, restoring the model's attention mode."""
        with self._lock:
            stream, self._stream = self._stream, None
            self._speculation = None
            if stream is not None:
                stream.__exit__(None, None, None)
            log.debug(
                "stream closed after %d encoder passes, %d rolled back",
                self.passes,
                self.rollbacks,
            )

    def _await_speech_start(self, samples: NDArray[np.float32]) -> NDArray[np.float32] | None:
        """Hold audio until the detector reports speech, then return it from
        :data:`PRE_ROLL_S` before the onset; ``None`` while still waiting."""
        onset = self._onset
        if onset is None:
            self._started = True
            return samples
        self._history = np.concatenate([self._history, samples])
        start = next((e for e in onset.feed(samples) if e.kind == "speech_start"), None)
        if start is None:
            drop = len(self._history) - _ONSET_HISTORY
            if drop > 0:
                self._history = self._history[drop:]
                self._history_start += drop
            return None
        first = max(round(start.speech_start_s * STT_SAMPLE_RATE) - _PRE_ROLL, self._history_start)
        audio = self._history[first - self._history_start :]
        self._history = np.zeros(0, dtype=np.float32)
        self._started = True
        return audio

    def _speech_floor(self) -> float:
        """Frames below this are not speech: :data:`END_OF_SPEECH_DROP_DB` under the
        loudest frame of the session, and never below :data:`SILENCE_FLOOR_DBFS`.

        Relative, so the noise floor of a real microphone after the speaker
        stops neither re-arms the buffer nor blocks the end-of-turn flush."""
        return max(self._loudest * END_OF_SPEECH_DROP, SILENCE_FLOOR)

    def _tail_is_pause(self, chunk: NDArray[np.float32]) -> bool:
        return _loudest_frame(chunk[-_FLUSH_TAIL:]) < self._speech_floor()

    def _encode_due(self) -> None:
        """Encode every full :data:`PASS_S` currently buffered, on the fixed grid."""
        while len(self._buffer) >= _PASS:
            window, self._buffer = self._buffer[:_PASS], self._buffer[_PASS:]
            self._encode(window)
        self._buffer_has_speech = (
            len(self._buffer) > 0 and _loudest_frame(self._buffer) >= self._speech_floor()
        )

    def _speculate(self) -> None:
        """Snapshot the stream, then encode everything buffered in one call."""
        stream = self._ensure_stream()
        self._speculation = _Speculation(
            state=_snapshot(stream, self._to_array),
            audio=self._buffer,
            text=self.text,
            result=self.result,
        )
        window, self._buffer = self._buffer, np.zeros(0, dtype=np.float32)
        self._buffer_has_speech = False
        self._encode(_pad(window))

    def _rollback(self) -> list[NDArray[np.float32]]:
        """Undo a speculative flush; returns the held chunks to replay in order."""
        spec = self._speculation
        if spec is None:
            return []
        _restore(self._ensure_stream(), spec.state)
        self.text, self.result = spec.text, spec.result
        self._buffer = spec.audio
        self._buffer_has_speech = True
        self._speculation = None
        self.rollbacks += 1
        return spec.held

    def _ensure_stream(self) -> Any:
        if self._stream is None:
            self._stream = self._model.transcribe_stream(
                context_size=CONTEXT_SIZE, depth=CONTEXT_DEPTH
            )
            self._stream.__enter__()
        return self._stream

    def _encode(self, window: NDArray[np.float32]) -> None:
        stream = self._ensure_stream()
        t0 = time.perf_counter()
        stream.add_audio(self._to_array(window))
        self.result = stream.result
        self.text = _normalize_text(self.result.text)
        self.passes += 1
        log.debug(
            "encoder pass %d: %d samples in %.0f ms",
            self.passes,
            len(window),
            (time.perf_counter() - t0) * 1000,
        )

    def _trimmed(self, result: Any) -> Any:
        """The library's result with any segment past the timestamp horizon dropped.

        Streaming token times are relative to the retained window, so once the
        utterance outgrows the left context they are no longer absolute.
        """
        if not result.sentences or result.sentences[-1].end <= self._horizon_s:
            return result
        kept = [s for s in result.sentences if s.end <= self._horizon_s]
        return _Result(result.text, kept)


class ParakeetStreamSession(STTStreamSession):
    """One streaming transcription: a buffer, a consumer task and the adapter.

    ``finalize()`` returns the transcript. ``close()`` restores the model's
    attention mode and must run after ``finalize()`` (the engine stays locked
    to this session until then); the context manager's exit does it. Use
    ``async with``.
    """

    def __init__(
        self, engine: ParakeetEngine, adapter: _ChunkedStream, language: str | None
    ) -> None:
        self._engine = engine
        self._adapter = adapter
        self._language = language
        self._pending: list[NDArray[np.float32]] = []
        self._pending_samples = 0
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
        return self._pending_samples / STT_SAMPLE_RATE + self._adapter.buffered_s

    async def add_audio(self, samples: NDArray[np.float32]) -> None:
        self._check_open()
        arr = _check_samples(samples)
        if len(arr) == 0:
            return
        if self._pending_samples + len(arr) > _MAX_BACKLOG:
            raise EngineError(
                f"streaming backlog over {MAX_BACKLOG_S:.0f}s; feed no faster than real time"
            )
        self._pending.append(arr)
        self._pending_samples += len(arr)
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
                self._pending_samples = 0
                await run_blocking(self._adapter.feed, chunk)
        except Exception as exc:
            self._failure = exc

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
        return _to_transcript(result, self._language, duration)

    async def close(self) -> None:
        self._closed = True
        try:
            task = self._consumer
            if task is not None:
                task.cancel()  # pending audio is discarded; the running pass completes
                await asyncio.wait({task})
                if not task.cancelled():
                    task.exception()  # retrieve it so it is never "never retrieved"
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
        """Wait for the consumer to finish the pending audio, then surface any failure."""
        task = self._consumer
        if task is not None:
            await asyncio.wait({task})
        if self._failure is not None:
            raise self._failure


@dataclass(slots=True)
class _Speculation:
    """What a speculative flush needs to undo itself."""

    state: _StreamState
    audio: NDArray[np.float32]
    text: str
    result: Any
    held: list[NDArray[np.float32]] = field(default_factory=list)
    held_samples: int = 0


@dataclass(slots=True)
class _StreamState:
    """A copy of ``StreamingParakeet``'s mutable state.

    The encoder caches are written in place (a ring buffer), so their arrays
    are copied; MLX copies are lazy and cost about 2 ms for every layer. The
    other fields are reassigned by the library rather than mutated, except the
    finalized token list, which is extended.
    """

    caches: list[tuple[Any, Any, Any, int]]
    audio_buffer: Any
    mel_buffer: Any
    decoder_hidden: Any
    last_token: Any
    finalized_tokens: list[Any]
    draft_tokens: list[Any]


def _snapshot(stream: Any, to_array: ToArray) -> _StreamState:
    """Copy the stream's state. ``to_array`` is ``mx.array``, which copies an MLX array."""

    def copy(a: Any) -> Any:
        return None if a is None else to_array(a)

    return _StreamState(
        caches=[(copy(c.keys), copy(c.values), copy(c.conv), c.offset) for c in stream.cache],
        audio_buffer=stream.audio_buffer,
        mel_buffer=stream.mel_buffer,
        decoder_hidden=stream.decoder_hidden,
        last_token=stream.last_token,
        finalized_tokens=list(stream.finalized_tokens),
        draft_tokens=list(stream.draft_tokens),
    )


def _restore(stream: Any, state: _StreamState) -> None:
    for cache, (keys, values, conv, offset) in zip(stream.cache, state.caches, strict=True):
        cache.keys, cache.values, cache.conv, cache.offset = keys, values, conv, offset
    stream.audio_buffer = state.audio_buffer
    stream.mel_buffer = state.mel_buffer
    stream.decoder_hidden = state.decoder_hidden
    stream.last_token = state.last_token
    stream.finalized_tokens = state.finalized_tokens
    stream.draft_tokens = state.draft_tokens


def _pad(window: NDArray[np.float32]) -> NDArray[np.float32]:
    """Zero-pad to :data:`PASS_S`: much shorter encoder calls corrupt the cache."""
    if len(window) >= _PASS:
        return window
    return np.concatenate([window, np.zeros(_PASS - len(window), dtype=np.float32)])


def _generate(model: Any, samples: NDArray[np.float32]) -> Any:
    """Blocking batch transcription. Returns the library's ``AlignedResult``."""
    import mlx.core as mx
    from parakeet_mlx.audio import get_logmel

    mel = get_logmel(mx.array(samples), model.preprocessor_config)
    return model.generate(mel)[0]


def _identity(samples: NDArray[np.float32]) -> Any:
    return samples


def _loudest_frame(samples: NDArray[np.float32]) -> float:
    """RMS of the loudest 32 ms frame; 0.0 for empty input."""
    return float(np.max(frame_rms(samples), initial=0.0))


def _is_silent(samples: NDArray[np.float32]) -> bool:
    """True when no 32 ms frame reaches the absolute silence floor."""
    return _loudest_frame(samples) < SILENCE_FLOOR


def _check_samples(samples: object) -> NDArray[np.float32]:
    arr = np.asarray(samples)
    if arr.ndim != 1:
        raise EngineError(f"expected mono 1-D samples, got shape {arr.shape}")
    if not np.issubdtype(arr.dtype, np.floating):
        raise EngineError(f"expected float32 samples, got {arr.dtype}")
    if not np.all(np.isfinite(arr)):
        raise EngineError("samples must be finite")
    return np.ascontiguousarray(arr, dtype=np.float32)


def _normalize_text(text: str) -> str:
    return " ".join(text.split())


class _Result:
    """A stand-in for the library's ``AlignedResult`` with a subset of sentences."""

    __slots__ = ("sentences", "text")

    def __init__(self, text: str, sentences: list[Any]) -> None:
        self.text = text
        self.sentences = sentences


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
