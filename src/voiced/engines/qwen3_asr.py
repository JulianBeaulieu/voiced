"""Qwen3-ASR speech-to-text through mlx-audio. Selectable, not the default.

Qwen3-ASR is an audio encoder in front of a Qwen3 language model that writes
the transcript token by token, in 30 languages, detecting the language when
it is not given. It has no live streaming on MLX, so this engine (see
``docs/decisions/0027-stt-qwen3-asr.md``) is the file-transcription and
language-coverage engine, not the push-to-talk one:

* :meth:`Qwen3ASREngine.transcribe` transcribes in pieces of about
  :data:`CHUNK_S`, cut at the quietest point within 5 s of each limit
  (mlx-audio's own splitter), each decoded on its own, and aligns each
  piece's words to the audio with Qwen3-ForcedAligner-0.6B, giving one
  segment per sentence; a language the aligner does not cover, or a failed
  alignment, gets one segment per piece;
* a stream session (:class:`~voiced.engines.buffered.BufferedStreamSession`)
  only buffers: ``partial()`` is always ``""`` and
  ``finalize()`` transcribes the whole turn without the aligner, holding
  :func:`voiced.gpu.stt_turn` around each piece because the client is
  waiting for it, so finalize grows with the length of the turn;
* every MLX call runs on the engine's one :class:`ModelThread`, one session
  at a time (:attr:`max_streams` is 1).

Language hints are BCP-47 (``"de"``, ``"pt-BR"``) and are mapped to the
model's language names (:data:`LANGUAGES`); the detected language comes back
as a BCP-47 tag in :attr:`Transcript.language`.

Everything that imports mlx-audio runs inside ``load()``, so this module
imports (the entry-point registry does so) without it.
"""

from __future__ import annotations

import importlib.util
import logging
import time
from collections.abc import Callable
from contextlib import nullcontext
from typing import Any

import numpy as np
from numpy.typing import NDArray

from voiced.audio import is_silent, stt_input
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
from voiced.engines.registry import ModelRefused
from voiced.gpu import Turn, stt_turn
from voiced.models import default_model, engine
from voiced.text import collapse_whitespace, primary_subtag, split_sentences
from voiced.threads import ModelThread

log = logging.getLogger(__name__)

__all__ = ["ALIGNER_REPO", "LANGUAGES", "Qwen3ASREngine"]

# BCP-47 primary subtag to the model's name for it: the 30 entries of
# ``support_languages`` in the checkpoints' config.json, English first.
LANGUAGES: dict[str, str] = {
    "en": "English",
    "zh": "Chinese",
    "yue": "Cantonese",
    "ar": "Arabic",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "pt": "Portuguese",
    "id": "Indonesian",
    "it": "Italian",
    "ko": "Korean",
    "ru": "Russian",
    "th": "Thai",
    "vi": "Vietnamese",
    "ja": "Japanese",
    "tr": "Turkish",
    "hi": "Hindi",
    "ms": "Malay",
    "nl": "Dutch",
    "sv": "Swedish",
    "da": "Danish",
    "fi": "Finnish",
    "pl": "Polish",
    "cs": "Czech",
    "fil": "Filipino",
    "fa": "Persian",
    "el": "Greek",
    "ro": "Romanian",
    "hu": "Hungarian",
    "mk": "Macedonian",
}
_CODES = {name.lower(): code for code, name in LANGUAGES.items()}

_QWEN3 = engine("qwen3-asr")
if _QWEN3 is None:  # pragma: no cover - the catalogue ships with the engine
    raise ImportError("voiced.models.CATALOGUE has no qwen3-asr engine")
MODELS = tuple(m.id for m in _QWEN3.models)
"""The checkpoints in the catalogue; the engine refuses any other Qwen3-ASR
repository of mlx-community's."""
ALIGNER_REPO = _QWEN3.shared[0].id
_TOKENIZERS = {"japanese": "nagisa", "korean": "soynlp"}
"""The aligner's languages that need a word tokenizer voiced does not
install; they are aligned only where it is importable (ADR 0027)."""

# Measurements, not derivations: see ADR 0027 before changing any of these.
CHUNK_S = 30.0
"""Length of the pieces transcribed and aligned one at a time, give or take
the 5 s mlx-audio's splitter searches for a quiet cut. On 140 s of speech,
30 s pieces kept every word with a 2.2 GB MLX peak; 60 s pieces lost a
tenth of them, and a 300 s piece looped until the token bound."""
TOKENS_PER_S = 20
"""Bound on the tokens written per second of a piece, so a decoder that
loops stops within that piece; English speech took 4 a second."""
MIN_TOKENS = 64
WARMUP_S = 1.0
MIN_SAMPLES = 1600
"""Input under 100 ms is nothing to transcribe."""

_SR = STT_SAMPLE_RATE


class Qwen3ASREngine(STTEngine):
    name = "qwen3-asr"
    model_id = default_model("qwen3-asr").id
    languages = list(LANGUAGES)
    license = default_model("qwen3-asr").license

    def __init__(self, model_id: str | None = None) -> None:
        super().__init__(model_id)
        if self.model_id.startswith("mlx-community/Qwen3-ASR-") and self.model_id not in MODELS:
            raise ModelRefused(
                f"{self.model_id} is not in voiced's model catalogue; its Qwen3-ASR models "
                f"are {', '.join(MODELS)}"
            )
        self._model: Any = None
        self._aligner: Any = None
        self._split: Any = None  # mlx-audio's splitter, imported with the model
        self._aligned: set[str] = set()  # the aligner's languages, lower case
        self._thread = ModelThread("qwen3-asr")
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

    def _load_blocking(self) -> None:
        """Build in locals and assign last, so a failure part-way leaves the
        engine unloaded."""
        from mlx_audio.stt.models.qwen3_asr.qwen3_asr import split_audio_into_chunks
        from mlx_audio.stt.utils import load_model

        from voiced.models import ModelNotDownloaded, pinned_revision, snapshot

        t0 = time.perf_counter()
        log.info("loading STT model %s", self.model_id)
        root = snapshot(self.model_id, check_only=True)
        try:
            aligner_root = snapshot(ALIGNER_REPO, check_only=True)
        except ModelNotDownloaded as exc:
            # Downloading the model fetches its shared aligner (ADR 0022).
            revision = pinned_revision(self.model_id)
            raise ModelNotDownloaded(self.model_id, revision, (ALIGNER_REPO,)) from exc
        model = load_model(root)
        aligner = load_model(aligner_root)
        load_s = time.perf_counter() - t0
        # Warm up both models so the first request does not pay for Metal
        # kernel compilation, and so a broken GPU path fails here.
        t1 = time.perf_counter()
        warm = np.zeros(round(WARMUP_S * _SR), dtype=np.float32)
        _decode(model, warm, LANGUAGES["en"])
        aligner.generate(warm, text="hello", language=LANGUAGES["en"])
        supported = {name.lower() for name in aligner.get_supported_languages() or []}
        self._aligned = {name for name in supported if _tokenizer_installed(name)}
        self._model, self._aligner, self._split = model, aligner, split_audio_into_chunks
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
        """A buffering session; one at a time (:attr:`max_streams` is 1)."""
        self._require_idle()
        code = self.normalize_language(language)
        # No aligner on a stream's turn, and each piece under the GPU lock,
        # since a client is waiting for it (ADR 0019).
        session = BufferedStreamSession(
            self._thread,
            lambda audio, stop: self._transcribe_blocking(
                audio, code, align=False, turn=stt_turn, stop=stop
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
        align: bool = True,
        turn: Turn = nullcontext,
        stop: Callable[[], bool] = lambda: False,
    ) -> Transcript:
        """On the model thread: each piece of about :data:`CHUNK_S` decoded
        on its own, with its own token bound and, without a hint, its own
        detected language; ``turn`` is held around each decode, and a silent
        piece is skipped. Batch and stream input both enter here, so both pass
        the same :func:`stt_input` gate."""
        import mlx.core as mx

        samples = stt_input(audio, min_samples=MIN_SAMPLES)
        duration = audio.duration_s
        if samples is None:
            return Transcript(text="", language=code, duration_s=duration)
        name = LANGUAGES[code] if code else None
        texts: list[str] = []
        segments: list[Segment] = []
        detected = code
        try:
            for piece, offset in self._split(samples, sr=_SR, chunk_duration=CHUNK_S):
                if stop():
                    break
                if is_silent(piece):
                    continue
                with turn():
                    result = _decode(self._model, piece, name)
                text = collapse_whitespace(result.text)
                if not text:
                    continue
                language = name or str(result.segments[0]["language"])
                detected = detected or _code(language)
                # mlx-audio pads a last piece under 1 s, so its end can pass the input's.
                end = min(offset + len(piece) / _SR, duration)
                segments += self._piece_segments(samples, offset, end, text, language, align)
                texts.append(text)
            return Transcript(
                text=" ".join(texts),
                language=detected,
                duration_s=duration,
                segments=tuple(segments),
            )
        finally:
            mx.clear_cache()

    def _piece_segments(
        self,
        samples: NDArray[np.float32],
        start: float,
        end: float,
        text: str,
        language: str,
        align: bool,
    ) -> list[Segment]:
        """One piece as sentence segments with the aligner's word times, or
        as one segment when not aligning, when the language is not the
        aligner's, or when aligning fails."""
        whole = [Segment(start=round(start, 3), end=round(end, 3), text=text)]
        if not align or language.lower() not in self._aligned:
            return whole
        try:
            sentences = self._align(samples[round(start * _SR) : round(end * _SR)], text, language)
        except Exception:
            log.warning(
                "qwen3-asr: aligning a %s piece failed; one segment", language, exc_info=True
            )
            return whole
        if sentences is None:
            return whole  # the aligner's words do not match the text
        span = end - start
        return [
            Segment(
                start=round(start + min(first, span), 3),
                end=round(start + min(last, span), 3),
                text=sentence,
            )
            for sentence, first, last in sentences
        ]

    def _align(
        self, audio: NDArray[np.float32], text: str, language: str
    ) -> list[tuple[str, float, float]] | None:
        """``text``'s sentences with their first and last word's times in
        ``audio``, counting words with the aligner's own tokenizer; None
        when the words do not add up to the text."""
        words = self._aligner.generate(audio, text=text, language=language).items
        processor = self._aligner.aligner_processor
        sentences = []
        for sentence in split_sentences(text):
            count = len(processor.encode_timestamp(sentence, language)[0])
            spoken, words = words[:count], words[count:]
            if count == 0 or len(spoken) < count:
                return None
            first = max(0.0, float(spoken[0].start_time))
            sentences.append((sentence, first, max(float(w.end_time) for w in spoken)))
        return sentences if not words else None


def _decode(model: Any, piece: NDArray[np.float32], language: str | None) -> Any:
    """Blocking: one piece, greedy, with its token bound. The piece is already
    the length mlx-audio would split at, so it is decoded whole."""
    budget = max(MIN_TOKENS, round(TOKENS_PER_S * len(piece) / _SR))
    return model.generate(piece, language=language, chunk_duration=float("inf"), max_tokens=budget)


def _code(name: object) -> str | None:
    """The model's language name to its BCP-47 subtag, or None."""
    return _CODES.get(str(name).lower()) if name else None


def _tokenizer_installed(language: str) -> bool:
    module = _TOKENIZERS.get(language)
    return module is None or importlib.util.find_spec(module) is not None
