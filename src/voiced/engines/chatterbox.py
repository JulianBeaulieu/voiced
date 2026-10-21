"""Chatterbox Turbo on MLX. Selectable, not the default.

Chatterbox Turbo is Resemble AI's 350M English model: a GPT-2 language model
(T3) emits speech tokens, 25 a second of audio, and S3Gen (a flow-matching
decoder and a HiFiGAN vocoder) renders them as 24 kHz audio in the voice its
conditionals describe. This engine (see
``docs/decisions/0029-tts-chatterbox-turbo.md``):

* streams each sentence with its own loop over mlx-audio's T3 token stream
  and S3Gen's ``inference_stream``, not mlx-audio's ``stream_generate``: it
  renders the first :data:`FIRST_TOKENS` tokens as soon as they exist, then
  every :data:`CHUNK_TOKENS` more, so the first audio is early and the rest
  keeps ahead of playback, and crossfades where renders join (:func:`speak`);
* waits for the STT service's encoder calls before every GPU step, each
  token and each render (``voiced.gpu``, ADR 0019);
* bounds each sentence's tokens by its length (``sentence_bound``), so a
  runaway stops, and logs when one does;
* loads the model and the default voice from voiced's pinned snapshot
  (``voiced.models``, the catalogue), and not the speech tokenizer only voice
  cloning needs (:func:`_without_speech_tokenizer`).

There is one voice, ``default``: the conditionals the checkpoint ships
(``conds.safetensors``). Turbo has no rate control: ``speed`` is checked
against the shared bounds and otherwise ignored. mlx-audio's port applies no
watermark (upstream's PerTh one is not in it).

Everything that imports MLX runs inside ``load()``, so this module imports
(the entry-point registry does so) without the ``tts`` extra.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from typing import Any

import numpy as np
from numpy.typing import NDArray

from voiced import gpu
from voiced.audio import float32_to_pcm16
from voiced.engines.base import TTS_SAMPLE_RATE, Voice
from voiced.engines.registry import ModelRefused
from voiced.engines.sentence import WARMUP_TEXT, SentenceTTSEngine, sentence_bound
from voiced.models import default_model, engine

log = logging.getLogger(__name__)

__all__ = ["DEFAULT_VOICE", "MODELS", "ChatterboxEngine", "speak"]

_CHATTERBOX = engine("chatterbox")
if _CHATTERBOX is None:  # pragma: no cover - the catalogue ships with the engine
    raise ImportError("voiced.models.CATALOGUE has no chatterbox engine")
MODELS = tuple(m.id for m in _CHATTERBOX.models)
"""The conversions in the catalogue: the 4-bit default and fp16 (ADR 0029)."""
S3_TOKENIZER_REPO = "mlx-community/S3TokenizerV2"
"""The speech tokenizer mlx-audio fetches with every model; only cloning
from a reference clip uses it."""

DEFAULT_VOICE = Voice(id="default", name="Default", language="en", gender=None)

TOKENS_PER_SECOND = 25
FIRST_TOKENS = 15
"""Speech tokens rendered for the first audio of a sentence. Each render
re-renders the sentence so far and costs 230 to 350 ms however short, so
fewer tokens barely move the first audio and leave too little to play while
the next render runs (ADR 0029)."""
CHUNK_TOKENS = 25
"""Speech tokens added before each later render: one second of audio for
about half a second of work."""
SILENCE_TOKENS = 5
"""Silence tokens appended for the final render, as mlx-audio does."""
CROSSFADE_SAMPLES = 480
"""20 ms. Each render draws fresh noise, so it does not repeat the samples
the previous render sent; the last 20 ms of every render but the final one
are held back and faded into the next render's version of them."""

TEMPERATURE = 0.8
TOP_K = 1000
TOP_P = 0.95
REPETITION_PENALTY = 1.2
CFM_STEPS = 2
"""mlx-audio's sampling and S3Gen settings for Turbo."""

_NOTHING: NDArray[np.float32] = np.zeros(0, dtype=np.float32)

Render = Callable[[list[int], int, bool], tuple[NDArray[np.float32], int]]
"""``(tokens, first sample wanted, final) -> (samples from there, total)``."""


def max_tokens(sentence: str) -> int:
    return sentence_bound(sentence, 1 / TOKENS_PER_SECOND).tokens


def speak(
    steps: Iterator[list[int]], render: Render, wait: Callable[[], None], silence: int
) -> Iterator[NDArray[np.float32]]:
    """One sentence's audio pieces from T3's speech tokens, as ``steps``
    yields them: a render after :data:`FIRST_TOKENS`, then every
    :data:`CHUNK_TOKENS` more, then, once ``steps`` ends, a final one with
    :data:`SILENCE_TOKENS` of ``silence`` appended. ``wait`` runs before every
    step and every render, each a GPU call. Where renders join they are
    crossfaded (:class:`_Joins`).

    A step that renders nothing yields an empty piece, which the sentence
    loop skips: each ``next()`` is then one GPU call, so a client that hangs
    up frees the slot after the token in flight, not at the next render
    (ADR 0005)."""
    tokens: list[int] = []
    joins = _Joins()
    render_at = FIRST_TOKENS
    while True:
        wait()
        new = next(steps, None)
        if new is None:
            break
        tokens.extend(new)
        if len(tokens) < render_at:
            yield _NOTHING
            continue
        wait()
        yield joins.emit(*render(tokens, joins.sent, False), final=False)
        render_at = len(tokens) + CHUNK_TOKENS
    if tokens:
        wait()
        final_tokens = [*tokens, *[silence] * SILENCE_TOKENS]
        yield joins.emit(*render(final_tokens, joins.sent, True), final=True)


class _Joins:
    """Where one sentence's renders meet. Each render draws fresh noise, so
    it does not repeat what the previous one sent: the last
    :data:`CROSSFADE_SAMPLES` of every render but the final one are held
    back (``tail``) and faded into the next render's samples for the same
    span. ``sent`` is the samples yielded, the tail not included: where the
    next render's audio is wanted from."""

    def __init__(self) -> None:
        self.sent = 0
        self.tail = np.zeros(0, dtype=np.float32)

    def emit(self, audio: NDArray[np.float32], total: int, *, final: bool) -> NDArray[np.float32]:
        """What to yield of a render's ``audio`` (its samples from ``sent``
        on, ``total`` in all)."""
        if not final and audio.size <= self.tail.size + CROSSFADE_SAMPLES:
            return audio[:0]  # nothing past the held-back tail yet: wait for more tokens
        audio = _crossfade(self.tail, audio)
        if final:
            # The silence tokens add 0.2 s, so the final render reaches past
            # the tail; should it not, the rest of the tail still goes out.
            return np.concatenate([audio, self.tail[audio.size :]])
        self.tail = audio[-CROSSFADE_SAMPLES:]
        self.sent = total - CROSSFADE_SAMPLES
        return audio[:-CROSSFADE_SAMPLES]


def _crossfade(tail: NDArray[np.float32], audio: NDArray[np.float32]) -> NDArray[np.float32]:
    """``audio`` with its start faded in over ``tail``, the previous render's
    version of the same samples."""
    n = min(tail.size, audio.size)
    if n == 0:
        return audio
    ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
    out = audio.copy()
    out[:n] = tail[:n] * (1.0 - ramp) + audio[:n] * ramp
    return out


class ChatterboxEngine(SentenceTTSEngine):
    name = "chatterbox"
    model_id = default_model("chatterbox").id
    languages = ["en"]
    license = default_model("chatterbox").license

    @classmethod
    def catalogue_voices(cls) -> list[Voice]:
        return [DEFAULT_VOICE]

    def __init__(self, model_id: str | None = None) -> None:
        super().__init__(model_id)
        if self.model_id.startswith("mlx-community/chatterbox") and self.model_id not in MODELS:
            raise ModelRefused(
                f"{self.model_id} is not in voiced's model catalogue; its Chatterbox models "
                f"are {', '.join(MODELS)}"
            )

    def _load_sync(self) -> None:
        """Build in locals and assign ``_model`` last, so a failure part-way
        leaves the engine unloaded."""
        from mlx_audio.tts.utils import load_model

        from voiced.models import load_pinned

        started = time.perf_counter()
        with _without_speech_tokenizer():
            model = load_pinned(load_model, self.model_id)
        model._s3tokenizer = None  # its unloaded weights: 0.5 GB
        rate = int(model.sample_rate)
        if rate != TTS_SAMPLE_RATE:
            raise RuntimeError(f"Chatterbox model reports {rate} Hz, expected {TTS_SAMPLE_RATE}")
        if getattr(model, "_conds", None) is None:
            raise RuntimeError(f"{self.model_id} has no default voice (conds.safetensors)")
        for _ in _stream(model, WARMUP_TEXT):
            pass
        self._model = model
        log.info(
            "chatterbox: loaded %s in %.1fs, without the speech tokenizer only cloning uses",
            self.model_id,
            time.perf_counter() - started,
        )

    async def voices(self) -> list[Voice]:
        self._check_loaded()
        return [DEFAULT_VOICE]

    def _has_voice(self, voice: str) -> bool:
        return voice == DEFAULT_VOICE.id

    def _synth_sentence(self, sentence: str, voice: str, speed: float) -> bytes:
        raise NotImplementedError("Chatterbox streams each sentence: see _synth_chunks")

    def _synth_chunks(
        self, sentence: str, voice: str, speed: float
    ) -> Generator[bytes, None, None]:
        """Blocking; each step runs on the engine's model thread. ``speed`` is
        ignored: the model has no rate control."""
        for piece in _stream(self._model, sentence):
            yield float32_to_pcm16(piece)


# -- blocking helpers ----------------------------------------------------------


class SpeechTokenizerSkipped(RuntimeError):
    """What mlx-audio's load gets instead of the speech tokenizer."""


_patch_lock = threading.Lock()
"""Serializes :func:`_without_speech_tokenizer`, so overlapping loads cannot
leave the wrapper installed."""


def _not_the_tokenizer_note(record: logging.LogRecord) -> bool:
    """Drops mlx-audio's warning that the speech tokenizer did not load: it
    was refused on purpose, and the engine's own load line says so."""
    return "S3 speech tokenizer" not in record.getMessage()


@contextmanager
def _without_speech_tokenizer() -> Iterator[None]:
    """Refuse mlx-audio's download of :data:`S3_TOKENIZER_REPO` for the load.

    ``ChatterboxTurboTTS.post_load_hook``
    (``mlx_audio/tts/models/chatterbox_turbo/chatterbox_turbo.py``, line 303
    in 0.5.9) calls ``hf_hub_download`` for it with no revision and loads
    its 0.5 GB of weights; it offers no argument to skip it or to name a
    revision. The services load with ``HF_HUB_OFFLINE``, where that download
    fails anyway, but ``voiced install`` runs each engine's load once with
    the network, and would fetch it from ``main``, unpinned. Only cloning
    from a reference clip uses it, and the hook logs a failure and goes on,
    so for the load that one repository's download fails. Every other call
    passes through, and the function is restored when the load ends.
    """
    import huggingface_hub

    quiet = _not_the_tokenizer_note
    hook_log = logging.getLogger("mlx_audio.tts.models.chatterbox_turbo.chatterbox_turbo")
    with _patch_lock:
        original = huggingface_hub.hf_hub_download

        def download(repo_id: str, *args: Any, **kwargs: Any) -> Any:
            if repo_id == S3_TOKENIZER_REPO:
                raise SpeechTokenizerSkipped(
                    "not loaded: voiced uses the bundled voice, which does not need it"
                )
            return original(repo_id, *args, **kwargs)

        huggingface_hub.hf_hub_download = download
        hook_log.addFilter(quiet)
        try:
            yield
        finally:
            hook_log.removeFilter(quiet)
            huggingface_hub.hf_hub_download = original


def _stream(model: Any, sentence: str) -> Iterator[NDArray[np.float32]]:
    """One sentence as 24 kHz float32 pieces (:func:`speak`) in the default voice."""
    import mlx.core as mx
    from mlx_audio.tts.models.chatterbox_turbo.chatterbox_turbo import punc_norm
    from mlx_audio.tts.models.chatterbox_turbo.models.s3gen.s3gen import (
        S3GEN_SIL,
        SPEECH_VOCAB_SIZE,
    )

    limit = max_tokens(sentence)
    ids = mx.array(model.tokenizer(punc_norm(sentence), return_tensors="np").input_ids)
    stream = model.t3.inference_turbo_stream(
        t3_cond=model._conds.t3,
        text_tokens=ids,
        temperature=TEMPERATURE,
        top_k=TOP_K,
        top_p=TOP_P,
        repetition_penalty=REPETITION_PENALTY,
        max_gen_len=limit,
        chunk_size=1,
    )

    def steps() -> Iterator[list[int]]:
        emitted = 0
        # With chunk_size=1 T3 yields each token as it is sampled and ends at
        # the stop token or after max_gen_len; its "final" flag carries
        # nothing then, so the end of the stream is the end of the sentence.
        for chunk, _ in stream:
            emitted += int(chunk.shape[-1])
            # Tokens past the speech vocabulary are T3's control tokens.
            yield [t for t in np.array(chunk).reshape(-1).tolist() if t < SPEECH_VOCAB_SIZE]
        # T3 emits a first token and then at most max_gen_len more, so only
        # a cut sentence passes the bound.
        if emitted > limit:
            log.warning(
                "chatterbox: runaway generation for %r: cut at the %d-token bound",
                sentence[:60],
                limit,
            )

    def render(tokens: list[int], start: int, final: bool) -> tuple[NDArray[np.float32], int]:
        audio, total = model.s3gen.inference_stream(
            speech_tokens=mx.array([tokens], dtype=mx.int32),
            ref_dict=model._conds.gen,
            n_cfm_timesteps=CFM_STEPS,
            prev_audio_samples=start,
            is_final=final,
        )
        return np.array(audio, dtype=np.float32).reshape(-1), int(total)

    yield from speak(steps(), render, gpu.Yielder(), int(S3GEN_SIL))
