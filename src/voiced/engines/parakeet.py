"""Parakeet TDT 0.6B v3 speech-to-text through ``parakeet-mlx``.

Batch: :meth:`ParakeetEngine.transcribe` runs the log-mel front end and the
TDT decoder over the whole utterance in one call and returns sentence
segments with timestamps.

Streaming: :meth:`ParakeetEngine.stream` wraps ``transcribe_stream`` with
``context_size=(256, 256)``. Two properties of the library shape the session
design (measured in docs/decisions/0004-stt-engine-parakeet.md):

* Every call to its ``add_audio`` re-encodes and re-decodes the whole retained
  window (up to 20 s), which costs 150 to 300 ms per call regardless of how
  much audio the call adds. Feeding 100 ms chunks one by one therefore runs
  slower than real time.
* The log-mel is normalized per call, so calls with less than about 320 ms of
  audio produce worse text, and calls with only a few hundred samples read
  past the end of the buffer and produce garbage.

So the session never calls the encoder from ``add_audio``. It buffers, and one
background pass loop feeds the encoder 320 to 600 ms at a time in a worker
thread. ``add_audio`` returns in microseconds, ``partial()`` returns the last
decoded hypothesis, and ``finalize()`` waits for the loop to drain the buffer
(the final tail is zero-padded to the minimum pass size).

The model's attention is switched to local windows while a stream session is
open and restored when it closes, so a batch call must never run while a
session is open. The server guarantees that by holding the engine's inference
slot for the whole session.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
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

DEFAULT_CONTEXT_SIZE: tuple[int, int] = (256, 256)
WARMUP_S = 1.0
MIN_PASS_S = 0.32
MAX_PASS_S = 0.60
# Below one analysis window (25 ms) there is nothing to transcribe, and the
# library's padding misbehaves on inputs this short.
MIN_BATCH_SAMPLES = 400
# Input quieter than this RMS (-60 dBFS) is treated as silence. The model
# hallucinates short words ("Yeah.") on near-silent noise, and no microphone
# delivers intelligible speech this quiet.
SILENCE_RMS = 1e-3


class ParakeetEngine(STTEngine):
    name = "parakeet"
    model_id = "mlx-community/parakeet-tdt-0.6b-v3"
    languages = list(PARAKEET_V3_LANGUAGES)
    license = "CC-BY-4.0"

    def __init__(
        self,
        model_id: str | None = None,
        *,
        context_size: tuple[int, int] = DEFAULT_CONTEXT_SIZE,
        depth: int = 1,
        min_pass_s: float = MIN_PASS_S,
        max_pass_s: float = MAX_PASS_S,
    ) -> None:
        super().__init__(model_id)
        if not 0 < min_pass_s <= max_pass_s:
            raise ValueError("need 0 < min_pass_s <= max_pass_s")
        self._context_size = (int(context_size[0]), int(context_size[1]))
        self._depth = depth
        self._min_pass = int(min_pass_s * STT_SAMPLE_RATE)
        self._max_pass = int(max_pass_s * STT_SAMPLE_RATE)
        self._model: Any = None
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
        await asyncio.to_thread(self._load_blocking)

    def _load_blocking(self) -> None:
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
        _stream_warmup(model, self._context_size, self._depth, silence)
        self._model = model
        self.load_seconds = t1 - t0
        self.warmup_seconds = time.perf_counter() - t1
        log.info(
            "STT model ready: load %.1fs, warm-up %.1fs", self.load_seconds, self.warmup_seconds
        )

    def _require_loaded(self) -> Any:
        if self._model is None:
            raise EngineNotLoaded(self.name)
        return self._model

    async def transcribe(self, audio: AudioInput, language: str | None = None) -> Transcript:
        model = self._require_loaded()
        lang = self.normalize_language(language)
        if audio.sample_rate != STT_SAMPLE_RATE:
            raise EngineError(f"expected {STT_SAMPLE_RATE} Hz audio, got {audio.sample_rate} Hz")
        samples = _check_samples(audio.samples)
        duration = len(samples) / STT_SAMPLE_RATE
        if len(samples) < MIN_BATCH_SAMPLES or _is_silent(samples):
            return Transcript(text="", language=lang, duration_s=duration)
        result = await asyncio.to_thread(_generate, model, samples)
        return _to_transcript(result, lang, duration)

    async def stream(self, language: str | None = None) -> STTStreamSession:
        model = self._require_loaded()
        lang = self.normalize_language(language)
        return ParakeetStreamSession(
            model,
            lang,
            context_size=self._context_size,
            depth=self._depth,
            min_pass=self._min_pass,
            max_pass=self._max_pass,
        )


class ParakeetStreamSession(STTStreamSession):
    """One streaming transcription. See the module docstring for the design."""

    def __init__(
        self,
        model: Any,
        language: str | None,
        *,
        context_size: tuple[int, int] = DEFAULT_CONTEXT_SIZE,
        depth: int = 1,
        min_pass: int = int(MIN_PASS_S * STT_SAMPLE_RATE),
        max_pass: int = int(MAX_PASS_S * STT_SAMPLE_RATE),
    ) -> None:
        self._model = model
        self._language = language
        self._context_size = context_size
        self._depth = depth
        self._min_pass = min_pass
        self._max_pass = max_pass
        # Streaming timestamps are relative to the retained window, which only
        # starts at zero while the utterance fits in the left context.
        self._timestamp_horizon_s = context_size[0] * float(model.time_ratio)
        self._stream: Any = None
        self._pending: list[NDArray[np.float32]] = []
        self._pending_samples = 0
        self._text = ""
        self._result: Any = None
        self._closed = False
        self._released = False
        self._flush = False
        self._pass_task: asyncio.Task[None] | None = None
        self._teardown_task: asyncio.Task[None] | None = None
        # Diagnostics, read by tests and benchmarks.
        self.samples_added = 0
        self.samples_encoded = 0
        self.passes = 0
        self.pass_sizes: list[int] = []
        self.last_pass_s = 0.0
        self._energy = 0.0  # sum of squares of everything added, for the silence gate

    @property
    def backlog_s(self) -> float:
        """Seconds of audio added but not yet encoded."""
        return (self.samples_added - self.samples_encoded) / STT_SAMPLE_RATE

    async def add_audio(self, samples: NDArray[np.float32]) -> None:
        self._check_open()
        arr = _check_samples(samples)
        if len(arr) == 0:
            return
        self._pending.append(arr)
        self._pending_samples += len(arr)
        self.samples_added += len(arr)
        self._energy += float(np.dot(arr, arr))
        if self._pending_samples >= self._min_pass:
            self._kick()

    def partial(self) -> str:
        return self._text

    async def finalize(self) -> Transcript:
        self._check_open()
        self._closed = True
        try:
            self._flush = True
            self._kick()
            if self._pass_task is not None:
                await self._pass_task
            duration = self.samples_added / STT_SAMPLE_RATE
            silent = self.samples_added == 0 or (
                math.sqrt(self._energy / self.samples_added) < SILENCE_RMS
            )
            if self._result is None or silent:
                return Transcript(text="", language=self._language, duration_s=duration)
            transcript = _to_transcript(self._result, self._language, duration)
            if duration > self._timestamp_horizon_s:
                transcript = replace(transcript, segments=())
            return transcript
        finally:
            await self._release()

    async def close(self) -> None:
        self._closed = True
        await self._release()

    def _check_open(self) -> None:
        if self._closed:
            raise EngineError("stream session is closed")

    def _kick(self) -> None:
        if self._released:
            return
        if self._pass_task is None or self._pass_task.done():
            self._pass_task = asyncio.create_task(self._run_passes())

    async def _run_passes(self) -> None:
        while not self._released:
            window = self._take_window()
            if window is None:
                return
            real, padded = window
            await asyncio.to_thread(self._encode, padded, real)

    def _take_window(self) -> tuple[int, NDArray[np.float32]] | None:
        """Pop the next encoder pass from the buffer: ``(real_samples, audio)``.

        Passes are between ``min_pass`` and ``max_pass`` samples; a backlog is
        split evenly so no pass is undersized. Only when flushing may fewer
        than ``min_pass`` real samples remain, and then they are zero-padded.
        """
        n = self._pending_samples
        if n == 0 or (n < self._min_pass and not self._flush):
            return None
        buf = np.concatenate(self._pending) if len(self._pending) > 1 else self._pending[0]
        take = n if n <= self._max_pass else math.ceil(n / math.ceil(n / self._max_pass))
        window, rest = buf[:take], buf[take:]
        self._pending = [rest] if len(rest) else []
        self._pending_samples = len(rest)
        if len(window) < self._min_pass:
            pad = np.zeros(self._min_pass - len(window), dtype=np.float32)
            window = np.concatenate([window, pad])
        return take, window

    def _encode(self, window: NDArray[np.float32], real: int) -> None:
        """Worker thread: one encoder pass. The only place that touches MLX."""
        import mlx.core as mx

        if self._stream is None:
            self._stream = self._model.transcribe_stream(
                context_size=self._context_size, depth=self._depth
            )
            self._stream.__enter__()
        t0 = time.perf_counter()
        self._stream.add_audio(mx.array(window))
        result = self._stream.result
        self._result = result
        self._text = _normalize_text(result.text)
        self.samples_encoded += real
        self.passes += 1
        self.pass_sizes.append(len(window))
        self.last_pass_s = time.perf_counter() - t0

    async def _release(self) -> None:
        """Stop feeding, wait for the in-flight pass, restore the model.

        The teardown runs as its own task and is shielded, so a cancelled
        caller still leaves the model usable: the encoder pass cannot be
        interrupted and the stream context must not be exited underneath it.
        """
        if self._released:
            return
        self._released = True
        self._pending = []
        self._pending_samples = 0
        self._teardown_task = asyncio.create_task(self._teardown(self._pass_task))
        await asyncio.shield(self._teardown_task)

    async def _teardown(self, pass_task: asyncio.Task[None] | None) -> None:
        if pass_task is not None:
            try:
                await pass_task
            except Exception:
                log.exception("streaming pass failed")
        if self._stream is not None:
            await asyncio.to_thread(self._exit_stream)

    def _exit_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.__exit__(None, None, None)


def _generate(model: Any, samples: NDArray[np.float32]) -> Any:
    """Blocking batch transcription. Returns the library's ``AlignedResult``."""
    import mlx.core as mx
    from parakeet_mlx.audio import get_logmel

    mel = get_logmel(mx.array(samples), model.preprocessor_config)
    return model.generate(mel)[0]


def _stream_warmup(
    model: Any, context_size: tuple[int, int], depth: int, samples: NDArray[np.float32]
) -> None:
    import mlx.core as mx

    with model.transcribe_stream(context_size=context_size, depth=depth) as stream:
        stream.add_audio(mx.array(samples))


def _is_silent(samples: NDArray[np.float32]) -> bool:
    return bool(np.sqrt(np.mean(np.square(samples, dtype=np.float32))) < SILENCE_RMS)


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
