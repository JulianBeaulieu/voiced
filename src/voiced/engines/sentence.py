"""Base class for TTS engines that synthesize one sentence at a time.

It owns the parts every such engine repeats: ``load()``, the loaded check,
the voice and speed checks before the first yield, and the
split-synthesize-yield loop. A subclass supplies:

* ``_load_sync()``, blocking: builds the model in locals, warms up, and
  assigns ``self._model`` last, so a failure leaves the engine unloaded;
* ``_has_voice(voice)``, run on the event loop, so a cheap lookup: whether
  ``voice`` is one of ``voices()``;
* ``_synth_sentence(sentence, voice, speed)``, blocking: int16 PCM at
  :data:`TTS_SAMPLE_RATE` for one sentence, or ``b""`` for nothing to say.

An engine that produces a sentence's audio in pieces also overrides
``_synth_chunks(sentence, voice, speed)``, a blocking generator of int16 PCM
pieces, so the first piece goes out before the sentence is done. By default
it is the sentence from ``_synth_sentence`` as one piece.

Every blocking step, the load, each ``next()`` of a sentence's pieces and
their ``close()``, runs on the engine's one
:class:`~voiced.threads.ModelThread`. MLX arrays are bound to the thread that
made them, and a streaming engine keeps them from one piece of a sentence to
the next (Pocket TTS's attention cache and decoder state); one thread makes
that safe for every engine. Like :func:`~voiced.threads.run_blocking`, a
cancelled request waits for the step in flight, so it cannot release the
inference slot while the model is still busy; it calls ``_interrupt()``
first, so an engine whose one step is a whole sentence can cut it short. A
TTS process runs one request at a time (the exclusive slot), so the one
thread costs nothing.

``MIN_SPEED`` and ``MAX_SPEED`` bound ``speed``. An engine without rate
control may ignore ``speed`` within the bounds, as ``TTSEngine.synthesize``
allows.
"""

from __future__ import annotations

import logging
import math
from abc import abstractmethod
from collections.abc import AsyncIterator, Generator
from dataclasses import dataclass
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
from voiced.threads import ModelThread

log = logging.getLogger(__name__)

__all__ = ["SentenceBound", "SentenceTTSEngine", "sentence_bound"]

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


SECONDS_PER_CHAR = 0.12
"""Upper bound on speech per character of text. Normal English runs about
0.07 s per character; this leaves room for slow delivery."""
EXTRA_SECONDS = 1.0
"""Added to the bound for very short sentences. One-word replies that stop on
their own run about 1 to 1.8 s (Soprano), so 1 s keeps most of them whole."""


@dataclass(frozen=True, slots=True)
class SentenceBound:
    """How long one sentence may run: ``tokens`` for generation, ``seconds``
    for the audio. They are the same bound in two units; see
    :func:`sentence_bound`."""

    tokens: int
    seconds: float


def sentence_bound(sentence: str, token_seconds: float) -> SentenceBound:
    """The longest plausible reading of ``sentence``, for engines that
    generate audio tokens until they decide to stop (Soprano, Chatterbox).

    ``seconds`` is ``SECONDS_PER_CHAR`` per character plus ``EXTRA_SECONDS``.
    ``tokens`` covers ``seconds`` at ``token_seconds`` per token and is the
    generation cap, so a runaway stops early.
    """
    seconds = len(sentence) * SECONDS_PER_CHAR + EXTRA_SECONDS
    return SentenceBound(tokens=max(1, math.ceil(seconds / token_seconds)), seconds=seconds)


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
        self._thread = ModelThread(self.name)

    async def load(self) -> None:
        await self._thread.run(self._load_and_bound_cache)

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
        """Blocking; runs on the model thread."""

    def _synth_chunks(
        self, sentence: str, voice: str, speed: float
    ) -> Generator[bytes, None, None]:
        """Blocking; each step runs on the model thread. The whole sentence as
        one piece unless the engine streams within a sentence."""
        yield self._synth_sentence(sentence, voice, speed)

    async def synthesize(
        self, text: str, voice: str, speed: float = 1.0
    ) -> AsyncIterator[PCMChunk]:
        """``text`` is already normalized by the caller; this only splits it
        into sentences and yields each sentence's pieces as they come."""
        self._check_loaded()
        if not self._has_voice(voice):
            raise UnknownVoice(voice)
        if not self.MIN_SPEED <= speed <= self.MAX_SPEED:
            raise EngineError(f"speed must be between {self.MIN_SPEED} and {self.MAX_SPEED}")
        for sentence in split_sentences(text):
            pieces = self._synth_chunks(sentence, voice, speed)
            try:
                while (
                    pcm := await self._thread.run_stoppable(self._interrupt, next, pieces, None)
                ) is not None:
                    if pcm:
                        yield PCMChunk(data=pcm, sample_rate=TTS_SAMPLE_RATE)
            finally:
                # Stops the engine's work for the sentence when the client goes
                # away. A failure here must not replace the exception that
                # ended the sentence (a hang-up would count as a failure).
                try:
                    await self._thread.run(pieces.close)
                except Exception:
                    log.exception("%s: closing the sentence's synthesis failed", self.name)

    def _interrupt(self) -> None:
        """Called on the event loop when the request is cancelled while a
        step of its sentence runs or is still queued; the cancel still waits
        for the step. An engine whose one step is a long model call (a whole
        sentence) asks it to return early here. State it reads should be
        made in ``_synth_chunks``, which runs on the loop before the step is
        submitted, so the queued case is covered. Nothing by default."""

    def _check_loaded(self) -> None:
        if self._model is None:
            raise EngineNotLoaded(self.name)
