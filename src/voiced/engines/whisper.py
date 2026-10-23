"""Whisper large-v3-turbo speech-to-text through mlx-audio. Selectable, not the default.

Whisper is OpenAI's encoder-decoder recogniser; turbo is large-v3 with 4
decoder layers instead of 32. Its reason to be in voiced is breadth: 100
languages, detected when not given. It is slower and less accurate than
Parakeet in English and has no streaming: the encoder always sees a 30 s
window, so every call costs about the same, whatever the length of the turn
(see ``docs/decisions/0032-stt-whisper-turbo.md``):

* silence never reaches the model: Whisper writes "Thank you." for silence,
  hum and room noise, and the port's no-speech probability is always 0, so
  Silero (:func:`voiced.vad.speech_spans`) cuts the input into spans of
  speech, pauses under :data:`PAUSE_MS` kept inside, and each span is
  decoded on its own by mlx-audio's port of Whisper's long-form loop; times
  stay the input's, and without a hint the first span's language is used
  for the rest;
* :meth:`WhisperEngine.transcribe` asks for word timestamps, which set the
  segments' start and end;
* a stream session only buffers
  (:class:`~voiced.engines.buffered.BufferedStreamSession`): ``partial()`` is
  always ``""`` and ``finalize()`` transcribes the whole turn without word
  timestamps, taking :func:`voiced.gpu.stt_turn` for each span because a
  client is waiting, and stopping between spans if the finalize is cancelled;
* every MLX call runs on the engine's one :class:`ModelThread`, one session
  at a time (:attr:`max_streams` is 1).

Language hints are BCP-47 (``"de"``, ``"pt-BR"``); the detected language
comes back as a BCP-47 subtag in :attr:`Transcript.language`.

Everything that imports mlx-audio runs inside ``load()``, so this module
imports (the entry-point registry does so) without it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from contextlib import nullcontext
from typing import Any

import numpy as np
from numpy.typing import NDArray

from voiced.audio import clip_segment, stt_input
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
from voiced.engines.buffered import BufferedStreamSession
from voiced.engines.memory import unload_on
from voiced.engines.registry import ModelRefused
from voiced.gpu import Turn, stt_turn
from voiced.models import default_model, engine
from voiced.text import collapse_whitespace, primary_subtag
from voiced.threads import ModelThread
from voiced.vad import speech_spans

log = logging.getLogger(__name__)

__all__ = ["LANGUAGES", "WhisperEngine"]

LANGUAGES: tuple[str, ...] = (
    "en", "zh", "de", "es", "ru", "ko", "fr", "ja", "pt", "tr", "pl", "ca", "nl", "ar",
    "sv", "it", "id", "hi", "fi", "vi", "he", "uk", "el", "ms", "cs", "ro", "da", "hu",
    "ta", "no", "th", "ur", "hr", "bg", "lt", "la", "mi", "ml", "cy", "sk", "te", "fa",
    "lv", "bn", "sr", "az", "sl", "kn", "et", "mk", "br", "eu", "is", "hy", "ne", "mn",
    "bs", "kk", "sq", "sw", "gl", "mr", "pa", "si", "km", "sn", "yo", "so", "af", "oc",
    "ka", "be", "tg", "sd", "gu", "am", "yi", "lo", "uz", "fo", "ht", "ps", "tk", "nn",
    "mt", "sa", "lb", "my", "bo", "tl", "mg", "as", "tt", "haw", "ln", "ha", "ba", "jv",
    "su", "yue",
)  # fmt: skip
"""The model's 100 languages as BCP-47 primary subtags, in its tokenizer's
order (English first)."""
JAVANESE = ("jv", "jw")
"""Javanese, the one language whose Whisper code is not its BCP-47 subtag."""

_WHISPER = engine("whisper")
if _WHISPER is None:  # pragma: no cover - the catalogue ships with the engine
    raise ImportError("voiced.models.CATALOGUE has no whisper engine")
MODELS = tuple(m.id for m in _WHISPER.models)
"""The checkpoints in the catalogue; the engine refuses any other turbo
repository of mlx-community's (``whisper-large-v3-turbo`` itself is
mlx-whisper's layout, which mlx-audio cannot load)."""
_REFUSED = "mlx-community/whisper-large-v3-turbo"

# Measurements, not derivations: see ADR 0032 before changing any of these.
MIN_SAMPLES = 1600
"""Input under 100 ms is nothing to transcribe."""
PAUSE_MS = 2000
"""A pause up to this long stays inside one span of speech; a longer one
separates two spans, each decoded on its own, so the model never sees the
silence between them (it writes words for it)."""
PAD_S = 0.3
"""Audio kept before and after each span, for Silero's onset and the last
word's tail."""
WARMUP_S = 1.0

_SR = STT_SAMPLE_RATE


class WhisperEngine(STTEngine):
    name = "whisper"
    model_id = default_model("whisper").id
    languages = list(LANGUAGES)
    license = default_model("whisper").license

    def __init__(self, model_id: str | None = None) -> None:
        super().__init__(model_id)
        if self.model_id.startswith(_REFUSED) and self.model_id not in MODELS:
            raise ModelRefused(
                f"{self.model_id} is not in voiced's model catalogue; its Whisper models "
                f"are {', '.join(MODELS)}"
            )
        self._model: Any = None
        self._thread = ModelThread("whisper")
        self._session: BufferedStreamSession | None = None

    @classmethod
    def normalize_language(cls, language: str | None) -> str | None:
        """``"pt-BR"`` to ``"pt"``; raise :class:`UnsupportedLanguage` if not supported."""
        if language is None:
            return None
        code = primary_subtag(language.strip())
        if code not in LANGUAGES:
            raise UnsupportedLanguage(language)
        return code

    async def load(self) -> None:
        await self._thread.run(self._load_blocking)

    async def unload(self) -> None:
        await unload_on(self._thread, self._drop)

    def _drop(self) -> None:
        self._model = None

    def _load_blocking(self) -> None:
        """Build in a local and assign last, so a failure part-way leaves the
        engine unloaded."""
        from mlx_audio.stt.utils import load_model

        from voiced.models import snapshot

        t0 = time.perf_counter()
        log.info("loading STT model %s", self.model_id)
        model = load_model(snapshot(self.model_id, check_only=True))
        load_s = time.perf_counter() - t0
        # Warm up so the first request does not pay for Metal kernel
        # compilation, and so a broken GPU path fails here.
        t1 = time.perf_counter()
        _generate(model, np.zeros(round(WARMUP_S * _SR), dtype=np.float32), "en", words=True)
        self._model = model
        log.info("STT model ready: load %.1fs, warm-up %.1fs", load_s, time.perf_counter() - t1)

    def _require_idle(self) -> None:
        if self._model is None:
            raise EngineNotLoaded(self.name)
        if self._session is not None:
            raise EngineError("a stream session is open on this engine")

    async def transcribe(self, audio: AudioInput, language: str | None = None) -> Transcript:
        self._require_idle()
        code = self.normalize_language(language)
        return await self._thread.run(self._transcribe_blocking, audio, code)

    async def stream(self, language: str | None = None) -> STTStreamSession:
        """A buffering session; one at a time (:attr:`max_streams` is 1).
        A turn's decode leaves out word timestamps (another encoder pass on
        the critical path) and takes the GPU lock for each span, since a
        client is waiting for it (ADR 0019)."""
        self._require_idle()
        code = self.normalize_language(language)
        session = BufferedStreamSession(
            self._thread,
            lambda audio, stop: self._transcribe_blocking(
                audio, code, words=False, turn=stt_turn, stop=stop
            ),
            self._session_closed,
        )
        self._session = session
        return session

    def _session_closed(self, session: BufferedStreamSession) -> None:
        if self._session is session:
            self._session = None

    def _transcribe_blocking(
        self,
        audio: AudioInput,
        code: str | None,
        *,
        words: bool = True,
        turn: Turn = nullcontext,
        stop: Callable[[], bool] = lambda: False,
    ) -> Transcript:
        """On the model thread. Batch and stream input both enter here, so
        both pass the same gates: :func:`stt_input`, then Silero's spans of
        speech, each decoded on its own with ``turn`` held around it; without
        a hint the first span's language is used for the rest. ``stop`` ends
        it between spans."""
        import mlx.core as mx

        samples = stt_input(audio, min_samples=MIN_SAMPLES)
        duration = audio.duration_s
        if samples is None:
            return Transcript(text="", language=code, duration_s=duration)
        if stop():
            return Transcript(text="", language=code, duration_s=duration)
        language = code
        segments: list[Segment] = []
        try:
            for start_s, end_s in speech_spans(samples, silence_ms=PAUSE_MS):
                if stop():
                    break
                start = max(0, round((start_s - PAD_S) * _SR))
                clip = samples[start : round((end_s + PAD_S) * _SR)]
                with turn():
                    result = _generate(self._model, clip, language, words=words)
                if language is None and result.language:
                    language = JAVANESE[0] if result.language == JAVANESE[1] else result.language
                offset = start / _SR
                for segment in result.segments:
                    text = collapse_whitespace(segment["text"])
                    if text:
                        segments.append(
                            clip_segment(
                                offset + segment["start"], offset + segment["end"], text, duration
                            )
                        )
        finally:
            mx.clear_cache()
        return Transcript(
            text=" ".join(s.text for s in segments),
            language=language,
            duration_s=duration,
            segments=tuple(segments),
        )


def _generate(model: Any, samples: NDArray[np.float32], code: str | None, *, words: bool) -> Any:
    """Blocking: Whisper's own transcribe loop over one span, greedy with its
    temperature fallback; with ``words``, segment times from word timestamps."""
    return model.generate(
        samples,
        language=JAVANESE[1] if code == JAVANESE[0] else code,
        word_timestamps=words,
    )
