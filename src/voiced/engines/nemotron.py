"""Nemotron streaming speech-to-text through mlx-audio's ``nemotron_asr``. Selectable,
not the default.

NVIDIA's Nemotron ASR models are cache-aware FastConformer-RNNT: the encoder
keeps its attention and convolution caches between chunks, so each chunk of
audio is encoded once and a streaming call costs the same at the end of a
long utterance as at its start (Parakeet's right context is re-encoded on
every call; ADR 0008). This engine (see ``docs/decisions/0025-stt-nemotron.md``):

* streams through mlx-audio's own session (``create_streaming_session``,
  ``feed``, ``step``, ``close``) in pieces that end on encoder chunk
  boundaries, so each piece is encoded when it arrives, ``partial()`` is the
  text up to the last piece and ``finalize()`` encodes only the remainder;
* runs every MLX call on its one :class:`~voiced.threads.ModelThread`, the
  finalize under :func:`voiced.gpu.stt_turn` (ADR 0019);
* transcribes a whole upload with the model's widest look-ahead (1.12 s),
  the most accurate setting, in sentence segments from the RNNT frame
  indices. A streaming transcript is one segment: the session's text deltas
  carry no times.

Its models and the locales each serves are in ``voiced.models.catalogue``:
Nemotron 3.5 (multilingual, language ID, OpenMDW-1.1) is the default, and
the English model (NVIDIA Open Model License) is selectable. Everything that
imports ``mlx_audio`` runs inside ``load()``, so this module imports (the
entry-point registry does so) without the ``stt`` extra.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, cast

import numpy as np
from numpy.typing import NDArray

from voiced.audio import check_samples, clip_segment, stt_input
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
from voiced.engines.memory import release_memory
from voiced.engines.registry import ModelRefused
from voiced.gpu import stt_turn
from voiced.models import default_model, engine, find
from voiced.text import collapse_whitespace, primary_subtag
from voiced.threads import ModelThread

__all__ = ["NemotronEngine", "NemotronStreamSession"]

log = logging.getLogger(__name__)

_CATALOGUED = engine("nemotron")
MODELS = tuple(m.id for m in _CATALOGUED.models) if _CATALOGUED is not None else ()
REPOSITORIES = ("nvidia/nemotron", "mlx-community/nemotron")
"""Where Nemotron models live: one of them the catalogue does not list is refused."""
AUTO = "auto"
"""Nemotron 3.5's language-ID prompt, used when no language is given. A model
without language prompts (the English one) ignores it."""

CHUNK_MS = 160
"""The streaming chunk: two 80 ms encoder frames, the second the first's
look-ahead. ADR 0025 has the measurements behind the choice."""
LOOKAHEAD_FRAMES = {80: 0, 160: 1, 320: 3, 560: 6, 1120: 13}
"""Right context, in 80 ms encoder frames, of each chunk size the models were
trained with (the model cards)."""
BATCH_LOOKAHEAD_FRAMES = LOOKAHEAD_FRAMES[1120]
"""A whole upload is transcribed at the widest look-ahead: the most accurate."""
DECODE_BUDGET = 1_000_000
"""``step``'s budget of joint evaluations: enough to decode everything one
chunk encoded, so a call never leaves frames for the next."""
WARMUP_S = 1.0

_SR = STT_SAMPLE_RATE


class NemotronEngine(STTEngine):
    name = "nemotron"
    model_id = default_model("nemotron").id
    languages = list(default_model("nemotron").languages)
    license = default_model("nemotron").license
    max_streams = 1

    def __init__(self, model_id: str | None = None) -> None:
        super().__init__(model_id)
        found = find(self.model_id)
        if found is None and self.model_id.startswith(REPOSITORIES):
            raise ModelRefused(
                f"{self.model_id} is not in voiced's model catalogue; its Nemotron models "
                f"are {', '.join(MODELS)}"
            )
        if found is not None and found[1].languages:
            self.languages = list(found[1].languages)
        self._model: Any = None
        self._first = self._chunk = 0  # samples per push, from the model at load
        self._thread = ModelThread("nemotron")
        self._session: NemotronStreamSession | None = None
        self._clearing: asyncio.Task[None] | None = None

    def prompt(self, language: str | None) -> str:
        """The model's language prompt for a BCP-47 hint: the served locale
        itself, or the first of its language; :data:`AUTO` without a hint.
        Raise :class:`UnsupportedLanguage` for a language not served."""
        if language is None:
            return AUTO
        tag = language.strip().replace("_", "-").lower()
        served = [loc for loc in self.languages if primary_subtag(loc) == primary_subtag(tag)]
        if not served:
            raise UnsupportedLanguage(language)
        return next((loc for loc in served if loc.lower() == tag), served[0])

    async def load(self) -> None:
        await self._thread.run(self._load_blocking)

    async def unload(self) -> None:
        await self._thread.run(self._unload_blocking)

    def _unload_blocking(self) -> None:
        self._model = None
        release_memory()

    def _load_blocking(self) -> None:
        import mlx.core as mx
        from mlx.utils import tree_flatten
        from mlx_audio.stt.models.nemotron_asr import Model
        from mlx_audio.stt.utils import load_model

        from voiced.models import snapshot

        t0 = time.perf_counter()
        log.info("loading STT model %s", self.model_id)
        model = load_model(str(snapshot(self.model_id, check_only=True)), lazy=True)
        if not isinstance(model, Model):
            raise RuntimeError(f"{self.model_id} is not a Nemotron ASR model")
        # NVIDIA's English checkpoint is fp32: 2.5 GB, over the memory budget
        # beside Kokoro, and bf16 has the same word error rate (ADR 0025).
        # Loaded lazily and evaluated one parameter at a time, the fp32 copy
        # is never whole in memory.
        model.set_dtype(mx.bfloat16)
        parameters = cast(list[tuple[str, Any]], tree_flatten(model.parameters()))
        for _, parameter in parameters:
            mx.eval(parameter)
        if model.prompt_kernel is None and len({primary_subtag(t) for t in self.languages}) > 1:
            raise RuntimeError(
                f"{self.model_id} has no language prompts: name an English-only "
                f"checkpoint by its catalogue id ({', '.join(MODELS)})"
            )
        if model.prompt_kernel is not None:
            missing = [loc for loc in self.languages if loc not in model.prompt_dictionary]
            if missing:
                raise RuntimeError(f"{self.model_id} has no language prompt for {missing}")
        # Every session the model creates streams at CHUNK_MS (the library
        # reads the attribute; create_streaming_session takes no context size).
        left = model.default_att_context_size[0]
        model.default_att_context_size = [left, LOOKAHEAD_FRAMES[CHUNK_MS]]
        load_s = time.perf_counter() - t0
        t1 = time.perf_counter()
        _generate(model, np.zeros(round(WARMUP_S * _SR), dtype=np.float32), AUTO)
        warm = _Decoder(model, AUTO)
        warm.push(np.zeros(warm.first + warm.chunk, dtype=np.float32))
        warm.finish(np.zeros(0, dtype=np.float32))
        _clear_cache()
        self._model, self._first, self._chunk = model, warm.first, warm.chunk
        log.info(
            "STT model ready: load %.1fs, warm-up %.1fs, %.2f GB",
            load_s,
            time.perf_counter() - t1,
            mx.get_active_memory() / 1e9,
        )

    def _require_idle(self) -> Any:
        if self._model is None:
            raise EngineNotLoaded(self.name)
        if self._session is not None:
            raise EngineError("a stream session is open on this engine")
        return self._model

    async def transcribe(self, audio: AudioInput, language: str | None = None) -> Transcript:
        model = self._require_idle()
        prompt = self.prompt(language)
        samples = stt_input(audio)
        if samples is None:
            return Transcript(text="", language=_code(language), duration_s=audio.duration_s)
        result = await self._thread.run(_generate, model, samples, prompt)
        return _to_transcript(result, _code(language), audio.duration_s)

    async def stream(self, language: str | None = None) -> STTStreamSession:
        self._require_idle()
        self._session = NemotronStreamSession(self, self.prompt(language), _code(language))
        return self._session

    def _session_closed(self, session: NemotronStreamSession) -> None:
        if self._session is not session:
            return
        self._session = None
        # A stream leaves over 1 GB in MLX's buffer cache. Emptying it takes
        # long enough to show in a transcript (Wyoming closes the session
        # before it sends one), so it is queued on the model thread, not awaited.
        self._clearing = asyncio.create_task(self._thread.run(_clear_cache))
        self._clearing.add_done_callback(_log_failure)


class _Decoder:
    """The only code that touches mlx-audio's ``NemotronStreamingSession``.
    Create, ``push`` and ``finish`` it on the model thread: the session's
    caches are MLX arrays.

    The library's log-mel front end emits a frame once the audio covers half
    an FFT window past its centre, and its encoder takes whole chunks of mel
    frames. Fed whole chunks of audio, the encoder would always hold back the
    last one, encoding it only when the next arrives. So a session is fed
    :attr:`first` samples first, then :attr:`chunk` at a time: each ends
    exactly on a chunk of frames, and is encoded as it is fed.
    """

    def __init__(self, model: Any, prompt: str) -> None:
        self._session = model.create_streaming_session(language=prompt)
        hop = model.preprocessor_config.hop_length
        encoder, frontend = self._session._encoder, self._session._frontend
        self.chunk = int(encoder.chunk_mel * hop)
        self.first = int((encoder.first_chunk_mel - 1) * hop + frontend.lookahead_samples)
        self.text = ""

    def push(self, samples: NDArray[np.float32]) -> None:
        """Encode and decode ``samples``: one piece of :attr:`first` or
        :attr:`chunk` samples, or the rest at :meth:`finish`. The library
        ingests at most :attr:`chunk` samples per step."""
        for start in range(0, len(samples), self.chunk):
            self._session.feed(samples[start : start + self.chunk])
            self._step()

    def finish(self, samples: NDArray[np.float32]) -> str:
        """Encode the rest, close the session and decode its last frames."""
        with stt_turn():
            self.push(samples)
            self._session.close()
            while not self._session.done:
                self._step()
        return self.text

    def _step(self) -> None:
        self.text += "".join(self._session.step(max_decode_tokens=DECODE_BUDGET))


class NemotronStreamSession(STTStreamSession):
    """One streaming transcription. ``add_audio`` hands each piece that ends
    on an encoder chunk to the model thread and keeps the rest;
    ``finalize()`` encodes the rest and returns the transcript. Use
    ``async with``."""

    def __init__(self, engine: NemotronEngine, prompt: str, language: str | None) -> None:
        self._engine = engine
        self._prompt = prompt
        self._language = language
        self._decoder: _Decoder | None = None  # made on the model thread
        self._rest = np.zeros(0, dtype=np.float32)
        self._next = engine._first
        self._samples = 0
        self._closed = False

    async def add_audio(self, samples: NDArray[np.float32]) -> None:
        self._check_open()
        arr = check_samples(samples)
        self._samples += len(arr)
        self._rest = np.concatenate([self._rest, arr])
        while len(self._rest) >= self._next:
            piece, self._rest = self._rest[: self._next], self._rest[self._next :]
            self._next = self._engine._chunk
            await self._engine._thread.run(self._push, piece)

    def partial(self) -> str:
        return "" if self._decoder is None else collapse_whitespace(self._decoder.text)

    async def finalize(self) -> Transcript:
        self._check_open()
        self._closed = True
        rest, self._rest = self._rest, np.zeros(0, dtype=np.float32)
        text = collapse_whitespace(await self._engine._thread.run(self._finish, rest))
        duration = self._samples / _SR
        segments = (Segment(0.0, round(duration, 3), text),) if text else ()
        return Transcript(
            text=text, language=self._language, duration_s=duration, segments=segments
        )

    async def close(self) -> None:
        self._closed = True
        self._decoder = None
        self._engine._session_closed(self)

    def _push(self, piece: NDArray[np.float32]) -> None:
        """On the model thread. A session closed meanwhile opens nothing."""
        if not self._closed:
            self._open_decoder().push(piece)

    def _finish(self, rest: NDArray[np.float32]) -> str:
        """On the model thread."""
        return self._open_decoder().finish(rest)

    def _open_decoder(self) -> _Decoder:
        if self._decoder is None:
            self._decoder = _Decoder(self._engine._model, self._prompt)
        return self._decoder

    def _check_open(self) -> None:
        if self._closed:
            raise EngineError("stream session is closed")


# --- helpers ------------------------------------------------------------------


def _generate(model: Any, samples: NDArray[np.float32], prompt: str) -> Any:
    """Blocking batch transcription at the widest look-ahead. The library
    streams the encoder over 30 s mel chunks, so memory does not grow with
    the input; MLX's buffer cache is emptied afterwards."""
    left = model.default_att_context_size[0]
    try:
        return model.generate(
            samples, language=prompt, att_context_size=[left, BATCH_LOOKAHEAD_FRAMES]
        )
    finally:
        _clear_cache()


def _clear_cache() -> None:
    """Return MLX's freed buffers to the system, after each request."""
    import mlx.core as mx

    mx.clear_cache()


def _log_failure(task: asyncio.Task[None]) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.warning("emptying MLX's buffer cache failed: %r", task.exception())


def _to_transcript(result: Any, language: str | None, duration_s: float) -> Transcript:
    segments: list[Segment] = []
    for sentence in result.sentences:
        text = collapse_whitespace(sentence.text)
        if not text:
            continue
        # Timed by its words: the model emits a sentence's closing
        # punctuation after its look-ahead, up to about a second late, and
        # the next sentence's leading space with it.
        words = [t for t in sentence.tokens if any(c.isalnum() for c in t.text)]
        first, last = (words[0], words[-1]) if words else (sentence, sentence)
        segments.append(clip_segment(float(first.start), float(last.end), text, duration_s))
    return Transcript(
        text=collapse_whitespace(result.text),
        language=language,
        duration_s=duration_s,
        segments=tuple(segments),
    )


def _code(language: str | None) -> str | None:
    """A hint's primary subtag, what the transcript reports."""
    return None if language is None else primary_subtag(language.strip())
