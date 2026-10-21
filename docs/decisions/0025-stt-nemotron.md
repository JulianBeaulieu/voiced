# 0025: Nemotron ships as a selectable streaming STT engine

Date: 2026-10-21. Status: accepted.

## Context

NVIDIA's Nemotron ASR streaming models are cache-aware FastConformer-RNNT
models of 0.6B parameters. The encoder keeps its attention and convolution
caches between chunks, so each chunk of audio is encoded once and a call
costs the same at the end of a long utterance as at its start. Parakeet
re-encodes its right context on every call (ADR 0008). Two checkpoints
matter:

- **Nemotron 3.5** (`nvidia/nemotron-3.5-asr-streaming-0.6b`): multilingual
  with language identification. The card lists 40 locales: 19
  "transcription-ready", 13 "broad-coverage" and 8 "adaptation-ready" that
  need fine-tuning first. voiced serves the first 32, in 28 languages.
  `mlx-community/nemotron-3.5-asr-streaming-0.6b` is its MLX conversion in
  bf16 (1.28 GB).
- **The English model** (`nvidia/nemotron-speech-streaming-en-0.6b`):
  English only, in the Transformers layout, fp32 (2.47 GB). It has no MLX
  conversion.

Checked on an Apple M1 Max (64 GB) before building anything:

- mlx-audio 0.5.9, already pinned for Kokoro and Soprano, has
  `mlx_audio.stt.models.nemotron_asr`. It loads both checkpoints, including
  NVIDIA's English repository as it is. No new dependency, no PyTorch, MLX
  on the GPU.
- Its streaming session (`create_streaming_session`, `feed`, `step`,
  `close`, `done`) runs the encoder one native chunk at a time and returns
  text deltas. The model card's chunk sizes are 80, 160, 320, 560 and
  1120 ms (a look-ahead of 0, 1, 3, 6 and 13 frames of 80 ms).
- Without a language, 3.5 detects it and emits a `<xx-XX>` tag, which the
  tokenizer strips.

## Licences

| What | Licence | Source |
|---|---|---|
| `nvidia/nemotron-3.5-asr-streaming-0.6b` | OpenMDW-1.1 | model card, since 2026-06-05 |
| `mlx-community/nemotron-3.5-asr-streaming-0.6b` (the weights voiced loads) | OpenMDW-1.1, as above | its card: "a format conversion of those weights; the same license and terms apply" |
| `nvidia/nemotron-speech-streaming-en-0.6b` | NVIDIA Open Model License (2025-10-24) | model card |
| mlx-audio, the runtime | MIT | package metadata |

NVIDIA's card for 3.5 changed licence twice on the Hub: the NVIDIA
Software and Model Evaluation License from its release on 2026-05-15, the
NVIDIA Open Model License on 2026-06-04, and OpenMDW-1.1 from 2026-06-05
to now. The checkpoint itself last changed on 2026-05-30; the later commits
are configs, documentation and other formats. The mlx-community conversion
was uploaded on 2026-06-04 and its card still names the NVIDIA Open Model
License, but it says the original's licence and terms apply, and the
original's are now OpenMDW-1.1. voiced reports OpenMDW-1.1 for it.

OpenMDW-1.1 is permissive: use, modification and redistribution with no
field-of-use restrictions; a redistribution keeps a copy of the agreement
and the notices, and the rights end only for someone who brings patent or
copyright litigation over the model. The NVIDIA Open Model License also
allows commercial use. A redistribution carries the notice "Licensed by
NVIDIA Corporation under the NVIDIA Open Model License". The rights end on
patent or copyright litigation, and for anyone who bypasses or disables
the model's guardrails without a substantially similar replacement.
voiced redistributes neither: each Mac downloads the weights from Hugging
Face. Both licences cover the weights, not the transcripts.

## Decision

`nemotron` is an STT engine, `voiced.engines.nemotron.NemotronEngine`,
selectable and never the default. Parakeet stays the default: it is the
batch engine (it transcribes 10 s in about 240 ms, Nemotron in about
800 ms) and the one the Home Assistant path was tuned on.

- **Models.** Both are in the catalogue, each with the locales it serves
  (`Model.languages`). Nemotron 3.5 is the engine's default, chosen for
  its licence, the permissive one, and its 28 languages, although the
  English model is a little better in English: 2.0% word error rate against
  2.5%, and batch at 0.073 of real time against 0.079. Their finalize is the
  same (below). The English model serves `en-US` only. NVIDIA's fp32 weights
  are loaded lazily, cast to bf16 and evaluated one parameter at a time, so
  the fp32 copy is never whole in memory: the load peaks at 2.16 GB (3.38 GB
  when cast after a full load), and the model takes 1.28 GB instead of
  2.51 GB at the same word error rate, 2.03% in fp32 and in bf16 (below).
  The download is still NVIDIA's fp32 repository, 2.47 GB on disk as the
  catalogue lists it; voiced hosts no conversion of its own.
- **Registration.** The engine refuses a `nvidia/nemotron*` or
  `mlx-community/nemotron*` repository the catalogue does not list
  (`ModelRefused`), and takes a catalogued model's languages from the
  catalogue. `engines.list` gives each model's languages in
  `model_languages`. A local folder is taken to be multilingual; one
  without language prompts fails to load with a message naming the
  English model's id.
- **Dependency.** None new: mlx-audio is in the `stt` extra already
  (ADR 0027). Its floor rises to 0.5.9 in both extras: 0.5.8 has neither
  the English model's loader nor the session's `first_chunk_mel`.
- **Languages.** The engine lists locales (`en-US`, `en-GB`, `pt-BR`,
  `pt-PT`, ...), so a region hint survives the service's language check
  and reaches the model: `pt-PT` is transcribed as European Portuguese, not
  Brazilian. A hint without a region picks the first locale of its language
  (`en` is `en-US`, `pt` is `pt-BR`). No hint is automatic detection, and
  the transcript's language is then `None`. The English model has no
  language prompts and ignores them.
- **Chunk.** Streaming uses 160 ms chunks: two 80 ms encoder frames, the
  second the first's look-ahead. 80 ms chunks finalize no faster once the
  pieces are aligned (below), cost half again the GPU time, and the English
  model at 80 ms cut a word short and dropped a question mark.
  `create_streaming_session` takes no context size for 3.5, so the engine
  overwrites the model's `default_att_context_size` once, at load.
- **Streaming.** The library's log-mel front end emits a frame once the
  audio covers half an FFT window (256 samples) past its centre, and its
  encoder takes whole chunks of 16 mel frames. Fed whole 160 ms chunks, the
  encoder always held the last one back until the next arrived: partials
  trailed a chunk, and finalize encoded that chunk and the remainder, two
  passes. So a session feeds the library a first piece of
  `(first chunk - 1) x hop + half window` samples, then whole chunks, each
  ending exactly on a chunk of frames and encoded as it arrives; the sizes
  come from the library's own session at load. `add_audio` awaits one
  model-thread call per piece. `finalize()` encodes the remainder, closes
  the session and decodes the last frames under `stt_turn` (ADR 0019);
  pushes do not take it. One session at a time (`max_streams` 1), and an
  upload is refused while a session is open; the service queues them.
- **Memory.** A stream leaves over 1 GB in MLX's buffer cache. Emptying it
  takes long enough to show in a transcript, and Wyoming closes the session
  before it sends one, so closing a session queues the clear on the model
  thread without waiting for it. Batch empties it after each request.
- **Batch.** A whole upload is transcribed with the widest look-ahead
  (13 frames, 1.12 s), the most accurate setting.
- **Segments.** Batch transcripts carry sentence segments timed from the
  RNNT frame indices of their words, at 80 ms resolution. The words land
  where they were spoken. A sentence's closing punctuation does not: the
  model emits it after its look-ahead, up to 1.3 s late, along with the
  next sentence's leading space. So a segment runs from its first token
  with a letter or digit to the end of its last one. On two sentences with
  a second of silence between them, the library's own sentence times were
  [0.16, 4.56] and [4.48, 6.80] s; timed by their words they are
  [0.16, 3.28] and [4.56, 6.16] s, against speech at 0 to 3.37 s and 4.37
  to 6.01 s. A streaming transcript is one segment from 0 to the end of
  the audio with the whole text: the session's deltas carry no times.

## Measurements

Apple M1 Max (64 GB), 2026-10-21. Finalize is measured from the end of
input to the transcript, over end-of-input alignments 20 ms apart, for a
client that stops at the last word and for one that sends the endpointer's
200 ms of silence.

`scripts/bench.py`, the services started on their own, Kokoro as TTS:

| Metric | Nemotron 3.5 | English, bf16 | Parakeet | ADR 0008 target |
|---|---|---|---|---|
| Finalize, 64 ms chunks, 200 ms silence (median / worst) | 55 / 64 ms | 54 / 90 ms | 1 / 3 ms | 50 / 150 ms |
| Finalize, 64 ms chunks, at the last word | 59 / 69 ms | 55 / 93 ms | 109 / 135 ms | 50 / 150 ms |
| Finalize, 100 ms chunks, 200 ms silence | 55 / 64 ms | 54 / 93 ms | 2 / 21 ms | 50 / 150 ms |
| Finalize, 100 ms chunks, at the last word | 61 / 74 ms | 55 / 92 ms | 112 / 124 ms | 50 / 150 ms |
| Finalize while Kokoro synthesizes, 200 ms / at the last word (worst) | 90 / 116 ms | 93 / 70 ms | 2 / 125 ms | 150 ms |
| Finalize, 2 clients at once, 200 ms / at the last word (worst) | 685 / 723 ms | 766 / 669 ms | 114 / 198 ms | 150 ms |
| Kokoro first audio while STT streams (median / worst) | 122 / 176 ms | 123 / 152 ms | 113 / 209 ms | 200 ms |
| Batch, 10.1 s clip (median) | 841 ms | 768 ms | 236 ms | 300 ms |
| STT footprint, idle after use | 1.44 GB (MLX peak 2.18 GB) | 1.39 GB (MLX peak 2.16 GB) | 1.68 GB (MLX peak 3.81 GB) | |
| STT + TTS footprint, idle after use | 2.18 GB | 2.13 GB | 2.42 GB | 3 GB |

In process, streamed in real time, 64 and 100 ms pieces pooled (median /
worst over 32 finalizes):

| Engine and chunk | At the last word | After 200 ms of silence |
|---|---|---|
| Nemotron 3.5, 160 ms | 63 / 121 ms | 59 / 68 ms |
| Nemotron 3.5, 80 ms | 59 / 80 ms | 53 / 68 ms |
| English, 160 ms | 60 / 74 ms | 49 / 70 ms |
| English, 80 ms | 62 / 91 ms | 48 / 66 ms |
| Parakeet | 120 / 156 ms | 1 / 5 ms |

Before the pieces were aligned, finalize was two encoder passes of 40 to
47 ms: 88 / 97 ms in process for 3.5 at 160 ms, and 95 / 112 ms through
the service. Feeding the remainder together with the close, so that both
passes came from one step, had saved only 4 ms. Aligning the pieces leaves
one pass.

Cost of a 160 ms chunk at points in a 31 s utterance, in process (median
over the following second):

| Model | 0 s | 5 s | 10 s | 20 s | 30 s |
|---|---|---|---|---|---|
| Nemotron 3.5 | 41 ms | 42 ms | 44 ms | 46 ms | 46 ms |
| English, bf16 | 38 ms | 44 ms | 39 ms | 46 ms | 45 ms |

A chunk costs about a quarter of its own duration and stays flat, as the
cache-aware design promises.

Two worst cases stand out, and neither is explained. The English model's
worst finalize through the service is 90 to 93 ms against 3.5's 64 to 74,
with its p95 at 84 to 89 ms, so about one finalize in ten takes some 30 ms
more; its medians are the same as 3.5's. 3.5's 121 ms at the last word in
process is a single finalize out of 32: through the service its worst is
69 ms, and the model tier's runs since peaked at 84 ms.

`scripts/bench_engine.py` (ADR 0024), on its 24 LibriSpeech clips, the
service started alone. The full rows are in `docs/benchmarks.md`:

| Model | WER | Finalize after 200 ms silence | Finalize at the last word | Batch RTF | Footprint | Rating |
|---|---|---|---|---|---|---|
| Nemotron 3.5 | 2.5% | 54 / 65 ms | 60 / 72 ms | 0.079 | 1.44 GB | quality 5, speed 4 |
| English, bf16 | 2.0% | 56 / 90 ms | 55 / 93 ms | 0.073 | 1.39 GB | quality 5, speed 4 |
| English, fp32 (not shipped; before the alignment) | 2.0% | 51 / 77 ms | 51 / 82 ms | 0.042 | 3.01 GB | quality 5, speed 4 |
| Parakeet | 2.0% | 1 / 6 ms | 115 / 129 ms | 0.023 | 1.52 GB | quality 5, speed 5 |

The English model's word error rate is 2.03% in fp32 and in bf16, on the
same clips. fp32 decodes faster, but its 3.01 GB footprint alone is over
the 3 GB budget the STT and TTS services share.

## Known misses

- **Finalize misses the 50 ms median, a little.** About 55 ms through the
  service, with or without trailing silence; the worst case is well inside
  150 ms. Parakeet meets the median with 200 ms of silence, because it
  transcribes during the silence, and misses it by 60 ms at the last word.
- **Batch is slow.** About 800 ms for 10 s against a 300 ms target.
  Nemotron is the streaming engine; use Parakeet for uploads.
- **One stream at a time.** A second client waits, up to 766 ms here.
  Raising `max_streams` to 2 is a follow-up, to be measured with
  `scripts/bench.py --streams 2`.
- **A local folder is taken to be multilingual.** An English-only
  checkpoint in a local folder fails to load; name it by its catalogue id.

## Consequences

- A streaming STT engine with automatic language detection across 28
  languages, under a permissive licence, and an English model with a
  slightly lower error rate.
- For a client that stops at the last word, Nemotron's finalize is about
  half Parakeet's; with the endpointer's silence, Parakeet's is near zero.
- The catalogue and the lock list both models; `voiced models download`
  fetches them and the service never downloads (ADR 0022).
- When mlx-audio moves, the engine relies on what `pyproject.toml`'s pin
  comment names, including three private attributes of the streaming
  session that size the pieces. The model tests cover them.
