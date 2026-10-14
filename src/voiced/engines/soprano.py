"""Soprano-80M on MLX. Selectable, not the default.

Soprano is autoregressive: a small Qwen3 language model emits one token per
64 ms of audio until it produces a stop token, and a Vocos decoder renders the
hidden states as 32 kHz audio. mlx-audio's port samples without the
repetition penalty upstream Soprano uses, and without it the model often never
stops. This engine (see ``docs/decisions/0010-tts-soprano.md``):

* applies the repetition penalty upstream Soprano uses, 1.2 over the last 64
  tokens, in the model's token loop;
* bounds each sentence by its length: a token cap for generation and the same
  bound in seconds for the audio, with a short fade where it cuts; and
* resamples the 32 kHz output to the project's 24 kHz.

The engine splits sentences and never normalizes text itself; mlx-audio's
``generate()`` still runs its own ``clean_text`` (lowercasing, ASCII folding)
and sentence split on what it receives. Soprano has no rate control, so
``speed`` is checked against the shared bounds and otherwise ignored, as
``TTSEngine.synthesize`` allows.

Everything that touches MLX is imported inside ``load()``, so this module
imports (the entry-point registry does so) without the ``tts`` extra.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from voiced.audio import float32_to_pcm16, resample
from voiced.engines.base import TTS_SAMPLE_RATE, EngineError, Voice
from voiced.engines.sentence import WARMUP_TEXT, SentenceTTSEngine

log = logging.getLogger(__name__)

__all__ = ["SOPRANO_VOICE", "SopranoEngine"]

SOPRANO_VOICE = Voice(id="default", name="Soprano", language="en-US", gender=None)

REPETITION_PENALTY = 1.2
REPETITION_CONTEXT = 64
TEMPERATURE = 0.3
TOP_P = 0.95

SECONDS_PER_CHAR = 0.12
"""Upper bound on speech per character of text. Normal English runs about
0.07 s per character; this leaves room for slow delivery."""
EXTRA_SECONDS = 1.0
"""Added to the bound for very short sentences. One-word replies that stop on
their own run about 1 to 1.8 s, so 1 s keeps most of them whole; one-word
replies that run away carry up to about 1 s of sustained sound before the
cut."""
FADE_SECONDS = 0.01
"""Linear fade-out applied where runaway audio is cut, so the cut does not click."""


@dataclass(frozen=True, slots=True)
class SentenceBound:
    """How long one sentence may run: ``tokens`` for generation, ``seconds``
    for the audio. They are the same bound in two units; see
    :func:`sentence_bound`."""

    tokens: int
    seconds: float


def sentence_bound(sentence: str, token_seconds: float) -> SentenceBound:
    """The longest plausible reading of ``sentence``.

    ``seconds`` is ``SECONDS_PER_CHAR`` per character plus ``EXTRA_SECONDS``.
    ``tokens`` covers ``seconds`` at ``token_seconds`` per token and is the
    generation cap, so a runaway stops early; :func:`penalized_stream` also
    keeps it inside the model's context. The audio is still cut to ``seconds``,
    because rounding up to whole tokens (and mlx-audio's own sub-sentence
    split, each part with its own cap) can leave it slightly longer.
    """
    seconds = len(sentence) * SECONDS_PER_CHAR + EXTRA_SECONDS
    return SentenceBound(tokens=max(1, math.ceil(seconds / token_seconds)), seconds=seconds)


class SopranoEngine(SentenceTTSEngine):
    name = "soprano"
    model_id = "mlx-community/Soprano-80M-bf16"
    languages = ["en-US"]
    license = "Apache-2.0"

    def _load_sync(self) -> None:
        """Build in locals and assign ``_model`` last, so a failure part-way
        leaves the engine unloaded."""
        started = time.perf_counter()
        model = _load_model(self.model_id)
        _synth(model, WARMUP_TEXT)
        self._model = model
        log.info(
            "soprano: loaded %s (%d Hz) in %.1fs",
            self.model_id,
            int(model.sample_rate),
            time.perf_counter() - started,
        )

    async def voices(self) -> list[Voice]:
        self._check_loaded()
        return [SOPRANO_VOICE]

    def _has_voice(self, voice: str) -> bool:
        return voice == SOPRANO_VOICE.id

    def _synth_sentence(self, sentence: str, voice: str, speed: float) -> bytes:
        """Blocking; runs in a worker thread. ``speed`` is ignored: the model
        has no rate control."""
        return _synth(self._model, sentence)


# -- blocking helpers ----------------------------------------------------------


def _load_model(model_id: str) -> Any:
    """mlx-audio's Soprano model with the repetition penalty in its token loop."""
    import mlx.core as mx
    from mlx_audio.lm.models.cache import KVCache
    from mlx_audio.lm.sample_utils import make_sampler
    from mlx_audio.tts.utils import load_model

    model = load_model(model_id)

    def stream_generate(
        input_ids: Any,
        max_tokens: int = 512,
        temperature: float = TEMPERATURE,
        top_p: float = TOP_P,
        **_kwargs: Any,
    ) -> Iterator[tuple[Any, Any]]:
        return penalized_stream(
            model,
            input_ids,
            max_tokens,
            sample=make_sampler(temperature, top_p) if temperature else None,
            new_cache=KVCache,
            xp=mx,
            evaluate=mx.eval,
        )

    model.stream_generate = stream_generate
    return model


def repetition_penalty(history: Any, logits: Any, xp: Any) -> Any:
    """Divide positive (multiply negative) logits of the last
    ``REPETITION_CONTEXT`` tokens in ``history`` by ``REPETITION_PENALTY``.

    ``history`` is the 1-D token sequence (prompt and generated so far) and
    ``logits`` is ``(1, vocab)``. Same rule as mlx-audio's
    ``make_repetition_penalty``, written against ``xp`` (``mlx.core`` or
    ``numpy``) so it runs on CI.
    """
    window = history[-REPETITION_CONTEXT:]
    if len(window) == 0:
        return logits
    selected = logits[:, window]
    logits[:, window] = xp.where(
        selected < 0, selected * REPETITION_PENALTY, selected / REPETITION_PENALTY
    )
    return logits


def penalized_stream(
    model: Any,
    input_ids: Any,
    max_tokens: int,
    *,
    sample: Callable[[Any], Any] | None,
    new_cache: Callable[[], Any],
    xp: Any,
    evaluate: Callable[..., None],
) -> Iterator[tuple[Any, Any]]:
    """mlx-audio's ``Model.stream_generate`` with :func:`repetition_penalty`
    applied before each sample. ``sample`` is ``None`` for greedy decoding.

    Same contract as upstream: the first yield is ``(None, hidden)`` from the
    prefill, then one ``(token, hidden)`` per generated token, at most
    ``max_tokens``, stopping early on the stop or end-of-sequence token.
    ``max_tokens`` is also limited to the context left after the prompt, so a
    runaway cannot overrun it; a prompt that fills the context is a client
    error.
    """
    if input_ids.ndim == 1:
        input_ids = input_ids[None, :]
    room = int(model.config.max_position_embeddings) - int(input_ids.shape[-1])
    if room <= 0:
        raise EngineError("sentence too long for Soprano: the prompt fills the model's context")
    limit = min(max_tokens, room)
    cache = [new_cache() for _ in range(model.config.num_hidden_layers)]
    logits, hidden = model._forward_with_hidden_states(input_ids, cache)
    evaluate(logits, hidden)
    yield None, hidden[:, -1:, :]
    history = input_ids[0]
    eos = model.tokenizer.eos_token_id if model.tokenizer is not None else None
    for _ in range(limit):
        next_logits = repetition_penalty(history, logits[:, -1, :], xp)
        if sample is None:
            token = xp.argmax(next_logits, axis=-1, keepdims=True)
        else:
            token = sample(next_logits)
            if token.ndim == 1:
                token = token[:, None]
        token_id = int(token[0, 0])
        if token_id == model._stop_token_id or token_id == eos:
            break
        history = xp.concatenate([history, token[0]])
        logits, hidden = model._forward_with_hidden_states(token, cache)
        evaluate(logits, hidden)
        yield token, hidden[:, -1:, :]
    else:
        if room < max_tokens:
            # The context, not the sentence's token cap, stopped a runaway; the
            # audio stays under the time bound, so _synth would not log it.
            log.warning(
                "soprano: runaway generation stopped by the context: %d tokens after a "
                "%d-token prompt (cap %d)",
                room,
                int(input_ids.shape[-1]),
                max_tokens,
            )


def _synth(model: Any, sentence: str) -> bytes:
    """One sentence to 24 kHz int16 PCM, held to :func:`sentence_bound`."""
    rate = int(model.sample_rate)
    token_seconds = int(model.config.decoder_config.token_size) / rate
    bound = sentence_bound(sentence, token_seconds)
    parts: list[NDArray[np.float32]] = []
    tokens = 0
    for result in model.generate(
        text=sentence, max_tokens=bound.tokens, temperature=TEMPERATURE, top_p=TOP_P
    ):
        parts.append(np.asarray(result.audio, dtype=np.float32).reshape(-1))
        tokens += int(result.token_count)
    if not parts:
        return b""
    audio = np.concatenate(parts)
    limit = int(bound.seconds * rate)
    # token_count includes the prefill step, so a sentence that stops on its
    # own reports at most ``bound.tokens``. mlx-audio adds one prefill step per
    # sub-prompt it splits a sentence into, so a split sentence can be logged
    # without hitting the cap; the log is a signal, the cut is the guarantee.
    if tokens > bound.tokens or len(audio) > limit:
        log.warning(
            "soprano: runaway generation for %r: %d tokens (cap %d), %.2fs of audio, kept %.2fs",
            sentence[:60],
            tokens,
            bound.tokens,
            len(audio) / rate,
            min(len(audio), limit) / rate,
        )
    if len(audio) > limit:
        audio = _fade_out(audio[:limit], int(FADE_SECONDS * rate))
    return float32_to_pcm16(resample(audio, rate, TTS_SAMPLE_RATE))


def _fade_out(audio: NDArray[np.float32], samples: int) -> NDArray[np.float32]:
    """Ramp the last ``samples`` of ``audio`` linearly to zero."""
    n = min(samples, len(audio))
    if n > 0:
        audio = audio.copy()
        audio[-n:] *= np.linspace(1.0, 0.0, n, dtype=np.float32)
    return audio
