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
  keeps ahead of playback (:func:`speak`);
* waits for the STT service's encoder calls before every GPU step, each
  token and each render (``voiced.gpu``, ADR 0019);
* bounds each sentence's tokens by its length, so a runaway stops;
* loads the model and the default voice from voiced's pinned snapshot
  (``voiced.models``, the catalogue), and not the speech tokenizer only voice cloning
  needs (:func:`_without_speech_tokenizer`).

There is one voice, ``default``: the conditionals the checkpoint ships
(``conds.safetensors``). Turbo has no rate control: ``speed`` is checked
against the shared bounds and otherwise ignored. mlx-audio's port applies no
watermark (upstream's PerTh one is not in it).

Everything that imports MLX runs inside ``load()``, so this module imports
(the entry-point registry does so) without the ``tts`` extra.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Generator, Iterator, Sequence
from contextlib import contextmanager
from typing import Any

import numpy as np
from numpy.typing import NDArray

from voiced import gpu
from voiced.audio import float32_to_pcm16
from voiced.engines.base import TTS_SAMPLE_RATE, Voice
from voiced.engines.sentence import WARMUP_TEXT, SentenceTTSEngine
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
re-renders the sentence so far and costs about 250 ms however short, so
fewer tokens barely move the first audio and leave too little to play while
the next render runs (ADR 0029)."""
CHUNK_TOKENS = 25
"""Speech tokens added before each later render: one second of audio for
about half a second of work."""
SILENCE_TOKEN = 4299
"""S3Gen's silence token (mlx-audio's ``S3GEN_SIL``)."""
SILENCE_TOKENS = 5
"""Silence tokens appended for the final render, as mlx-audio does."""
SECONDS_PER_CHAR = 0.12
EXTRA_SECONDS = 1.0
"""A sentence's token bound, as Soprano's (ADR 0010): 0.12 s of speech per
character plus 1 s, far above a natural reading."""

TEMPERATURE = 0.8
TOP_K = 1000
TOP_P = 0.95
REPETITION_PENALTY = 1.2
CFM_STEPS = 2
"""mlx-audio's sampling and S3Gen settings for Turbo."""

Steps = Iterator[tuple[list[int], bool]]
"""Speech tokens as T3 emits them, each with whether it was the last."""
Render = Callable[[list[int], int, bool], tuple[NDArray[np.float32], int]]
"""``(tokens, samples already yielded, final) -> (new samples, total)``."""


def max_tokens(sentence: str) -> int:
    seconds = len(sentence) * SECONDS_PER_CHAR + EXTRA_SECONDS
    return math.ceil(seconds * TOKENS_PER_SECOND)


def speak(steps: Steps, render: Render, wait: Callable[[], None]) -> Iterator[NDArray[np.float32]]:
    """One sentence's audio pieces: renders the tokens from ``steps`` after
    :data:`FIRST_TOKENS`, then every :data:`CHUNK_TOKENS` more, and once at
    the end with :data:`SILENCE_TOKENS` appended. ``wait`` runs before every
    step and every render, each a GPU call."""
    tokens: list[int] = []
    done = 0
    render_at = FIRST_TOKENS
    final = False
    while not final:
        wait()
        step = next(steps, None)
        if step is None:
            final = True
        else:
            new, final = step
            tokens.extend(new)
        if not final and len(tokens) < render_at:
            continue
        if final:
            if not tokens:
                return
            tokens.extend([SILENCE_TOKEN] * SILENCE_TOKENS)
        wait()
        piece, done = render(tokens, done, final)
        render_at = len(tokens) + CHUNK_TOKENS
        if piece.size:
            yield piece


class ChatterboxEngine(SentenceTTSEngine):
    name = "chatterbox"
    model_id = default_model("chatterbox").id
    languages = ["en"]
    license = default_model("chatterbox").license

    @classmethod
    def catalogue_voices(cls) -> list[Voice]:
        return [DEFAULT_VOICE]

    def _load_sync(self) -> None:
        """Build in locals and assign ``_model`` last, so a failure part-way
        leaves the engine unloaded."""
        from mlx_audio.tts.utils import load_model

        from voiced.models import snapshot

        started = time.perf_counter()
        root = snapshot(self.model_id, check_only=True)
        with _without_speech_tokenizer():
            model = load_model(root)
        model._s3tokenizer = None  # its unloaded weights: 0.5 GB
        rate = int(model.sample_rate)
        if rate != TTS_SAMPLE_RATE:
            raise RuntimeError(f"Chatterbox model reports {rate} Hz, expected {TTS_SAMPLE_RATE}")
        if model._conds is None:
            raise RuntimeError(f"{self.model_id} has no default voice (conds.safetensors)")
        for _ in _stream(model, WARMUP_TEXT):
            pass
        self._model = model
        log.info("chatterbox: loaded %s in %.1fs", self.model_id, time.perf_counter() - started)

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
        """Blocking; each step runs in a worker thread. ``speed`` is ignored:
        the model has no rate control."""
        for piece in _stream(self._model, sentence):
            yield float32_to_pcm16(piece)


# -- blocking helpers ----------------------------------------------------------


class SpeechTokenizerSkipped(RuntimeError):
    """What mlx-audio's load gets instead of the speech tokenizer."""


@contextmanager
def _without_speech_tokenizer() -> Iterator[None]:
    """Refuse mlx-audio's download of :data:`S3_TOKENIZER_REPO` for the load.

    ``ChatterboxTurboTTS.post_load_hook``
    (``mlx_audio/tts/models/chatterbox_turbo/chatterbox_turbo.py``, line 302
    in 0.5.9) calls ``hf_hub_download`` for it with no revision, which
    resolves ``main`` on the Hub, and loads its 0.5 GB of weights; it offers
    no argument to skip it or to name a revision. Only cloning from a
    reference clip uses it, and the hook logs a failure and goes on, so for
    the load that one repository's download fails. Every other call passes
    through, and the function is restored when the load ends.
    """
    import huggingface_hub

    original = huggingface_hub.hf_hub_download

    def download(repo_id: str, *args: Any, **kwargs: Any) -> Any:
        if repo_id == S3_TOKENIZER_REPO:
            raise SpeechTokenizerSkipped(
                "not loaded: voiced uses the bundled voice, which does not need it"
            )
        return original(repo_id, *args, **kwargs)

    huggingface_hub.hf_hub_download = download
    try:
        yield
    finally:
        huggingface_hub.hf_hub_download = original


def _stream(model: Any, sentence: str) -> Iterator[NDArray[np.float32]]:
    """One sentence as 24 kHz float32 pieces (:func:`speak`) in the default voice."""
    import mlx.core as mx
    from mlx_audio.tts.models.chatterbox_turbo.chatterbox_turbo import punc_norm
    from mlx_audio.tts.models.chatterbox_turbo.models.s3gen.s3gen import SPEECH_VOCAB_SIZE

    text = punc_norm(sentence)
    ids = mx.array(model.tokenizer(text, return_tensors="np").input_ids)
    stream = model.t3.inference_turbo_stream(
        t3_cond=model._conds.t3,
        text_tokens=ids,
        temperature=TEMPERATURE,
        top_k=TOP_K,
        top_p=TOP_P,
        repetition_penalty=REPETITION_PENALTY,
        max_gen_len=max_tokens(sentence),
        chunk_size=1,
    )

    def steps() -> Steps:
        for chunk, final in stream:
            # Tokens past the speech vocabulary are T3's control tokens.
            yield [t for t in np.array(chunk).reshape(-1).tolist() if t < SPEECH_VOCAB_SIZE], final

    def render(tokens: Sequence[int], done: int, final: bool) -> tuple[NDArray[np.float32], int]:
        audio, total = model.s3gen.inference_stream(
            speech_tokens=mx.array([list(tokens)], dtype=mx.int32),
            ref_dict=model._conds.gen,
            n_cfm_timesteps=CFM_STEPS,
            prev_audio_samples=done,
            is_final=final,
        )
        return np.array(audio, dtype=np.float32).reshape(-1), int(total)

    yield from speak(steps(), render, gpu.Yielder())
