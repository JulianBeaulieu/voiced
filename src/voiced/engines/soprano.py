"""Soprano-80M on MLX. Selectable, not the default.

Soprano is an autoregressive model: a small Qwen3 language model emits one
token per 64 ms of audio until it produces ``[STOP]``, and a Vocos decoder
turns the hidden states into a waveform. The mlx-audio port omits the
repetition penalty the upstream implementation applies, and without it the
model frequently never stops and babbles to the token cap. This engine
(see ``docs/decisions/0005-tts-soprano.md``):

* installs the repetition penalty into the model's token loop,
* caps the number of tokens from the text length, and
* truncates audio that still runs past the duration the text can justify.

The model produces 32 kHz audio; it is resampled to the project's 24 kHz.
``speed`` is ignored: the model has no control for it.

MLX imports happen inside ``load()`` so this module imports anywhere.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

from voiced.engines.audio import to_pcm16
from voiced.engines.base import (
    TTS_SAMPLE_RATE,
    EngineError,
    EngineNotLoaded,
    PCMChunk,
    TTSEngine,
    UnknownVoice,
    Voice,
)
from voiced.tts.normalize import normalize
from voiced.tts.sentences import split_sentences

log = logging.getLogger(__name__)

__all__ = ["SOPRANO_VOICE", "SopranoEngine", "max_seconds_for", "patch_repetition_penalty"]

SOPRANO_VOICE = Voice(id="default", name="Soprano", language="en-US", gender=None)
WARMUP_TEXT = "Warming up."

SECONDS_PER_CHAR = 0.12
"""Upper bound on how long speech for one character of text may last. Normal
English runs about 0.07 s per character; this leaves room for slow, careful
delivery without letting a runaway generation go on for long."""
EXTRA_SECONDS = 1.0
"""Added to the bound so very short sentences ("Hi.") are not clipped."""


def max_seconds_for(text: str, seconds_per_char: float = SECONDS_PER_CHAR) -> float:
    """Longest plausible duration of ``text`` spoken aloud."""
    return len(text) * seconds_per_char + EXTRA_SECONDS


def patch_repetition_penalty(model: Any, penalty: float = 1.2, context: int = 64) -> Any:
    """Replace ``model.stream_generate`` with a copy that applies mlx-audio's
    repetition penalty processor (the upstream Soprano default) to the logits
    before sampling. Returns ``model``."""
    import mlx.core as mx
    from mlx_audio.lm.models.cache import KVCache
    from mlx_audio.lm.sample_utils import make_logits_processors, make_sampler

    def stream_generate(
        input_ids: Any,
        max_tokens: int = 512,
        temperature: float = 0.3,
        top_p: float = 0.95,
        **_kwargs: Any,
    ) -> Iterator[tuple[Any, Any]]:
        if input_ids.ndim == 1:
            input_ids = input_ids[None, :]
        cache = [KVCache() for _ in range(model.config.num_hidden_layers)]
        logits, hidden = model._forward_with_hidden_states(input_ids, cache)
        mx.eval(logits, hidden)
        yield None, hidden[:, -1:, :]
        sampler = make_sampler(temperature, top_p)
        processors = make_logits_processors(
            repetition_penalty=penalty, repetition_context_size=context
        )
        history = input_ids
        for _ in range(max_tokens):
            next_logits = logits[:, -1, :]
            for processor in processors:
                next_logits = processor(history, next_logits)
            if temperature == 0:
                token = mx.argmax(next_logits, axis=-1, keepdims=True)
            else:
                token = sampler(next_logits)
                if token.ndim == 1:
                    token = token[:, None]
            token_id = int(token[0, 0])
            if token_id == model._stop_token_id:
                break
            if model.tokenizer is not None and token_id == model.tokenizer.eos_token_id:
                break
            history = mx.concatenate([history, token], axis=1)
            logits, hidden = model._forward_with_hidden_states(token, cache)
            mx.eval(logits, hidden)
            yield token, hidden[:, -1:, :]

    model.stream_generate = stream_generate
    return model


class SopranoEngine(TTSEngine):
    name = "soprano"
    model_id = "mlx-community/Soprano-80M-bf16"
    languages = ["en-US"]
    license = "Apache-2.0"

    def __init__(
        self,
        model_id: str | None = None,
        *,
        repetition_penalty: float = 1.2,
        temperature: float = 0.3,
        top_p: float = 0.95,
        seconds_per_char: float = SECONDS_PER_CHAR,
        warmup: bool = True,
    ) -> None:
        super().__init__(model_id)
        self.repetition_penalty = repetition_penalty
        self.temperature = temperature
        self.top_p = top_p
        self.seconds_per_char = seconds_per_char
        self._warmup = warmup
        self._model: Any = None
        self._sample_rate = 0
        self._token_seconds = 0.0

    async def load(self) -> None:
        await asyncio.to_thread(self._load_sync)

    def _load_sync(self) -> None:
        started = time.perf_counter()
        from mlx_audio.tts.utils import load_model

        model = load_model(self.model_id)
        self._sample_rate = int(model.sample_rate)
        token_size = int(model.config.decoder_config.token_size)
        self._token_seconds = token_size / self._sample_rate
        self._model = patch_repetition_penalty(model, self.repetition_penalty)
        if self._warmup:
            self._synth_sentence(WARMUP_TEXT)
        log.info(
            "soprano: loaded %s (%d Hz, %.0f ms per token) in %.1fs",
            self.model_id,
            self._sample_rate,
            self._token_seconds * 1000,
            time.perf_counter() - started,
        )

    async def voices(self) -> list[Voice]:
        self._check_loaded()
        return [SOPRANO_VOICE]

    async def synthesize(
        self, text: str, voice: str, speed: float = 1.0
    ) -> AsyncIterator[PCMChunk]:
        self._check_loaded()
        if voice != SOPRANO_VOICE.id:
            raise UnknownVoice(voice)
        if not speed > 0:
            raise EngineError("speed must be positive")
        for sentence in split_sentences(normalize(text)):
            pcm = await asyncio.to_thread(self._synth_sentence, sentence)
            if pcm:
                yield PCMChunk(data=pcm, sample_rate=TTS_SAMPLE_RATE)

    def token_cap(self, text: str) -> int:
        """Tokens that cover :func:`max_seconds_for` ``text``."""
        return max(1, math.ceil(max_seconds_for(text, self.seconds_per_char) / self._token_seconds))

    def _synth_sentence(self, sentence: str) -> bytes:
        import numpy as np

        limit_s = max_seconds_for(sentence, self.seconds_per_char)
        cap = self.token_cap(sentence)
        parts: list[Any] = []
        tokens = 0
        for result in self._model.generate(
            text=sentence,
            max_tokens=cap,
            temperature=self.temperature,
            top_p=self.top_p,
        ):
            parts.append(np.array(result.audio, copy=False).reshape(-1))
            tokens += int(result.token_count)
        if not parts:
            return b""
        audio = np.concatenate(parts) if len(parts) > 1 else parts[0]
        limit_samples = int(limit_s * self._sample_rate)
        if len(audio) > limit_samples:
            log.warning(
                "soprano: runaway generation (%d tokens, %.1fs) truncated to %.1fs for %r",
                tokens,
                len(audio) / self._sample_rate,
                limit_s,
                sentence[:60],
            )
            audio = audio[:limit_samples]
        return to_pcm16(audio, self._sample_rate)

    def _check_loaded(self) -> None:
        if self._model is None:
            raise EngineNotLoaded(self.name)
