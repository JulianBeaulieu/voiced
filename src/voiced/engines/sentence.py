"""Base class for TTS engines that synthesize one sentence at a time.

It owns the parts every such engine repeats: ``load()`` through
``run_blocking``, the loaded check, the voice and speed checks before the
first yield, and the split-synthesize-yield loop with each model call in
``run_blocking`` (so a cancelled request cannot release the inference slot
while the GPU is still busy). A subclass supplies:

* ``_load_sync()``, blocking, run in a worker thread: builds the model in
  locals, warms up, and assigns ``self._model`` last, so a failure leaves the
  engine unloaded;
* ``_has_voice(voice)``, run on the event loop, so a cheap lookup: whether
  ``voice`` is one of ``voices()``;
* ``_synth_sentence(sentence, voice, speed)``, blocking, run in a worker
  thread: int16 PCM at :data:`TTS_SAMPLE_RATE` for one sentence, or ``b""``
  for nothing to say.

``MIN_SPEED`` and ``MAX_SPEED`` bound ``speed``. An engine without rate
control may ignore ``speed`` within the bounds, as ``TTSEngine.synthesize``
allows.
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

__all__ = ["SentenceTTSEngine"]

WARMUP_TEXT = "Warming up."


MLX_CACHE_LIMIT_BYTES = 256 * 1024 * 1024
"""Upper bound on MLX's cache of freed GPU buffers in a TTS process.

MLX keeps freed buffers for reuse. A sentence-at-a-time engine allocates
differently shaped buffers for every sentence, so without a bound the cache
only grows: the Kokoro service held 1.8 GB of cache after warm-up and 4.5 GB
after one 70-word reply, all in its footprint. Clearing the cache after every
sentence keeps it at zero but makes each sentence allocate afresh, which added
about 14 ms to Kokoro's first audio. A cap keeps enough buffers to reuse: at
256 MB first audio is within 0 to 3 ms of an unbounded cache and the Kokoro
service's footprint is about 0.7 GB. The limit is process-wide, and a TTS
process runs one engine.
"""


def _limit_mlx_cache() -> None:
    """Cap MLX's buffer cache. A no-op without MLX (the fake engines on CI)."""
    try:
        import mlx.core as mx
    except ImportError:
        return
    mx.set_cache_limit(MLX_CACHE_LIMIT_BYTES)


def _clear_mlx_cache() -> None:
    """Drop the buffers MLX holds in its cache. A no-op without MLX."""
    try:
        import mlx.core as mx
    except ImportError:
        return
    mx.clear_cache()


class SentenceTTSEngine(TTSEngine):
    MIN_SPEED: ClassVar[float] = 0.25
    MAX_SPEED: ClassVar[float] = 4.0

    def __init__(self, model_id: str | None = None) -> None:
        super().__init__(model_id)
        self._model: Any = None

    async def load(self) -> None:
        await run_blocking(self._load_and_bound_cache)

    def _load_and_bound_cache(self) -> None:
        # The cap is set first so the warm-up's transient is bounded too; the
        # clear then drops what the load and warm-up left below it.
        _limit_mlx_cache()
        try:
            self._load_sync()
        finally:
            _clear_mlx_cache()

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
            raise EngineError(f"speed must be between {self.MIN_SPEED} and {self.MAX_SPEED}")
        for sentence in split_sentences(text):
            pcm = await run_blocking(self._synth_sentence, sentence, voice, speed)
            if pcm:
                yield PCMChunk(data=pcm, sample_rate=TTS_SAMPLE_RATE)

    def _check_loaded(self) -> None:
        if self._model is None:
            raise EngineNotLoaded(self.name)
