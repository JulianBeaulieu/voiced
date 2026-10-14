# 0010: Soprano-80M ships selectable, not default, with runaway guards

Date: 2026-10-14. Status: accepted.

## Context

Soprano-80M had the nicest voice in the model benchmarks. It is
autoregressive: a Qwen3-based language model emits one token per 64 ms of
audio until it produces a stop token, and a Vocos-based decoder renders the
hidden states as 32 kHz audio. mlx-audio's port samples without the
repetition penalty upstream Soprano uses, and without it the model often
never stops and babbles to the token cap: tens of seconds of audio for a
one-sentence prompt. Kokoro (ADR 0009) renders a sentence in one forward pass
and cannot run away, which is why it stays the default and Soprano is opt-in.

## License

`SopranoEngine.license` is `Apache-2.0`. Checked on the Hugging Face model
cards on 2026-10-14:

- `mlx-community/Soprano-80M-bf16`, the MLX conversion the engine loads,
  declares `license: apache-2.0` and says it was converted from
  `ekwek/Soprano-80M`.
- `ekwek/Soprano-80M`, the source model, declares `license: apache-2.0`, and
  its README's License section reads "This project is licensed under the
  Apache-2.0 license."

The weights are not vendored; `load()` downloads them into the Hugging Face
cache on first run, as with Kokoro.

## Decision

`SopranoEngine` (entry point `soprano`) is a `SentenceTTSEngine`
(`voiced.engines.sentence`), so loading through `run_blocking`, the loaded,
voice and speed checks before the first yield, and one `PCMChunk` per sentence
are the same code as Kokoro's. It has one voice, `default`.

Text arrives normalized by the server and the engine only splits it into
sentences. mlx-audio's `generate()` then runs its own `clean_text`
(lowercasing, ASCII folding) and its own sentence split on each sentence it
receives; the engine does not add a third pass.

Soprano has no rate control. `speed` is checked against the same bounds as
Kokoro (0.25 to 4.0) and otherwise ignored, as `TTSEngine.synthesize` allows
for engines that cannot change speed. A client asking for 1.5 gets normal
speed.

### Runaway guards

1. **Repetition penalty.** The engine replaces the model's `stream_generate`
   with `penalized_stream`, which applies the repetition penalty upstream
   Soprano uses: logits of the last 64 tokens (prompt and generated) are
   divided by 1.2 when positive and multiplied by 1.2 when negative, before
   sampling. The loop is otherwise mlx-audio's, so the rest of `generate()`
   (text cleaning, decoder trimming) is reused unchanged. The penalty and the
   loop are written against an array module (`mlx.core` in service, `numpy`
   on CI), so CI runs them against a fake model whose logits favour repeating
   one token. Those tests fail without the penalty and fail if the 64-token
   window is not applied.
2. **One bound per sentence, in two units.** `sentence_bound(sentence)` is the
   longest plausible reading: 0.12 s per character (normal speech is about
   0.07 s) plus 1 s. In tokens (64 ms each, rounded up) it is the generation
   cap, so a runaway stops early. In seconds it is the audio limit: rounding
   up to whole tokens, and mlx-audio's sub-sentence split with a cap per
   part, can leave the audio slightly longer, so it is cut to the bound with
   a 10 ms linear fade, which keeps the cut from clicking. A sentence that
   hits the cap or the cut logs one warning (`soprano: runaway generation
   ...`); count runaways from the log. mlx-audio counts one prefill step per
   sub-prompt in `token_count`, so a sentence that stopped on its own reports
   at most the cap unless mlx-audio split it; the log is a signal, the cut is
   the guarantee.
3. **The model's context.** `penalized_stream` limits each sub-prompt's cap to
   the 512-token context minus that prompt's length, so a runaway on a long
   sentence cannot overrun the context. For sentences over about 200
   characters this limit binds before the token cap, so a runaway it stops
   logs its own warning (`... stopped by the context`). A prompt that already
   fills the context (mlx-audio's `clean_text` expands digits, so 300
   characters of numbers can become 2000 tokens) raises `EngineError`, a
   client error, rather than producing a fragment of audio.

The 1 s margin is deliberate. One-word replies from this model ("Hi.",
"Thanks.") that stop on their own last about 1 to 1.8 s, with breath and
trailing silence. With 1 s, "Hi." may run 1.36 s, which keeps most of them
whole; 0.5 s (a 0.86 s bound for "Hi.") was tried and would cut most of them
mid-word. The cost is that one-word replies that run away carry up to about
1 s of sustained sound before the cut.

The constants (penalty, window, sampling temperature and top-p, the bound)
are module constants, not constructor options; nothing passes engine options
through the registry yet.

### mlx-audio coupling

The engine reads private parts of mlx-audio's Soprano model
(`_forward_with_hidden_states`, `_stop_token_id`,
`config.num_hidden_layers`, `config.max_position_embeddings`,
`config.decoder_config.token_size`) and relies on `generate()` calling
`self.stream_generate` and counting the prefill step. mlx-audio is pinned to
`>=0.5.8,<0.5.10`, the releases this was checked against; a model test
asserts the penalized loop is installed.

## Resampling (outside engine ownership)

Soprano produces 32 kHz and the TTS contract is 24 kHz. The conversion is
`resample(samples, src_rate, dst_rate)` in `voiced.audio`, not in the engine:
it is the second resample adapter next to `PCMConverter` (STT input), so it
belongs in the shared PCM toolbox. It uses `scipy.signal.resample_poly` at the
reduced ratio 3/4. scipy is an mlx-audio dependency and is declared in the
`tts` extra; the identity and validation paths return before importing it, so
the module imports without the extra. Each sentence is resampled on its own,
and chunk durations add up within one sample per chunk.

## Measurements

M1 Max (64 GB, macOS 15.7.4), one engine per process, warm:

- First chunk: median 156 to 223 ms over three runs of 7 warm requests
  (single requests 129 to 262 ms), against Kokoro's 106 ms and the 200 ms
  target. Soprano has to generate a sentence's tokens before decoding it.
- Resident set size with the model loaded: 392 MB.
- How often the bound fires:
  - 0 of 20 on a corpus of ordinary sentences: 20 everyday statements and
    requests of 45 to 70 characters, the kind of line an assistant speaks,
    written for this measurement with no digits or acronyms so normalization
    is not in play.
  - 6 of 40 on very short replies: 10 one- and two-word prompts ("Hi.",
    "Okay.", "Got it.") with 4 sampling seeds each. Sampling is not fully
    reproducible across runs; another run gave 10 of 40. With the bound
    raised to 3 s, 3 of 40 still ran past it, so these are real runaways,
    not natural speech being clipped.

## Consequences

- A runaway still costs up to a second or two of audio and GPU time per
  sentence, ends in a faded cut that can clip a word, and is most likely on
  one- and two-word sentences. That is the price of offering the voice; users
  who want determinism use Kokoro.
- `speed` has no effect with Soprano. The README says so.
- An mlx-audio upgrade past the pin needs the model tests run before the pin
  moves.
