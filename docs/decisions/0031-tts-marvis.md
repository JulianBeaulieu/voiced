# 0031: Marvis ships selectable, not default, for French and German voices

Date: 2026-10-21. Status: accepted.

## Context

Marvis is Marvis-AI's small text-to-speech model of Sesame's CSM design: a
backbone transformer predicts the first Mimi codebook of each 80 ms frame
from the text and the audio so far, a 60M decoder transformer the other 31,
and Mimi's decoder turns the frames into 24 kHz audio. No phonemizer. A
voice is a reference clip and its transcript, which the model continues.
v0.2 (2025-10-20) comes as a 250M and a 100M backbone, each converted for
MLX at 4, 6 and 8 bits; its card lists English, French and German.

mlx-audio, already in voiced's pin (0.5.9), runs it as its `sesame` model
(the `marvis` name maps to it). Checked on an Apple M1 Max (64 GB, macOS
15.7.4) with Python 3.12 before building anything:

- mlx-audio 0.5.9 loads both 8-bit conversions; no new package, no
  PyTorch. The tokenizer is `transformers`' (already an mlx-audio
  dependency).
- The MLX conversions' `config.json` names its tokenizer by repository
  (`Marvis-AI/marvis-tts-250m-v0.2`, or the 100m one), and the voice clips
  live there too. mlx-audio fetches Mimi's weights from
  `kyutai/moshiko-pytorch-bf16` without a revision.
- mlx-audio's `generate` encodes the voice's 13 s clip through Mimi for
  every sentence and yields audio in batches of frames: warm, through it,
  first audio was 171 ms for the 250M and 160 ms for the 100M at its
  shortest interval (two frames), against Kokoro's 94 ms. Real-time factor
  about 0.55. MLX peak 2.5 and 2.3 GB.
- It applies CSM's watermark only when `silentcipher` is importable;
  voiced does not install it, so the audio carries no watermark.
- The upstream repository, `Marvis-Labs/marvis-tts`, has had no commit
  since 2025-08-28 (its licence file); the Hugging Face models were last
  updated 2025-11-06. Nothing in voiced depends on upstream moving: the
  weights are pinned and mlx-audio carries the model.

## Licences

| What | Licence | Source |
|---|---|---|
| `Marvis-AI/marvis-tts-{100m,250m}-v0.2-MLX-8bit` (weights) | Apache-2.0, not gated | model cards |
| `Marvis-AI/marvis-tts-250m-v0.2` (tokenizer, voice clips) | Apache-2.0, not gated | model card |
| `kyutai/moshiko-pytorch-bf16`, `tokenizer-e351c8d8-checkpoint125.safetensors` (Mimi) | CC-BY-4.0, not gated | model card |
| `Marvis-Labs/marvis-tts` (training and inference code, not used) | Apache-2.0 | GitHub |
| mlx-audio, the runtime | MIT | package metadata |

The voice clips are four scripted readings (a paragraph about coffee for
the two English ones, a sentence each for French and German), published in
the Apache-2.0 repository with no other terms. The card says the model
was trained on Emilia (CC-BY-NC-4.0 for the data); as for the other engines, voiced takes
the weights' own licence. The card asks for consent before cloning a voice;
voiced offers no cloning. The licence voiced reports for each model is
`Apache-2.0; codec: CC-BY-4.0`, and the README carries Kyutai's attribution.
voiced redistributes none of the files: each Mac downloads them.

## Decision

`marvis` is a TTS engine, `voiced.engines.marvis.MarvisEngine`, a
`SentenceTTSEngine`, selectable and never the default. What it is for:
French and German voices under Apache-2.0, at a quality a little below
Kokoro's (rated 4 of 5 against 5). Not for English, where Kokoro sounds
more natural and does a twelfth of the work for each second of audio.

- **Its own frame loop.** The engine does not call mlx-audio's `generate`;
  `_stream` runs its steps: the voice's transcript and the sentence as one
  text, then the voice's Mimi frames, which the model continues one frame
  at a time until it samples an all-zero frame, each frame decoded by
  Mimi's streaming decoder. What changes is when the work is done: each
  voice's clip is encoded once at load, not for every sentence, and each
  frame is yielded as soon as it is decoded, so first audio is one frame
  after the prompt. That took first audio from 171 to 63 ms in process
  (250M; 160 to 51 ms for the 100M). The sampling is mlx-audio's
  (temperature 0.9, top-k 50). Fifteen samples of the three naturalness
  sentences scored alike through both: UTMOS 4.07 through the loop, 4.02
  through `generate` (250M).
- **GPU, behind the STT.** Before every frame it waits while an STT encoder
  call holds the lock (`voiced.gpu.Yielder`, ADR 0019). A frame is about
  40 ms of GPU work: 32 small decoder passes and one Mimi step.
- **Bounded sentences.** Now and then the model does not sample its end
  frame and keeps making sound after the sentence (a run-on). A sentence
  may run for at most `sentence_bound`'s seconds (0.12 s a character and
  1 s more) in frames; at the bound it stops, fades out its last 10 ms and
  logs a warning. That is a truncation, and a run-on still plays up to the
  bound: for a five-word sentence, about 3 s of extra sound. The splitter's
  300-character sentences keep a sentence well inside the backbone's 2048
  positions, so that limit is checked, not relied on. No retry: it would
  double the latency of the sentences that run on and hide the fault.
- **Run-on rates.** With the 100M, over 20 everyday assistant sentences
  per language, each read four times: French 1 of 80, German 4 of 80,
  English 2 of 80 (`conversational_a`); "Le renard brun rapide saute
  par-dessus le chien paresseux." ran on 1 time in 10 here and 3 in 8 in
  an earlier run. With the 250M, French ran on 2 of 20 and German 0 of 20,
  but "Allume les lumières de la cuisine." ran on every time (5 of 5), and
  mlx-audio's own `generate` does the same (1.4 to 9 s for that sentence).
  Run-ons cluster on particular sentences rather than on a language, and
  at a few in a hundred all three languages are served.
- **One model thread, one sentence at a time.** The attention caches and
  Mimi's decoder state are MLX arrays on the model, kept from frame to
  frame and reset when a sentence starts; the load and every step run on
  the engine's `ModelThread` (ADR 0028), and `_stream` holds a lock from
  the first frame to the close, so an overlapping sentence fails loudly.
- **No Hub at run time.** The engine builds the `sesame` `Model` itself
  from the pinned `config.json` with `text_tokenizer` pointed at the pinned
  tokenizer, and the weights as mlx-audio's `load_model` would load them.
  The model's constructor fetches Mimi with an unpinned `hf_hub_download`
  and offers no way to name a file, so for the construction the codec
  module's `hf_hub_download` is replaced by one that returns the pinned
  file (and refuses anything else), then restored, as Chatterbox does for
  its tokenizer (ADR 0029). Once every voice is encoded, Mimi's encoder
  (about 155 MB of float32 weights) is freed: decoding does not use it.
- **24 kHz, not resampled.** Mimi decodes at 24 kHz, voiced's rate, so the
  engine checks the model's rate at load and refuses another rather than
  resampling.
- **8-bit conversions.** Marvis-AI publishes 4, 6 and 8-bit conversions of
  each model; voiced pins the 8-bit ones, the closest to the full-precision
  weights, the size the survey and issue named. The 4 and 6-bit ones were
  not measured: the weights are 0.45 GB of a 1.2 GB footprint, so they
  would save little memory, at a cost in quality not checked.
- **Models pinned.** The catalogue lists the 100M (default) and the 250M
  8-bit conversions (`config.json`, `model.safetensors*`: 452.4 and
  665.5 MB), and two shared models: `Marvis-AI/marvis-tts-250m-v0.2`'s
  tokenizer and four clips (6.6 MB; the 100M's repository has the same
  tokenizer and English clips, byte for byte, but no French or German
  ones) and Mimi (384.6 MB). Another `Marvis-AI/` id is refused when the
  engine is made (`ModelRefused`).
- **The 100M is the default model.** It scored better here (UTMOS 4.27
  against 3.98), its first audio is shorter, it holds 0.2 GB less, and its
  French runs on less often. The 250M stays in the catalogue, its note
  saying so.
- **Voices.** The four clips, in a static table: `conversational_a`
  (female, the default, mlx-audio's too), `conversational_b` (male), both
  `en`; `conversational_fr` (male, `fr`, named Conversational FR) and
  `conversational_de` (male, `de`, Conversational DE). The genders are
  from the clips' pitch (about 180 Hz against 105 to 120 Hz). The model continues a clip, so each voice speaks its own
  language; the engine declares `en`, `fr` and `de`, and a voice is served
  when its language is in `tts.languages` (ADR 0023), whose default is
  English only. A sentence without a letter or a digit is skipped.
- **Speed.** No rate control; `speed` is checked against the shared bounds
  and ignored.

## Measurements

Apple M1 Max, 64 GB, macOS 15.7.4, nothing else running.

`scripts/bench_engine.py tts --engine marvis` (ADR 0024), one row per
model in `docs/benchmarks.md`:

| Model | UTMOS | First audio, median / worst | While STT streams | RTF | Footprint | MLX peak | Rated |
|---|---|---|---|---|---|---|---|
| 100M 8-bit | 4.27 | 89 / 89 ms | 79 / 114 ms | 0.490 | 1.23 GB | 2.31 GB | quality 4, speed 3 |
| 250M 8-bit | 3.98 | 102 / 139 ms | 92 / 157 ms | 0.496 | 1.45 GB | 2.53 GB | quality 3, speed 3 |
| Kokoro, for comparison | 4.52 | 94 / 98 ms | 112 / 226 ms | 0.042 | 0.73 GB | 1.86 GB | quality 5, speed 5 |

UTMOS of a sampled model varies from one reading to the next: over 15
readings (250M, `conversational_a`) single sentences scored 3.4 to 4.4,
mean 4.05, so the 0.3 between the two models is within that spread; the
100M scored 4.16 and 4.27 in two harness runs, the 250M 3.96 and 3.98. The
harness's three readings are what the ratings use. One harness run of the
100M, with another process on the GPU, measured a real-time factor of 0.71
and 191 / 781 ms first audio while STT streamed; it was run again on a
quiet machine, and the row above is that run.

`scripts/bench.py` with `--extra-tts Marvis100M=URL,conversational_a` and
the same for the 250M, against `voiced serve stt` (Parakeet), `voiced serve
tts` (Kokoro) and two more `voiced serve tts`, one per Marvis model, each
in its own process, over real sockets. First audio is the `openai`
client's streamed `pcm` request for "Turn on the kitchen lights." to the
first byte, after one warm-up.

| Metric (`bench.py`) | Measured | Target |
|---|---|---|
| Marvis 100M first audio | median 90 / p95 104 / worst 115 ms (n=10) | <= 200 ms |
| Marvis 250M first audio | median 103 / p95 118 / worst 129 ms (n=10) | <= 200 ms |
| Kokoro first audio, same run | median 95 / p95 98 / worst 99 ms (n=10) | <= 200 ms |
| Marvis 100M first audio while another client streams to STT | median 78 / p95 149 / worst 182 ms (n=30) | <= 200 ms |
| Marvis 250M, the same | median 92 / p95 122 / worst 123 ms (n=30) | <= 200 ms |
| Streaming finalize, 64 ms chunks, 0 ms trailing silence, alone | median 114 / p95 122 / worst 122 ms (n=32) | <= 150 ms worst |
| The same, Marvis 100M synthesizing | median 105 / p95 122 / worst 123 ms (n=16) | <= 150 ms worst |
| The same, Marvis 250M synthesizing | median 98 / p95 100 / worst 100 ms (n=16) | <= 150 ms worst |
| The same with 200 ms trailing silence, either Marvis synthesizing | median 1 / worst 1 ms (n=16 each) | <= 150 ms worst |
| TTS (Marvis 100M) memory after use | footprint 1.24 GB, RSS 0.98 GB, MLX active 784 MB, peak 2314 MB | |
| TTS (Marvis 250M) memory after use | footprint 1.43 GB, RSS 1.18 GB, MLX active 995 MB, peak 2526 MB | |
| STT (Parakeet) memory, same run | footprint 1.80 GB, MLX peak 3258 MB | |
| STT + TTS (Marvis 100M) footprint, idle after use | 3.04 GB | <= 3 GB |
| STT + TTS (Marvis 250M) footprint, idle after use | 3.23 GB | <= 3 GB |
| STT + TTS (Kokoro) footprint, same run | 2.55 GB | <= 3 GB |

Freeing Mimi's encoder after the voices are encoded took the Marvis
service's footprint from 1.40 to 1.24 GB (100M) and 1.61 to 1.43 GB (250M);
before it, the pair took 3.20 and 3.41 GB.

The STT keeps its targets while Marvis synthesizes: the engine waits for
every encoder call, one frame at a time.

## Known misses and limits

- **Memory.** Beside the STT service, the pair takes 3.04 GB with the 100M,
  just over the 3 GB budget for both services (ADR 0008), and 3.23 GB with
  the 250M. Neither model meets it; the 100M misses by less, which is one
  more reason it is the default. Most of the rest is Mimi's float32
  decoder and the buffers of the per-frame passes.
- Quality: below Kokoro (4.27 against 4.52), rated 4 of 5; in English,
  Kokoro is the better choice.
- A real-time factor of about 0.5: a reply takes half its length to
  synthesize, against Kokoro's 4%. Streaming keeps it ahead of playback,
  but a long reply keeps the GPU busy for longer, and the speed rating is
  3 of 5.
- MLX peaks at 2.3 GB in the TTS process while it synthesizes, for 0.45 GB
  of weights: Mimi and the per-frame decoder passes' buffers.
- **Run-ons.** A few sentences in a hundred run on to the bound (above),
  and some sentences do so far more often; the bound cuts them, so such a
  sentence ends with a few seconds of extra sound.
- French and German text is normalized only as ADR 0023 does for those
  languages: digits and symbols are left for the model to read.
- The engine relies on private parts of mlx-audio's `sesame` model, named
  in `pyproject.toml`'s pin comment; a bump of mlx-audio has to re-run the
  model tier.
- Upstream is quiet (no commit since 2025-08-28): fixes to the model, its
  run-ons among them, are not to be expected.
- Not measured on the Mac Studio.

## Consequences

- A seventh TTS engine to keep working; the fake tier runs on CI without
  MLX and the model tier covers the private calls.
- The menu bar app lists the four voices with their languages through
  `engines.list`, and both models with their ratings.
- voiced has an Apache-2.0 path to French and German speech; OmniVoice's
  is non-commercial and slow.
