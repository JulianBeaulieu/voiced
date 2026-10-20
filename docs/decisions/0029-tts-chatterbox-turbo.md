# 0029: Chatterbox Turbo ships selectable, not default, on the GPU

Date: 2026-10-20. Status: accepted.

## Context

Chatterbox Turbo is Resemble AI's 350M English text-to-speech model. A GPT-2
medium language model (T3) turns text into speech tokens, 25 per second of
audio, conditioned on a voice; S3Gen, a two-step flow-matching decoder and a
HiFiGAN vocoder, renders the tokens as 24 kHz audio in that voice. The voice
is a set of precomputed conditionals (`conds.safetensors`, 165 KB) that the
checkpoint ships, or ones computed from a reference clip. Text may carry
tags such as `[laugh]`, `[sigh]` or `[whispering]`, each a token of its own,
which the model performs rather than reads. In the TTS Arena the original
Chatterbox sits just above Kokoro (Elo 1476 against 1473); there is no
independent figure for Turbo.

mlx-audio, already in the `tts` extra at `>=0.5.8,<0.5.10`, has a
`chatterbox_turbo` port (identical in 0.5.8 and 0.5.9) and mlx-community
publishes conversions at 4, 5, 6 and 8 bits and fp16. Checked on an Apple M1
Max (64 GB, macOS 15.7.4) with Python 3.12 before building anything:

- It runs on MLX on the GPU with what voiced already installs: no PyTorch,
  no new package (`transformers`, for the text tokenizer, comes with
  mlx-audio), no espeak-ng.
- mlx-audio's `stream_generate` streams within a sentence, but in fixed
  chunks of `chunk_size` tokens, and every chunk re-renders the sentence so
  far through S3Gen. A render costs 230 to 350 ms whatever its length (the
  voice prompt is part of every one), T3 takes about 8 ms a token, and the
  first token about 100 ms. At 25-token chunks first audio was 434 to 466 ms
  and a long reply ran at 0.64 times real time; at 10-token chunks first
  audio was 329 ms but the reply ran slower than real time (1.7), so a
  listener would hear gaps.
- fp16 was no faster than 4-bit (first audio 458 to 469 ms at 25-token
  chunks) and held 3.0 GB against 1.05 GB.
- Loading any conversion also downloads `mlx-community/S3TokenizerV2`
  (495 MB) from the Hub's `main` and keeps it in memory: the speech
  tokenizer that computes conditionals from a reference clip. The bundled
  voice does not need it.

## Licences

| What | Licence | Source |
|---|---|---|
| `ResembleAI/chatterbox-turbo` (weights, text tokenizer, default voice) | MIT | model card, `license: mit` |
| `resemble-ai/chatterbox`, the reference code | MIT | the repository's LICENSE |
| `mlx-community/chatterbox-turbo-4bit` and `-fp16` | MIT, Resemble AI's | their cards say `apache-2.0`, but a conversion cannot relicense the weights it converts |
| `mlx-audio`, the port voiced runs | MIT | already a dependency |

The mlx-community cards are mislabelled: the converted weights carry the
original's MIT licence, which is what voiced reports (`/v1/models`, the
README). Neither repository is gated. voiced redistributes none of it:
each Mac downloads the weights from Hugging Face.

Resemble's own package adds an inaudible PerTh watermark to everything it
synthesizes. mlx-audio's port does not: there is no `perth` import or
watermarking step anywhere in `mlx_audio/tts/models/chatterbox_turbo` at
0.5.9, and voiced adds none.

## Decision

`chatterbox` is a fifth TTS engine, `voiced.engines.chatterbox.ChatterboxEngine`,
a `SentenceTTSEngine`, selectable and never the default. It needs no extra:
everything it imports is in `tts`, and everything that imports MLX runs
inside `load()`.

- **Model.** `mlx-community/chatterbox-turbo-4bit` by default (0.42 GB on
  disk); `mlx-community/chatterbox-turbo-fp16` (3.0 GB) works as `model`.
  Both are in the model catalogue (`voiced.models.CATALOGUE`, ADR 0022)
  with the files the engine loads: the config, the weights, the text
  tokenizer's files and `conds.safetensors`, pinned in `models.lock.json`;
  `voiced models download` fetches them and the engine loads them with
  `check_only`, so the service never downloads. The 4-bit build is the
  default because it lost little against fp16 and costs a third of the
  memory. UTMOS, the benchmark's predicted naturalness, gives fp16 3.82 and
  4-bit 3.70 (docs/benchmarks.md). In
  a round trip of twelve household sentences through Parakeet, 4-bit had no
  word errors in 82 and fp16 one ("three fifteen" heard as "thirty
  fifteen"), neither build clicked where pieces join (the largest step
  between samples stayed within the 99.9th percentile of the clip's own),
  and both read a 70-word reply word for word. This is a check of
  intelligibility; with UTMOS's 0.12 it is all the comparison there is so
  far, and nobody has yet compared the two by ear.
- **Streaming, voiced's own loop.** The engine does not call
  `stream_generate`. It drives mlx-audio's T3 token stream
  (`t3.inference_turbo_stream`, one token at a time) and S3Gen's
  `inference_stream` itself, in `speak()`, a function of a token stream, a
  render and a wait, so its schedule is tested without MLX. It renders the
  first 15 tokens as soon as they exist, then every 25 more, then once more
  at the end with mlx-audio's five silence tokens appended; each render
  yields only the samples not yet sent. 15 tokens is the smallest first
  chunk that kept playback fed: at 10, first audio was about 40 ms earlier
  but the second piece came 60 to 160 ms after the first had played out.
  Owning the loop also gives the GPU yield points below and a per-sentence
  token bound, and skips `stream_generate`'s own sentence split, which
  voiced has already done.
- **GPU, under the lock.** Before each T3 step and each render the engine
  calls the sentence's `voiced.gpu.Yielder`, as Kokoro does before each of
  its blocks (ADR 0019): it waits while an STT encoder call holds the lock,
  up to 300 ms per sentence. A token step is about 8 ms of GPU work, so an
  STT call waits behind at most one; a render is one evaluation of 230 to
  350 ms that the engine cannot split without patching S3Gen, so an encoder
  call that starts during one waits for it (see Measurements).
- **Length bound.** Each sentence may emit at most 0.12 s of speech per
  character plus 1 s, in tokens, Soprano's bound (ADR 0010); mlx-audio's
  own is 2000 tokens, 80 s, whatever the sentence.
- **The speech tokenizer is not loaded.** mlx-audio offers no argument to
  skip it or to name its revision: `ChatterboxTurboTTS.post_load_hook`
  (`chatterbox_turbo.py`, line 303 at 0.5.9) calls
  `hf_hub_download("mlx-community/S3TokenizerV2", "model.safetensors")`,
  which resolves `main` on the Hub at every start, and loads it. Building
  the model without that hook would mean re-doing mlx-audio's generic
  loader (config, sanitize, quantize, load) and the hook's tokenizer and
  conditionals, about 40 lines on its internals. Instead, for the duration
  of `load()` only, `huggingface_hub.hf_hub_download` refuses that one
  repository (every other call passes through, and the function is
  restored in a `finally`); the hook logs that the tokenizer was not loaded
  and goes on, and the engine drops the unloaded module. Nothing at load
  resolves anything online, which matters once the services run with
  `HF_HUB_OFFLINE` (ADR 0022), and the service holds 0.5 GB less. Cloning
  from a reference clip, later, would pin the tokenizer and load it.
- **Voices.** One, `default`: the conditionals the checkpoint ships,
  pinned with the model. Cloning from a user's clip is later; it needs the
  speech tokenizer, a place to keep the clips and their consent, and its
  own ADR.
- **Tags.** voiced's text normalization leaves `[laugh]` and its kind as
  they are and the sentence split keeps them with the sentence after them,
  so they reach the model's tokenizer as the tokens it knows. No code was
  needed. Other engines read them as words.
- **Speed.** Turbo has no rate control; `speed` is checked against the
  shared bounds and ignored.

## Measurements

Apple M1 Max, 64 GB, macOS 15.7.4, with no other model running (the runs
took a lock other benchmarks on the machine honour).

In process, `ChatterboxEngine.synthesize` on normalized text, voice
`default`, after load and warm-up (n=6 per sentence; first audio is the
call to its first piece; a gap is time a listener playing the pieces as
they come would hear silence):

| Metric | 4-bit (default) | fp16 |
|---|---|---|
| First audio, "Turn on the kitchen lights." | median 309 / worst 319 ms | median 331 / worst 345 ms |
| First audio, a 4.5 s sentence | median 316 / worst 319 ms | median 344 / worst 349 ms |
| 70-word reply (15 s of audio): time, real-time factor, gaps | 6.5 s, 0.44, none | 7.1 s, 0.47, none |
| Footprint after load / after the reply | 0.62 / 0.83 GB | 2.58 / 2.82 GB |
| Footprint after synthesis with mlx-audio's own load, speech tokenizer included | 1.05 GB | 3.05 GB |
| Load from the cache | 2.1 s | 2.6 s |

With the first render at 10 tokens instead of 15, the 4-bit build's first
audio was 285 ms median but every reply had 60 to 160 ms of gaps.

`scripts/bench.py` against `voiced serve stt` (Parakeet), `voiced serve tts`
(Kokoro) and a third `voiced serve tts` running Chatterbox 4-bit, each in
its own process, over real sockets. bench.py has no Chatterbox flag yet, so
the Chatterbox server was passed as `--phonon-url` and its rows are the ones
bench.py labels Phonon:

| Metric | Measured |
|---|---|
| Chatterbox first audio, requests back to back | median 638 / p95 665 / worst 668 ms (n=10) |
| Chatterbox first audio while another client streams to STT, requests 0.2 to 0.6 s apart | median 339 / p95 414 / worst 435 ms (n=30) |
| Kokoro first audio, same run | median 96 / p95 98 / worst 98 ms (n=10) |
| STT streaming finalize, 64 ms chunks, 0 ms trailing silence, alone | median 114 / p95 126 / worst 126 ms (n=32) |
| The same while Chatterbox synthesizes back to back | median 109 / p95 131 / worst 131 ms (n=16) |
| The same while Kokoro synthesizes, for comparison | median 102 / p95 109 / worst 112 ms (n=16) |
| The same with 200 ms trailing silence, Chatterbox synthesizing | median 1 / p95 2 / worst 3 ms (n=16) |
| TTS (Chatterbox) memory after a long reply | footprint 0.89 GB, RSS 0.73 GB, MLX peak 1.45 GB |
| STT + TTS (Chatterbox) footprint, idle after use | 2.68 GB (target 3 GB) |

An earlier run the same way measured the 0 ms finalize at median 102 /
worst 124 ms alone and 129 / 143 ms while Chatterbox synthesized; the
difference between the two runs is as large as Chatterbox's effect.
`scripts/bench_engine.py` (ADR 0024) gave first audio 638 / 652 ms back
to back and 350 / 463 ms while STT streams for 4-bit, 708 / 727 and 395 /
502 ms for fp16, real-time factors 0.43 and 0.47, and footprints 0.90 and
2.99 GB (docs/benchmarks.md).

The back-to-back first audio is not the engine's: both benchmarks read the
first piece of each request and close it, and the HTTP route notices a
client that left only when the engine hands it its next piece, so each
request waited for the previous, abandoned one to reach its next render
(about 0.3 s more). In process, a request after an abandoned one took 306
to 312 ms. A fix in the route, for every engine, is its own change; the
row will be measured again after it.

## Known misses

- First audio is 310 to 340 ms for a fresh request, three times Kokoro's
  95 ms and over the 200 ms target Kokoro is held to: an opt-in engine for
  those who want its voice and its tags, not for the lowest latency. The floor is
  S3Gen's fixed cost per render; a smaller first chunk does not move it
  much and starves playback.
- An STT encoder call that starts during a render waits for it, and a
  render is up to about 350 ms. In the runs above the push-to-talk
  finalize stayed inside its 150 ms worst case (131 and 143 ms) while
  Chatterbox synthesized, but a longer sentence has longer renders.
- mlx-audio prints `S3 Token -> Mel Inference...` and a progress bar for
  each render to the service's stdout and stderr, which `VOICED_STDIO_LOG`
  collects: a few lines per sentence. Removing them would mean patching
  mlx-audio's flow-matching module, so they stay until upstream drops them.

## Consequences

- A fifth engine to keep working against mlx-audio's T3 and S3Gen methods,
  which are not a documented API; the existing `<0.5.10` pin holds them,
  and a model test checks the silence token voiced copies.
- Every new mlx-audio release needs `post_load_hook` checked: if it gains a
  way to skip the speech tokenizer or pin its revision, the download guard
  goes.
- Cloning, when it comes, adds `mlx-community/S3TokenizerV2` to the pins
  and loads it on demand.
