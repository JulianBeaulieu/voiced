"""Base class for TTS engines that synthesize one sentence at a time.

It owns the parts every such engine repeats: ``load()`` through
``run_blocking``, the loaded check, the voice and speed checks before the
first yield, and the split-synthesize-yield loop with each model call in
``run_blocking`` (so a cancelled request cannot release the inference slot
while the GPU is still busy). A subclass supplies three blocking methods:

* ``_load_sync()`` builds the model in locals, warms up, and assigns
  ``self._model`` last, so a failure leaves the engine unloaded;
* ``_has_voice(voice)`` says whether ``voice`` is one of ``voices()``;
* ``_synth_sentence(sentence, voice, speed)`` returns int16 PCM at
  :data:`TTS_SAMPLE_RATE` for one sentence, or ``b""`` for nothing to say.

``MIN_SPEED`` and ``MAX_SPEED`` bound ``speed``; an engine without rate
control sets both to 1.0 so any other speed is a client error rather than
silently ignored.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import AsyncIterator
from typing import Any, ClassVar

from voiced.engines.base import (
    TTS_SAMPLE_RATE,
    EngineError,
    EngineNotLoaded,
    PCMChunk,
    TTSEngine,
    UnknownVoice,
)
from voiced.text.sentences import split_sentences
from voiced.threads import run_blocking

__all__ = ["WARMUP_TEXT", "SentenceTTSEngine"]

WARMUP_TEXT = "Warming up."


class SentenceTTSEngine(TTSEngine):
    MIN_SPEED: ClassVar[float] = 0.25
    MAX_SPEED: ClassVar[float] = 4.0

    def __init__(self, model_id: str | None = None) -> None:
        super().__init__(model_id)
        self._model: Any = None

    async def load(self) -> None:
        await run_blocking(self._load_sync)

    @abstractmethod
    def _load_sync(self) -> None:
        """Blocking load and warm-up; assigns ``self._model`` last."""

    @abstractmethod
    def _has_voice(self, voice: str) -> bool:
        """Whether ``voice`` is a loaded voice id."""

    @abstractmethod
    def _synth_sentence(self, sentence: str, voice: str, speed: float) -> bytes:
        """Blocking; runs in a worker thread."""

    async def synthesize(
        self, text: str, voice: str, speed: float = 1.0
    ) -> AsyncIterator[PCMChunk]:
        """``text`` is already normalized by the caller; this only splits it
        into sentences and synthesizes one chunk per sentence."""
        self._check_loaded()
        if not self._has_voice(voice):
            raise UnknownVoice(voice)
        if not self.MIN_SPEED <= speed <= self.MAX_SPEED:
            if self.MIN_SPEED == self.MAX_SPEED:
                raise EngineError(f"speed must be {self.MIN_SPEED} for {self.name}")
            raise EngineError(f"speed must be between {self.MIN_SPEED} and {self.MAX_SPEED}")
        for sentence in split_sentences(text):
            pcm = await run_blocking(self._synth_sentence, sentence, voice, speed)
            if pcm:
                yield PCMChunk(data=pcm, sample_rate=TTS_SAMPLE_RATE)

    def _check_loaded(self) -> None:
        if self._model is None:
            raise EngineNotLoaded(self.name)
