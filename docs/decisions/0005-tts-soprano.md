# 0005: Soprano-80M ships selectable, not default, with runaway guards

Date: 2026-10-14. Status: accepted.

## Context

Soprano-80M (`ekwek/Soprano-80M`, MLX conversion `mlx-community/Soprano-80M-bf16`)
had the nicest voice in the benchmarks. It is autoregressive: a Qwen3-based
language model emits one token per 64 ms of audio until it generates
`[STOP]`, and a Vocos decoder renders the hidden states. The mlx-audio port
omits the repetition penalty that the upstream implementation applies, and
without it the model frequently never emits `[STOP]`: in the benchmark 5 of
6 sampled runs and 8 of 8 greedy runs babbled to the 512-token cap, about
33 seconds of audio for a one-sentence prompt. Kokoro is not autoregressive
and cannot run away, which is why it is the default.

The model also lowercases and strips non-ASCII text internally ("CO2"
becomes "cotwo"), so it depends on `voiced.tts.normalize` even more than
Kokoro does.

## License

Verified 2026-10-14 on the Hugging Face model cards: both `ekwek/Soprano-80M`
and `mlx-community/Soprano-80M-bf16` declare `license: apache-2.0`, and the
original README has a "License" section stating Apache-2.0. `SopranoEngine.license`
is `Apache-2.0`.

## Decision

`SopranoEngine` is registered as `soprano` and is never selected unless the
config says `engine = "soprano"`. Three guards, in order of importance:

1. **Repetition penalty.** `patch_repetition_penalty` replaces the model's
   `stream_generate` with a copy that runs mlx-audio's own
   `make_logits_processors(repetition_penalty=1.2, repetition_context_size=64)`
   over the logits before sampling. This is a port of the prototype's
   `soprano_fix.py`. It brought runaways down to about 1 in 10 in the
   benchmark. Monkeypatching was chosen over subclassing because the rest of
   `generate()` (text cleaning, decoder trimming) is reused unchanged.
2. **Token cap from text length.** The longest plausible duration of a
   sentence is `0.12 s per character + 1 s` (normal speech is about 0.07 s
   per character), and `max_tokens` is that duration divided by the model's
   64 ms per token. The cap bounds the cost of a runaway to the length of a
   slow reading.
3. **Duration check.** Audio longer than the same bound is truncated and a
   warning is logged with the sentence. With the cap in place this triggers
   only when generation hit the cap, so the log line doubles as a runaway
   counter.

The constants are engine attributes (`seconds_per_char`, `repetition_penalty`,
`temperature`, `top_p`) so a deployment can tune them without a code change
once the registry passes engine options through.

The model's 32 kHz output is resampled to 24 kHz with a polyphase filter
(`scipy.signal.resample_poly`). `speed` is accepted and ignored; the model
has no rate control, and time-stretching audio is out of scope.

## Consequences

- A runaway still costs up to a few seconds of audio and GPU time per
  sentence, and the truncated tail can clip a word. That is the price of
  offering the voice at all; users who want determinism use Kokoro.
- Soprano's load is slower than Kokoro's (the first call compiles the
  decoder graph) so `load()` warms up with a short sentence like Kokoro.
- The repetition-penalty patch depends on three private attributes of
  mlx-audio's Soprano model (`_forward_with_hidden_states`, `_stop_token_id`,
  `config.num_hidden_layers`). The model test suite covers it; an mlx-audio
  upgrade that breaks it will show up there, and the engine can fall back to
  the token cap and duration check alone.
