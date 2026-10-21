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

Several sessions stream at once (ADR 0020). Each has its own planner, speech
detector and library stream state over the one set of weights; every MLX call
of the engine runs on the engine's one :class:`ModelThread`, a session's work
in jobs of at most one encoder call, each ranked by :class:`_Rank` when the
thread picks its next call: the calls a client waits on first, then sessions
that are behind, then the rest in turn. Calls of the first two ranks hold
:func:`voiced.gpu.stt_turn`, so Kokoro synthesizing in the TTS service waits
for them (ADR 0019).

Local attention is a setting of the shared model (:class:`_Attention`): on
while any session is open, restored when the last one closes. The engine
refuses a batch call while a session is open, and the server holds a shared
inference slot (``stt.streams`` of them) until the session's context manager
exits.
"""

from __future__ import annotations

import asyncio
import bisect
import functools
import logging
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, replace
from enum import IntEnum
from typing import Any, Literal, Protocol

import numpy as np
from numpy.typing import NDArray

from voiced.audio import VAD_FRAME_SAMPLES as _VAD_FRAME
from voiced.audio import check_samples, is_silent
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
from voiced.models import default_model
from voiced.text import collapse_whitespace, primary_subtag
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
URGENT_BACKLOG_S = 2 * PASS_S
"""Unencoded audio past which a session is behind, so its encoder calls go first
on the GPU (ADR 0019). A session that keeps up holds under one pass in the
planner plus the few chunks that arrive during a call; this is one more pass."""
WARMUP_S = 1.0
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


class _Rank(IntEnum):
    """Which of the sessions' calls the model thread runs next, lowest first;
    the earliest queued among equals, so sessions take turns."""

    WAITED = 0
    """A client waits on this call: the call at a pause, every call after
    ``finalize``."""
    BEHIND = 1
    """The session is over :data:`URGENT_BACKLOG_S` behind."""
    AHEAD = 2
    """A regular pass while the speaker talks and the encoder keeps up."""


class ParakeetEngine(STTEngine):
    name = "parakeet"
    model_id = default_model("parakeet").id
    languages = list(PARAKEET_V3_LANGUAGES)
    license = default_model("parakeet").license
    max_streams = 6
    """Its sessions take turns on one model thread, which keeps up with about
    six real-time streams on an M1 Max; ``stt.streams`` (2) is what keeps
    every finalize within the target (ADR 0020)."""

    def __init__(self, model_id: str | None = None) -> None:
        super().__init__(model_id)
        self._model: Any = None
        self._thread = ModelThread("parakeet")
        self._attention: _Attention | None = None
        self._sessions: set[ParakeetStreamSession] = set()

    @classmethod
    def normalize_language(cls, language: str | None) -> str | None:
        """``"en-US"`` to ``"en"``; raise :class:`UnsupportedLanguage` if not supported."""
        if language is None:
            return None
        code = primary_subtag(language.strip())
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
        # The pinned revision's directory, from the cache; never downloaded here.
        model = from_pretrained(str(snapshot(self.model_id, check_only=True)))
        load_s = time.perf_counter() - t0
        # Warm up both paths so the first real request does not pay for Metal
        # kernel compilation, and so a broken GPU path fails here, before the
        # service reports healthy.
        t1 = time.perf_counter()
        _generate(model, np.zeros(round(WARMUP_S * _SR), dtype=np.float32))
        attention = _Attention(model)
        warm = _LibraryStream(model, attention)
        try:
            warm.encode(np.zeros(_PASS, dtype=np.float32), 0, urgent=False)
        finally:
            warm.close()
        self._attention = attention
        self._model = model
        log.info("STT model ready: load %.1fs, warm-up %.1fs", load_s, time.perf_counter() - t1)

    def _require_loaded(self) -> Any:
        if self._model is None:
            raise EngineNotLoaded(self.name)
        return self._model

    def _require_idle(self) -> Any:
        model = self._require_loaded()
        if self._sessions:
            raise EngineError("a stream session is open on this engine")
        return model

    async def transcribe(self, audio: AudioInput, language: str | None = None) -> Transcript:
        model = self._require_idle()
        lang = self.normalize_language(language)
        if audio.sample_rate != _SR:
            raise EngineError(f"expected {_SR} Hz audio, got {audio.sample_rate} Hz")
        samples = check_samples(audio.samples)
        duration = len(samples) / _SR
        if len(samples) < LIBRARY_MIN_SAMPLES or is_silent(samples):
            return Transcript(text="", language=lang, duration_s=duration)
        result = await self._thread.run(_generate, model, samples)
        return _to_transcript(result, lang, duration)

    async def stream(self, language: str | None = None) -> STTStreamSession:
        """A new session, alongside any already open: the server opens at most
        :attr:`max_streams` at once (``stt.streams``, ADR 0020)."""
        model = self._require_loaded()
        lang = self.normalize_language(language)
        planner = _Planner(self._new_detector())
        session = ParakeetStreamSession(self, self._new_library(model), planner, lang)
        self._sessions.add(session)
        return session

    def _new_library(self, model: Any) -> _Library:
        assert self._attention is not None
        return _LibraryStream(model, self._attention)

    def _new_detector(self) -> Endpointer:
        """Silero, one per session (about 1 ms to create). Only its speech start
        and per-frame decisions are used, so its silence window does not matter."""
        return Endpointer(silence_ms=0)

    def _session_closed(self, session: ParakeetStreamSession) -> None:
        self._sessions.discard(session)


# --- planning: what to encode, when ------------------------------------------


@dataclass(frozen=True, slots=True)
class _Encode:
    """Encode ``window`` (exactly one pass) that starts at session sample ``start``."""

    window: NDArray[np.float32]
    start: int
    speculative: bool = False
    """The call at a pause, which the hypothesis at the end of speech waits on."""


_Step = _Encode | Literal["checkpoint", "restore"]


@dataclass(frozen=True, slots=True)
class _Job:
    """One model-thread call of a session: planner steps up to and including at
    most one encoder call, so another session's urgent call waits for one at
    most. The exception is :meth:`_StepRunner.finish`, all of whose calls a
    client waits on."""

    steps: tuple[_Step, ...]

    @property
    def speculative(self) -> bool:
        return any(isinstance(s, _Encode) and s.speculative for s in self.steps)

    @property
    def samples(self) -> int:
        """Audio this job encodes that the planner no longer holds (it keeps the
        buffer of a speculative call)."""
        return sum(
            len(s.window) for s in self.steps if isinstance(s, _Encode) and not s.speculative
        )


def _without_undone(steps: list[_Step]) -> list[_Step]:
    """Without any call at a pause that the same steps undo: a pause that began
    and ended inside one ``feed`` (a quiet frame in a chunk) needs no call,
    and it would run as urgent. The kept calls are the same."""
    out: list[_Step] = []
    for step in steps:
        if step == "restore" and len(out) >= 2 and out[-2] == "checkpoint":
            del out[-2:]
        else:
            out.append(step)
    return out


def _jobs(steps: list[_Step]) -> list[_Job]:
    jobs: list[_Job] = []
    start = 0
    for i, step in enumerate(steps):
        if isinstance(step, _Encode):
            jobs.append(_Job(tuple(steps[start : i + 1])))
            start = i + 1
    if start < len(steps):
        jobs.append(_Job(tuple(steps[start:])))
    return jobs


class _Planner:
    """Turns incoming audio into encoder calls. Pure numpy; positions are session
    samples (everything fed since the session started).

    * **Speech-start gate.** Nothing is planned until ``speech`` reports speech
      start; then the buffer starts :data:`PRE_ROLL_S` before the onset.
    * **Grid.** Every full :data:`PASS_S` of buffered audio is one call if any of
      its 32 ms frames is speech. A pass with none is never encoded (the model
      invents words in silence): the grid restarts :data:`PRE_ROLL_S` before the
      next speech, and a long pause keeps at most one pass of audio meanwhile.
    * **Pause.** When the latest 32 ms frame is not speech, a checkpoint and one
      call over the buffer up to the end of the first frame after the last
      speech (always under a pass), padded to a pass, run at once, so the
      hypothesis is complete before the endpointer's silence window elapses.
      The buffer is kept. If speech resumes, or the pause outgrows
      :data:`HOLD_MAX_S`, the checkpoint is restored and the buffer goes on as
      if there had been no pause; at finalize the speculative call is the last
      one.

    What is encoded depends on the audio alone, never on how it was cut into
    ``feed`` calls (the client's chunks, or how much a session that is behind
    plans at once): windows are decided from judged frames, and every position
    is the start of speech, a pass after one, or a frame boundary.
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
        self._buffer = np.concatenate([self._buffer, samples])
        steps: list[_Step] = []
        for events, before in self._frames(samples):
            steps += self._decide(events, before)
        return _without_undone(steps)

    def _decide(self, events: list[EndpointEvent], before: int) -> list[_Step]:
        """The steps due once one more 32 ms frame is judged (it ends at
        :meth:`_heard`; ``before`` is where it starts). Taken frame by frame
        from judged positions only, so they do not depend on how the audio was
        cut into ``feed`` calls."""
        if not self._started:
            onset = next((e for e in events if e.kind == "speech_start"), None)
            if onset is None:
                self._drop_before(self._heard() - self._history)
                return []
            self._started = True
            self._drop_before(round(onset.speech_start_s * _SR) - _PRE_ROLL)
        steps: list[_Step] = []
        if self._speculating:
            if self._last_speech() <= before and self._heard() - self._last_speech() <= _MAX_HELD:
                return []  # the pause goes on
            steps.append("restore")
            self._speculating = False
        steps += self._due()
        # _has_speech asks which pass the recorded speech frames belong to, so
        # it applies the half-frame rule against _start; _at_pause measures the
        # pause from the end of the last speech frame (_speech_ends[-1]), where
        # the buffer starts does not matter.
        if self._has_speech() and self._at_pause():
            window = self._buffer[: self._last_speech() + _VAD_FRAME - self._start]
            steps += ["checkpoint", _Encode(_pad(window), self._start, speculative=True)]
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
        if not self._has_speech() and self._heard() - _PASS > self._start:
            # Silence fills more than a pass: keep one. The next speech frame
            # ends past it, so the grid restarts before that frame
            # (_skip_to_speech), as at speech start.
            self._drop_before(self._heard() - _PASS)
        return steps

    def _frames(self, samples: NDArray[np.float32]) -> Iterator[tuple[list[EndpointEvent], int]]:
        """Feed the detector frame by frame, recording where each speech frame
        ends; after each frame it judged, its events and where the frame began."""
        i, short = 0, self._fed - self._heard()  # samples the detector holds back
        while i < len(samples):
            n = min(_VAD_FRAME - short, len(samples) - i)
            before, last = self._heard(), self._speech.last_speech_s
            events = self._speech.feed(samples[i : i + n])
            self._fed += n
            if self._speech.last_speech_s != last:
                self._speech_ends.append(self._last_speech())
            if self._heard() > before:
                yield events, before
            i, short = i + n, 0

    def _heard(self) -> int:
        """Session position up to which the detector has judged every frame."""
        return round(self._speech.position_s * _SR)

    def _last_speech(self) -> int:
        """Session position where the last frame judged speech ended."""
        return round(self._speech.last_speech_s * _SR)

    def _has_speech(self) -> bool:
        """Whether the buffer holds a speech frame by :meth:`_speech_in`'s rule
        (most of the frame past ``_start``), the one :meth:`_skip_to_speech`
        relies on: a frame reaching under half a frame past ``_start`` belongs to
        the pass before."""
        half = _VAD_FRAME // 2
        return bool(self._speech_ends) and self._speech_ends[-1] - half > self._start

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


class _Attention:
    """The model's attention mode, one setting of the weights every stream
    shares: local attention (:data:`CONTEXT_SIZE`, the same for every stream)
    while any stream is open, full attention, which batch needs, once none is.
    The library would set it per stream and restore it when any one closes.
    Model thread only."""

    def __init__(self, model: Any) -> None:
        self._model = model
        self.streams = 0

    def enter(self) -> None:
        if self.streams == 0:
            self._model.encoder.set_attention_model("rel_pos_local_attn", CONTEXT_SIZE)
        self.streams += 1

    def exit(self) -> None:
        self.streams -= 1
        if self.streams == 0:
            self._model.encoder.set_attention_model("rel_pos")


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

    def __init__(self, model: Any, attention: _Attention) -> None:
        import mlx.core as mx

        self._model = model
        self._attention = attention
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
        self.text = collapse_whitespace(self.result().text)
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
        """Exit the stream context; the last stream open restores the model's
        attention mode."""
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.__exit__(None, None, None)
            finally:
                self._attention.exit()

    def _open(self) -> Any:
        if self._stream is None:
            stream = self._model.transcribe_stream(
                context_size=CONTEXT_SIZE, keep_original_attention=True
            )
            self._attention.enter()
            self._stream = stream.__enter__()
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
    """Runs the planner's steps against the library. :meth:`plan` runs on the
    event loop (numpy and Silero, about 0.12 ms per 32 ms frame); every other
    method runs on the engine's model thread, one call at a time, so ``close``
    can never overlap a running encoder call.

    The first failure, planning or encoding, is kept in :attr:`failure`: the
    state is broken, so nothing more runs and every later call raises it.
    """

    def __init__(
        self,
        library: _Library,
        planner: _Planner,
        session_rank: Callable[[], _Rank],
    ) -> None:
        self._library = library
        self._planner = planner
        self._session_rank = session_rank
        """The rank of the session's calls other than one at a pause."""
        self._checkpoint: object = None
        self.failure: BaseException | None = None
        self.rollbacks = 0

    @property
    def buffered_s(self) -> float:
        return self._planner.buffered / _SR

    @property
    def text(self) -> str:
        return self._library.text

    @property
    def passes(self) -> int:
        return self._library.calls

    def plan(self, samples: NDArray[np.float32]) -> list[_Job]:
        with self._latch():
            return _jobs(self._planner.feed(samples))

    def rank(self, job: _Job) -> _Rank:
        return _Rank.WAITED if job.speculative else self._session_rank()

    def urgent(self, job: _Job) -> bool:
        """Whether a client is waiting on this job's encoder call, so it goes
        first on the GPU: the call at a pause, every call after ``finalize``,
        and every call of a session that is behind. Calls while the speaker
        talks and the encoder keeps up (it runs several times faster than real
        time) do not need to, and synthesis keeps the GPU through them."""
        return self.rank(job) < _Rank.AHEAD

    def run(self, job: _Job) -> None:
        urgent = self.urgent(job)
        with self._latch():
            for step in job.steps:
                if isinstance(step, _Encode):
                    self._library.encode(step.window, step.start, urgent)
                elif step == "checkpoint":
                    self._checkpoint = self._library.checkpoint()
                else:
                    self._library.restore(self._checkpoint)
                    self._checkpoint = None
                    self.rollbacks += 1

    def finish(self, jobs: list[_Job], samples: NDArray[np.float32]) -> Any:
        """The rest at ``finalize`` in one call: the jobs planned but not run,
        the audio not yet planned (under about a pass), the calls still needed
        for the final hypothesis. The final result, or ``None`` if nothing was
        encoded."""
        for job in [*jobs, *self.plan(samples)]:
            self.run(job)
        with self._latch():
            steps = self._planner.flush()
        for job in _jobs(steps):
            self.run(job)
        return self._library.result()

    def close(self) -> None:
        self._library.close()
        log.debug("stream closed: %d encoder calls, %d undone", self.passes, self.rollbacks)

    @contextmanager
    def _latch(self) -> Iterator[None]:
        if self.failure is not None:
            raise self.failure
        try:
            yield
        except BaseException as exc:
            self.failure = exc
            raise


class ParakeetStreamSession(STTStreamSession):
    """One streaming transcription: a buffer, a consumer task and the adapter.

    ``finalize()`` returns the transcript. ``close()`` releases the session's
    stream state and must run after ``finalize()`` (the engine counts the
    session as open until then); the context manager's exit does it. Use
    ``async with``.
    """

    def __init__(
        self,
        engine: ParakeetEngine,
        library: _Library,
        planner: _Planner,
        language: str | None,
    ) -> None:
        self._engine = engine
        self._thread = engine._thread
        self._adapter = _StepRunner(library, planner, self._rank)
        self._language = language
        self._pending: deque[NDArray[np.float32]] = deque()
        self._pending_samples = 0
        self._jobs: deque[_Job] = deque()  # planned, not yet started
        # What those jobs and the running one encode, kept as a plain counter
        # for backlog_s, which the model thread reads; _not_started() is the
        # same without the running job, read on the loop only.
        self._planned_samples = 0
        self._samples_added = 0
        self._consumer: asyncio.Task[None] | None = None
        self._progress = asyncio.Event()  # set after every call the consumer makes
        # The session's lifecycle: open, then closed (finalize or close: every
        # call ranks WAITED, no more audio), then handed over (finalize took
        # the rest as one call, or close discards it: the consumer stops).
        self._closed = False
        self._handed_over = False

    @property
    def passes(self) -> int:
        return self._adapter.passes

    @property
    def backlog_s(self) -> float:
        """Seconds of audio added but not yet encoded: pending, planned into
        jobs not yet done (the one running included), and buffered by the
        planner. Plain counters, so the model thread can read it."""
        return (self._pending_samples + self._planned_samples) / _SR + self._adapter.buffered_s

    async def add_audio(self, samples: NDArray[np.float32]) -> None:
        self._check_open()
        arr = check_samples(samples)
        if len(arr) == 0:
            return
        if len(arr) / _SR > MAX_BACKLOG_S:
            raise EngineError(f"audio chunk over {MAX_BACKLOG_S:.0f}s; send smaller chunks")
        # Behind: wait for the encoder, pushing back on the caller.
        while self.backlog_s + len(arr) / _SR > MAX_BACKLOG_S and self._consuming():
            await self._next_progress()
            self._check_open()
        self._pending.append(arr)
        self._pending_samples += len(arr)
        self._samples_added += len(arr)
        if self._consumer is None or self._consumer.done():
            self._consumer = asyncio.create_task(self._consume())

    def _rank(self) -> _Rank:
        """After ``finalize`` (the client waits for the transcript), or over
        :data:`URGENT_BACKLOG_S` unencoded. Read on the model thread."""
        if self._closed:
            return _Rank.WAITED
        if self.backlog_s > URGENT_BACKLOG_S:
            return _Rank.BEHIND
        return _Rank.AHEAD

    def _consuming(self) -> bool:
        return self._consumer is not None and not self._consumer.done()

    async def _next_progress(self) -> None:
        self._progress.clear()
        await self._progress.wait()

    async def _consume(self) -> None:
        """Plan a pass of pending audio at a time and run its jobs one by one,
        until there is nothing left or ``finalize`` takes the rest. A failure
        is the adapter's to keep (:attr:`_StepRunner.failure`)."""
        try:
            while not self._handed_over:
                if not self._jobs:
                    if not self._pending:
                        return
                    jobs = self._adapter.plan(self._take(_PASS))
                    self._jobs.extend(jobs)
                    self._planned_samples += sum(job.samples for job in jobs)
                    self._progress.set()  # a backlog of silence plans no job,
                    await asyncio.sleep(0)  # but frees room all the same
                    continue
                job = self._jobs.popleft()
                try:
                    await self._thread.run_ranked(
                        functools.partial(self._adapter.rank, job), self._adapter.run, job
                    )
                finally:
                    self._planned_samples -= job.samples
                    self._progress.set()
        finally:
            self._progress.set()  # the task is done when a waiter wakes

    def _take(self, samples: int) -> NDArray[np.float32]:
        """Up to ``samples`` of the pending audio, oldest first."""
        parts: list[NDArray[np.float32]] = []
        taken = 0
        while self._pending and taken < samples:
            part = self._pending.popleft()
            if taken + len(part) > samples:
                part, rest = part[: samples - taken], part[samples - taken :]
                self._pending.appendleft(rest)
            parts.append(part)
            taken += len(part)
        self._pending_samples -= taken
        if len(parts) == 1:
            return parts[0]
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.float32)

    def _not_started(self) -> int:
        """Samples pending or planned into jobs not yet started: unlike
        ``_planned_samples``, without the job running now. Loop only."""
        return self._pending_samples + sum(job.samples for job in self._jobs)

    def partial(self) -> str:
        return self._adapter.text

    async def finalize(self) -> Transcript:
        self._check_open()
        # From here every call of this session ranks WAITED (_rank reads it).
        self._closed = True
        # A backlog is encoded call by call, as before, so other sessions' calls
        # at a pause still go between; what is left for the one call below is
        # about a pass (the call in flight is not part of it).
        while self._not_started() > _PASS and self._consuming():
            await self._next_progress()
        self._handed_over = True
        jobs, self._jobs = list(self._jobs), deque()
        self._planned_samples -= sum(job.samples for job in jobs)
        audio = self._take(self._pending_samples)
        # Queued before this coroutine first waits, so behind the session's own
        # call in flight (equal rank, queued earlier) and ahead of anything
        # queued after it.
        result = await self._thread.run_ranked(self._rank, self._adapter.finish, jobs, audio)
        duration = self._samples_added / _SR
        if result is None:
            return Transcript(text="", language=self._language, duration_s=duration)
        return _to_transcript(result, self._language, duration)

    async def close(self) -> None:
        self._closed = True
        self._handed_over = True
        try:
            task = self._consumer
            if task is not None:
                # Pending audio is discarded; a call not yet started is
                # withdrawn, and the running one completes.
                task.cancel()
                await asyncio.wait({task})
                if not task.cancelled():
                    task.exception()  # retrieve it so it is never "never retrieved"
        finally:
            try:
                await self._thread.run(self._adapter.close)
            finally:
                self._engine._session_closed(self)

    def _check_open(self) -> None:
        if self._adapter.failure is not None:
            raise self._adapter.failure
        if self._closed:
            raise EngineError("stream session is closed")


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


def _to_transcript(result: Any, language: str | None, duration_s: float) -> Transcript:
    segments: list[Segment] = []
    for sentence in result.sentences:
        text = collapse_whitespace(sentence.text)
        if not text:
            continue
        start = max(0.0, float(sentence.start))
        end = max(start, float(sentence.end))
        if duration_s > 0:
            end = min(end, duration_s)
            start = min(start, end)
        segments.append(Segment(start=round(start, 3), end=round(end, 3), text=text))
    return Transcript(
        text=collapse_whitespace(result.text),
        language=language,
        duration_s=duration_s,
        segments=tuple(segments),
    )
