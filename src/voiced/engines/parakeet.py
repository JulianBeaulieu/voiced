"""Parakeet TDT 0.6B v3 speech-to-text through ``parakeet-mlx``.

Batch: :meth:`ParakeetEngine.transcribe` runs the log-mel front end and the
TDT decoder over the whole utterance in one call and returns sentence
segments with timestamps.

Streaming has three parts, measured and explained in
docs/decisions/0008-stt-engine-parakeet.md:

* :class:`_Planner` decides what to encode and when, from the audio and a
  Silero speech detector alone: nothing before speech starts, encoder calls of
  exactly :data:`PASS_S` on a fixed grid while the speaker talks, and on a
  pause a speculative call over everything buffered, undone if speech resumes.
  It is plain numpy and knows nothing about the library.
* :class:`_LibraryStream` is the only code that touches ``StreamingParakeet``:
  encode one window, checkpoint and restore its state, and report the result
  with timestamps in session time.
* :class:`ParakeetStreamSession` buffers ``add_audio`` (it waits for the
  encoder only when :data:`MAX_BACKLOG_S` is behind) and feeds both from one
  consumer task.

Every encoder call a client waits on (see :meth:`_StepRunner.urgent`) holds
:func:`voiced.gpu.stt_turn`, so Kokoro synthesizing in the TTS service waits
for it (ADR 0019).

Every MLX call of an engine runs on the engine's :class:`ModelThread`. Entering
the library's stream context switches the model to local attention globally;
:meth:`ParakeetStreamSession.close` restores it. The engine refuses a second
session or a batch call while one is open, and the server holds the engine's
inference slot until the session's context manager exits.
"""

from __future__ import annotations

import asyncio
import bisect
import logging
import time
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol

import numpy as np
from numpy.typing import NDArray

from voiced.audio import VAD_FRAME_SAMPLES as _VAD_FRAME
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
from voiced.gpu import stt_turn
from voiced.threads import ModelThread
from voiced.vad import Endpointer, EndpointEvent

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

# Measurements, not derivations: see ADR 0008 before changing any of these.
CONTEXT_SIZE: tuple[int, int] = (64, 64)
"""Local-attention context in encoder frames (80 ms each), left and right. The
right context is what every call re-encodes and re-decodes, so it bounds the cost
of a call however long the utterance; 64 is the smallest that kept the transcript."""
PASS_S = 0.8
"""Every regular encoder call is exactly this much audio, at fixed offsets from the
start of speech, so the pass boundaries (and with them the transcript, since the
library normalizes the log-mel per call) do not depend on how audio was chunked or
on how busy the GPU was. Shorter calls lose words with the context above; windows
shorter than this are zero-padded to it."""
PRE_ROLL_S = 0.3
"""Audio kept from before speech start. Nothing earlier in the session is encoded:
a call of room noise alone looks like speech to the per-call mel normalization and
adds invented words."""
HOLD_MAX_S = 2.5
"""How long a pause after a speculative call is held before it is given up on and
the buffer goes on as if there had been no pause."""
MAX_BACKLOG_S = 30.0
"""Bound on the audio a session holds unencoded. ``add_audio`` waits while the
new chunk would take the backlog past it, so a session that is behind (it waited
for the slot while its client kept streaming, or the client sends faster than
real time) pushes back on its transport instead of growing the buffer. Once the
encoder has caught up the chunk is taken anyway, so the backlog can exceed this
by what the planner holds (under :data:`HOLD_MAX_S` plus a pass). A single chunk
larger than this is rejected."""
FEED_MAX_S = 2.0
"""Most audio one model-thread call feeds. A session catching up runs a burst as
several calls, so ``add_audio`` waits for one call (about three passes) rather
than for the whole burst, and ``close`` is never held up longer than that."""
URGENT_BACKLOG_S = 2 * PASS_S
"""Unencoded audio past which a session is behind, so its encoder calls go first
on the GPU (ADR 0019). A session that keeps up holds under one pass in the
planner plus the few chunks that arrive during a call; this is one more pass."""
WARMUP_S = 1.0
SILENCE_FLOOR_DBFS = -60.0
"""Batch only: input with no 32 ms frame above this RMS is silence, nothing to
transcribe (the model says "Yeah." on near-silent noise)."""
SILENCE_FLOOR = dbfs_to_amplitude(SILENCE_FLOOR_DBFS)
BATCH_CHUNK_S = 20.0
"""Batch input longer than this is transcribed in chunks of this length, overlapping
by :data:`BATCH_OVERLAP_S`. Bounds the MLX peak at about 2.5 GB for any input length
(a 5 minute request peaked at 6.4 GB in one piece); merged text matched the
one-piece transcript at 0.985 to 0.999 word accuracy."""
BATCH_OVERLAP_S = 4.0
LIBRARY_MIN_SAMPLES = 257
"""Batch only, defensive: ``n_fft // 2 + 1``. parakeet-mlx 0.5's reflect padding
reads past the buffer below this (upstream issue to be filed); streaming calls are
always a full pass."""

_SR = STT_SAMPLE_RATE
_PASS = round(PASS_S * _SR)
_PRE_ROLL = round(PRE_ROLL_S * _SR)
_MAX_HELD = round(HOLD_MAX_S * _SR)
_BATCH_CHUNK = round(BATCH_CHUNK_S * _SR)
_BATCH_OVERLAP = round(BATCH_OVERLAP_S * _SR)
_FEED_MAX = round(FEED_MAX_S * _SR)


class ParakeetEngine(STTEngine):
    name = "parakeet"
    model_id = "mlx-community/parakeet-tdt-0.6b-v3"
    languages = list(PARAKEET_V3_LANGUAGES)
    license = "CC-BY-4.0"

    @classmethod
    def download_patterns(cls) -> list[str]:
        """What ``from_pretrained`` reads (``voiced.models``)."""
        return ["config.json", "model.safetensors"]

    def __init__(self, model_id: str | None = None) -> None:
        super().__init__(model_id)
        self._model: Any = None
        self._thread = ModelThread("parakeet")
        self._session: ParakeetStreamSession | None = None
        self._speech: Endpointer | None = None

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
        await self._thread.run(self._load_blocking)

    def _load_blocking(self) -> None:
        from parakeet_mlx import from_pretrained

        from voiced.models import snapshot

        t0 = time.perf_counter()
        log.info("loading STT model %s", self.model_id)
        # The pinned revision's directory, from the cache voiced install filled.
        model = from_pretrained(str(snapshot(self.model_id, self.download_patterns())))
        load_s = time.perf_counter() - t0
        # Warm up both paths so the first real request does not pay for Metal
        # kernel compilation, and so a broken GPU path fails here, before the
        # service reports healthy.
        t1 = time.perf_counter()
        _generate(model, np.zeros(round(WARMUP_S * _SR), dtype=np.float32))
        warm = _LibraryStream(model)
        try:
            warm.encode(np.zeros(_PASS, dtype=np.float32), 0, urgent=False)
        finally:
            warm.close()
        self._model = model
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
        if audio.sample_rate != _SR:
            raise EngineError(f"expected {_SR} Hz audio, got {audio.sample_rate} Hz")
        samples = _check_samples(audio.samples)
        duration = len(samples) / _SR
        if len(samples) < LIBRARY_MIN_SAMPLES or _is_silent(samples):
            return Transcript(text="", language=lang, duration_s=duration)
        result = await self._thread.run(_generate, model, samples)
        return _to_transcript(result, lang, duration)

    async def stream(self, language: str | None = None) -> STTStreamSession:
        model = self._require_idle()
        lang = self.normalize_language(language)
        speech = self._speech_detector()
        speech.reset()
        adapter = _StepRunner(self._new_library(model), _Planner(speech))
        session = ParakeetStreamSession(self, adapter, lang)
        self._session = session
        return session

    def _new_library(self, model: Any) -> _Library:
        return _LibraryStream(model)

    def _speech_detector(self) -> Endpointer:
        """Silero, reused across sessions (the engine runs one at a time) and reset
        for each. Only its speech start and per-frame decisions are used, so its
        silence window does not matter."""
        if self._speech is None:
            self._speech = Endpointer(silence_ms=0)
        return self._speech

    def _session_closed(self, session: ParakeetStreamSession) -> None:
        if self._session is session:
            self._session = None


# --- planning: what to encode, when ------------------------------------------


@dataclass(frozen=True, slots=True)
class _Encode:
    """Encode ``window`` (exactly one pass) that starts at session sample ``start``."""

    window: NDArray[np.float32]
    start: int
    speculative: bool = False
    """The call at a pause, which the hypothesis at the end of speech waits on."""


_Step = _Encode | Literal["checkpoint", "restore"]


class _Planner:
    """Turns incoming audio into encoder calls. Pure numpy; positions are session
    samples (everything fed since the session started).

    * **Speech-start gate.** Nothing is planned until ``speech`` reports speech
      start; then the buffer starts :data:`PRE_ROLL_S` before the onset.
    * **Grid.** Every full :data:`PASS_S` of buffered audio is one call if any of
      its 32 ms frames is speech. A pass with none is never encoded (the model
      invents words in silence): the grid restarts :data:`PRE_ROLL_S` before the
      next speech, and a long pause keeps at most one pass of audio meanwhile.
    * **Pause.** When the latest 32 ms frame is not speech and less than a pass
      is buffered, a checkpoint and one call over the buffer, padded to a pass,
      run at once, so the hypothesis is complete before the endpointer's silence
      window elapses. The buffer is kept. If speech resumes, or the pause outgrows
      :data:`HOLD_MAX_S`, the checkpoint is restored and the buffer goes on as if
      there had been no pause; at finalize the speculative call is the last one.
    """

    def __init__(self, speech: Endpointer) -> None:
        self._speech = speech
        self._started = False
        self._fed = 0
        self._buffer = np.zeros(0, dtype=np.float32)
        self._start = 0  # session position of _buffer[0]
        self._speculating = False
        self._speech_ends: list[int] = []  # where frames judged speech end, from _start on
        # enough history to reach back from a speech-start event to the pre-roll
        self._history = _PRE_ROLL + round(speech.min_speech_s * _SR) + 2 * _VAD_FRAME

    @property
    def buffered(self) -> int:
        return len(self._buffer)

    def feed(self, samples: NDArray[np.float32]) -> list[_Step]:
        before = self._fed
        events = self._detect(samples)
        self._fed += len(samples)
        self._buffer = np.concatenate([self._buffer, samples])
        if not self._started:
            onset = next((e for e in events if e.kind == "speech_start"), None)
            if onset is None:
                self._drop_before(self._fed - self._history)
                return []
            self._started = True
            self._drop_before(round(onset.speech_start_s * _SR) - _PRE_ROLL)
        steps: list[_Step] = []
        if self._speculating:
            if self._last_speech() <= before and self._fed - self._last_speech() <= _MAX_HELD:
                return []  # the pause goes on
            steps.append("restore")
            self._speculating = False
        steps += self._due()
        if self._has_speech() and self._at_pause():
            steps += ["checkpoint", _Encode(_pad(self._buffer), self._start, speculative=True)]
            self._speculating = True
        return steps

    def flush(self) -> list[_Step]:
        """The calls still needed for the final hypothesis."""
        steps: list[_Step] = []
        if not self._speculating:
            while self._has_speech():
                if self._speech_in(self._start):
                    steps.append(_Encode(_pad(self._buffer[:_PASS]), self._start))
                    self._drop_before(self._start + _PASS)
                else:
                    self._skip_to_speech()
        self._speculating = False
        self._drop_before(self._fed)
        return steps

    def _due(self) -> list[_Step]:
        steps: list[_Step] = []
        # A window is decided once the detector has judged all of its frames, so
        # the decision does not depend on how the audio was chunked.
        while self._start + _PASS <= self._heard() and self._has_speech():
            if self._speech_in(self._start):
                steps.append(_Encode(self._buffer[:_PASS], self._start))
                self._drop_before(self._start + _PASS)
            else:
                self._skip_to_speech()
        if not self._has_speech():
            self._drop_before(self._fed - _PASS)
        return steps

    def _detect(self, samples: NDArray[np.float32]) -> list[EndpointEvent]:
        """Feed the detector frame by frame, recording where each speech frame ends."""
        events: list[EndpointEvent] = []
        i, short = 0, self._fed - self._heard()  # samples the detector holds back
        while i < len(samples):
            n = min(_VAD_FRAME - short, len(samples) - i)
            last = self._speech.last_speech_s
            events += self._speech.feed(samples[i : i + n])
            if self._speech.last_speech_s != last:
                self._speech_ends.append(self._last_speech())
            i, short = i + n, 0
        return events

    def _heard(self) -> int:
        """Session position up to which the detector has judged every frame."""
        return round(self._speech.position_s * _SR)

    def _last_speech(self) -> int:
        """Session position where the last frame judged speech ended."""
        return round(self._speech.last_speech_s * _SR)

    def _has_speech(self) -> bool:
        return self._last_speech() > self._start

    def _speech_in(self, start: int) -> bool:
        """Whether the pass starting at ``start`` holds the middle of a speech frame
        (a frame straddling two passes belongs to the one with more of it)."""
        half = _VAD_FRAME // 2
        i = bisect.bisect_left(self._speech_ends, start + half)
        return i < len(self._speech_ends) and self._speech_ends[i] - half < start + _PASS

    def _skip_to_speech(self) -> None:
        """Drop a pass with no speech: the grid restarts :data:`PRE_ROLL_S` before
        the next speech frame, as it does at speech start, so the next call has
        the same lead-in (a call that starts right at a word misreads it)."""
        half = _VAD_FRAME // 2
        i = bisect.bisect_left(self._speech_ends, self._start + half)
        onset = self._speech_ends[i] - _VAD_FRAME
        self._drop_before(max(onset - _PRE_ROLL, self._start + 1))

    def _at_pause(self) -> bool:
        return self._heard() - self._last_speech() >= _VAD_FRAME

    def _drop_before(self, position: int) -> None:
        drop = min(position - self._start, len(self._buffer))
        if drop > 0:
            self._buffer = self._buffer[drop:]
            self._start += drop
            del self._speech_ends[: bisect.bisect_right(self._speech_ends, self._start)]


# --- the library seam ---------------------------------------------------------


class _Library(Protocol):
    """What the adapter needs from the encoder: :class:`_LibraryStream`, or a fake."""

    @property
    def text(self) -> str:
        """The current hypothesis."""

    @property
    def calls(self) -> int:
        """Encoder calls made, undone ones included."""

    def encode(self, window: NDArray[np.float32], start: int, urgent: bool) -> None: ...

    def checkpoint(self) -> object: ...

    def restore(self, checkpoint: object) -> None: ...

    def result(self) -> Any: ...

    def close(self) -> None: ...


@dataclass(slots=True)
class _Checkpoint:
    """``StreamingParakeet``'s mutable state plus ours.

    The encoder caches are ring buffers written in place, so their arrays are
    copied (MLX copies are lazy). The library reassigns its other fields rather
    than mutating them, except the finalized token list, which it extends.
    """

    caches: list[tuple[Any, Any, Any, int]]
    audio_buffer: Any
    mel_buffer: Any
    decoder_hidden: Any
    last_token: Any
    finalized_tokens: list[Any]
    draft_tokens: list[Any]
    starts: int
    finalized: int
    draft: list[Any]
    text: str


class _LibraryStream:
    """The only code that touches ``StreamingParakeet``, so the only code to
    re-check when ``parakeet-mlx`` changes (it is pinned to 0.5).

    ``encode`` takes exactly one pass. Each call of ``n`` samples becomes
    ``n // hop + 1`` mel frames. The library's finalized token times are
    relative to its mel buffer, which it trims to the right context, and its
    draft token times to the end of the finalized region. ``encode`` records
    where each window started in the session and maps every token back to
    session time, so timestamps stay right for any length, after the
    speech-start gate and across dropped pauses.
    """

    def __init__(self, model: Any) -> None:
        import mlx.core as mx

        self._model = model
        self._array = mx.array  # also copies an MLX array
        self._hop_s = model.preprocessor_config.hop_length / _SR
        self._subsampling = int(model.encoder_config.subsampling_factor)
        self._stream: Any = None
        self._starts: list[int] = []  # session position of every encoded window
        self._frames = 0  # mel frames per window
        self._finalized: list[Any] = []  # tokens in session time
        self._draft: list[Any] = []
        self.text = ""
        self.calls = 0

    def encode(self, window: NDArray[np.float32], start: int, urgent: bool) -> None:
        stream = self._open()
        retained = 0 if stream.mel_buffer is None else int(stream.mel_buffer.shape[1])
        base = len(self._starts) * self._frames - retained  # mel frame of the buffer start
        known = len(stream.finalized_tokens)
        t0 = time.perf_counter()
        with stt_turn() if urgent else nullcontext():
            stream.add_audio(self._array(window))
        if not self._frames:  # one window is shorter than the right context: untrimmed
            self._frames = int(stream.mel_buffer.shape[1])
        self._starts.append(start)
        encoded = (retained + self._frames) // self._subsampling  # encoder frames this call
        finalized = max(0, encoded - CONTEXT_SIZE[1]) * self._subsampling
        self._finalized += [self._to_session(t, base) for t in stream.finalized_tokens[known:]]
        self._draft = [self._to_session(t, base + finalized) for t in stream.draft_tokens]
        self.text = _normalize_text(self.result().text)
        self.calls += 1
        log.debug("encoder call %d in %.0f ms", self.calls, (time.perf_counter() - t0) * 1000)

    def result(self) -> Any:
        """The library's ``AlignedResult`` in session time, or ``None`` before any call."""
        if self._stream is None:
            return None
        from parakeet_mlx.alignment import sentences_to_result, tokens_to_sentences

        tokens = self._finalized + self._draft
        return sentences_to_result(
            tokens_to_sentences(tokens, self._stream.decoding_config.sentence)
        )

    def checkpoint(self) -> _Checkpoint:
        stream = self._open()

        def copy(a: Any) -> Any:
            return None if a is None else self._array(a)

        return _Checkpoint(
            caches=[(copy(c.keys), copy(c.values), copy(c.conv), c.offset) for c in stream.cache],
            audio_buffer=stream.audio_buffer,
            mel_buffer=stream.mel_buffer,
            decoder_hidden=stream.decoder_hidden,
            last_token=stream.last_token,
            finalized_tokens=list(stream.finalized_tokens),
            draft_tokens=list(stream.draft_tokens),
            starts=len(self._starts),
            finalized=len(self._finalized),
            draft=self._draft,
            text=self.text,
        )

    def restore(self, checkpoint: object) -> None:
        assert isinstance(checkpoint, _Checkpoint)
        stream = self._open()
        for cache, (keys, values, conv, offset) in zip(
            stream.cache, checkpoint.caches, strict=True
        ):
            cache.keys, cache.values, cache.conv, cache.offset = keys, values, conv, offset
        stream.audio_buffer = checkpoint.audio_buffer
        stream.mel_buffer = checkpoint.mel_buffer
        stream.decoder_hidden = checkpoint.decoder_hidden
        stream.last_token = checkpoint.last_token
        stream.finalized_tokens = checkpoint.finalized_tokens
        stream.draft_tokens = checkpoint.draft_tokens
        del self._starts[checkpoint.starts :]
        del self._finalized[checkpoint.finalized :]
        self._draft = checkpoint.draft
        self.text = checkpoint.text

    def close(self) -> None:
        """Exit the stream context, restoring the model's attention mode."""
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.__exit__(None, None, None)

    def _open(self) -> Any:
        if self._stream is None:
            self._stream = self._model.transcribe_stream(context_size=CONTEXT_SIZE)
            self._stream.__enter__()
        return self._stream

    def _to_session(self, token: Any, base: int) -> Any:
        start = self._session_s(base + token.start / self._hop_s)
        end = self._session_s(base + token.end / self._hop_s)
        return replace(token, start=start, duration=max(0.0, end - start))

    def _session_s(self, frame: float) -> float:
        """Absolute mel frame of the encoded stream to seconds of session audio."""
        index = min(max(int(frame // self._frames), 0), len(self._starts) - 1)
        within = (frame - index * self._frames) / self._frames
        return (self._starts[index] + within * _PASS) / _SR


# --- the adapter and the session ----------------------------------------------


class _StepRunner:
    """Runs the planner's steps against the library. Synchronous: the session
    calls it only on the engine's model thread, one call at a time, so ``close``
    can never overlap a running encoder call."""

    def __init__(self, library: _Library, planner: _Planner) -> None:
        self._library = library
        self._planner = planner
        self._checkpoint: object = None
        self.rollbacks = 0
        self.client_waiting: Callable[[], bool] = lambda: False
        """Set by the session: whether its client waits on the encoder now."""

    @property
    def buffered_s(self) -> float:
        return self._planner.buffered / _SR

    @property
    def text(self) -> str:
        return self._library.text

    @property
    def passes(self) -> int:
        return self._library.calls

    def feed(self, samples: NDArray[np.float32]) -> None:
        self._run(self._planner.feed(samples))

    def flush(self) -> Any:
        """Run the remaining calls; the final result, or ``None`` if nothing was encoded."""
        self._run(self._planner.flush())
        return self._library.result()

    def urgent(self, step: _Encode) -> bool:
        """Whether a client is waiting on this encoder call, so it goes first on
        the GPU: the call at a pause, every call after ``finalize``, and every
        call of a session that is behind. Calls while the speaker talks and the
        encoder keeps up (it runs several times faster than real time) do not
        need to, and synthesis keeps the GPU through them."""
        return step.speculative or self.client_waiting()

    def close(self) -> None:
        self._library.close()
        log.debug("stream closed: %d encoder calls, %d undone", self.passes, self.rollbacks)

    def _run(self, steps: list[_Step]) -> None:
        for step in steps:
            if isinstance(step, _Encode):
                self._library.encode(step.window, step.start, self.urgent(step))
            elif step == "checkpoint":
                self._checkpoint = self._library.checkpoint()
            else:
                self._library.restore(self._checkpoint)
                self._checkpoint = None
                self.rollbacks += 1


class ParakeetStreamSession(STTStreamSession):
    """One streaming transcription: a buffer, a consumer task and the adapter.

    ``finalize()`` returns the transcript. ``close()`` restores the model's
    attention mode and must run after ``finalize()`` (the engine stays locked
    to this session until then); the context manager's exit does it. Use
    ``async with``.
    """

    def __init__(self, engine: ParakeetEngine, adapter: _StepRunner, language: str | None) -> None:
        self._engine = engine
        self._thread = engine._thread
        self._adapter = adapter
        self._language = language
        self._pending: list[NDArray[np.float32]] = []
        self._pending_samples = 0
        self._in_flight = 0
        self._samples_added = 0
        self._consumer: asyncio.Task[None] | None = None
        self._progress = asyncio.Event()  # set after every call the consumer makes
        self._failure: BaseException | None = None
        self._closed = False
        adapter.client_waiting = self._client_waiting

    @property
    def passes(self) -> int:
        return self._adapter.passes

    @property
    def backlog_s(self) -> float:
        """Seconds of audio added but not yet encoded: pending, taken by the
        consumer but not yet fed through (the call running now and the rest of
        its burst), and buffered by the adapter. While a call runs, the part of
        it under one pass that the planner keeps is counted twice."""
        return (self._pending_samples + self._in_flight) / _SR + self._adapter.buffered_s

    async def add_audio(self, samples: NDArray[np.float32]) -> None:
        self._check_open()
        arr = _check_samples(samples)
        if len(arr) == 0:
            return
        if len(arr) / _SR > MAX_BACKLOG_S:
            raise EngineError(f"audio chunk over {MAX_BACKLOG_S:.0f}s; send smaller chunks")
        # Behind: wait for the encoder, pushing back on the caller.
        while self.backlog_s + len(arr) / _SR > MAX_BACKLOG_S and self._consuming():
            self._progress.clear()
            await self._progress.wait()
            self._check_open()
        self._pending.append(arr)
        self._pending_samples += len(arr)
        self._samples_added += len(arr)
        if self._consumer is None or self._consumer.done():
            self._consumer = asyncio.create_task(self._consume())

    def _client_waiting(self) -> bool:
        """After ``finalize`` (the client waits for the transcript), or over
        :data:`URGENT_BACKLOG_S` unencoded. Read on the model thread."""
        return self._closed or self.backlog_s > URGENT_BACKLOG_S

    def _consuming(self) -> bool:
        return self._consumer is not None and not self._consumer.done()

    async def _consume(self) -> None:
        try:
            while self._pending:
                chunk = (
                    np.concatenate(self._pending) if len(self._pending) > 1 else self._pending[0]
                )
                self._pending = []
                self._pending_samples = 0
                for i in range(0, len(chunk), _FEED_MAX):
                    self._in_flight = len(chunk) - i
                    try:
                        await self._thread.run(self._adapter.feed, chunk[i : i + _FEED_MAX])
                    finally:
                        self._in_flight = 0
                        self._progress.set()
        except Exception as exc:
            self._failure = exc
        finally:
            self._progress.set()  # the task is done when a waiter wakes

    def partial(self) -> str:
        return self._adapter.text

    async def finalize(self) -> Transcript:
        self._check_open()
        self._closed = True
        await self._drain()
        result = await self._thread.run(self._adapter.flush)
        duration = self._samples_added / _SR
        if result is None:
            return Transcript(text="", language=self._language, duration_s=duration)
        return _to_transcript(result, self._language, duration)

    async def close(self) -> None:
        self._closed = True
        try:
            task = self._consumer
            if task is not None:
                task.cancel()  # pending audio is discarded; the running call completes
                await asyncio.wait({task})
                if not task.cancelled():
                    task.exception()  # retrieve it so it is never "never retrieved"
        finally:
            try:
                await self._thread.run(self._adapter.close)
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


# --- helpers ------------------------------------------------------------------


def _pad(window: NDArray[np.float32]) -> NDArray[np.float32]:
    """Zero-pad to one pass: every streaming call is exactly :data:`PASS_S`."""
    if len(window) >= _PASS:
        return window
    return np.concatenate([window, np.zeros(_PASS - len(window), dtype=np.float32)])


def _generate(model: Any, samples: NDArray[np.float32]) -> Any:
    """Blocking batch transcription. Returns the library's ``AlignedResult``.

    Input longer than :data:`BATCH_CHUNK_S` is transcribed in overlapping
    chunks merged on their tokens, as the library's file-based ``transcribe``
    does, so memory does not grow with the length of the input. MLX's buffer
    cache is emptied afterwards rather than held until the next request.
    """
    import mlx.core as mx
    from parakeet_mlx.alignment import (
        merge_longest_common_subsequence,
        merge_longest_contiguous,
        sentences_to_result,
        tokens_to_sentences,
    )
    from parakeet_mlx.audio import get_logmel

    def run(audio: NDArray[np.float32]) -> Any:
        return model.generate(get_logmel(mx.array(audio), model.preprocessor_config))[0]

    try:
        if len(samples) <= _BATCH_CHUNK:
            return run(samples)
        tokens: list[Any] = []
        for start in range(0, len(samples), _BATCH_CHUNK - _BATCH_OVERLAP):
            end = min(start + _BATCH_CHUNK, len(samples))
            offset = start / _SR
            chunk = [replace(t, start=t.start + offset) for t in run(samples[start:end]).tokens]
            if not tokens:
                tokens = chunk
            else:
                overlap = BATCH_OVERLAP_S
                try:
                    tokens = merge_longest_contiguous(tokens, chunk, overlap_duration=overlap)
                except RuntimeError:  # how the library reports no contiguous overlap
                    merge = merge_longest_common_subsequence
                    tokens = merge(tokens, chunk, overlap_duration=overlap)
            if end == len(samples):
                break
        return sentences_to_result(tokens_to_sentences(tokens))
    finally:
        mx.clear_cache()


def _is_silent(samples: NDArray[np.float32]) -> bool:
    """True when no 32 ms frame reaches the absolute silence floor."""
    return float(np.max(frame_rms(samples), initial=0.0)) < SILENCE_FLOOR


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
