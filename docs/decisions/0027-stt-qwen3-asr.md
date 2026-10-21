# 0027: Qwen3-ASR ships selectable, for files and languages

Date: 2026-10-20. Status: accepted.

## Context

Qwen3-ASR is Alibaba Qwen's open speech recogniser: an audio encoder in
front of a Qwen3 language model that writes the transcript token by token.
It comes in 0.6B and 1.7B sizes, covers 30 languages (Chinese, Cantonese,
Japanese, Korean, Arabic, Hindi and Thai among them, none of which Parakeet
v3 has) and detects the language when it is not told. A separate model,
Qwen3-ForcedAligner-0.6B, times each word of a known transcript in 11 of
those languages. On the Open ASR Leaderboard the 1.7B averages 5.75 WER and
the 0.6B 6.60, against 6.51 for Parakeet TDT 0.6B v3 (issue #59). Upstream
streams only on vLLM; on MLX it is a batch model.

Checked on an Apple M1 Max (64 GB, macOS 15.7.4) with Python 3.12 before
building anything:

- mlx-audio 0.5.9, the release the `tts` extra already pins, has
  `qwen3_asr` and `qwen3_forced_aligner`. They need `transformers` for the
  tokenizer and the feature extractor, already in the lock through
  mlx-audio. No PyTorch.
- It runs on MLX. The 0.6B 8-bit build loaded in 2.1 s cold (0.9 s warm)
  with 1.01 GB of MLX allocations.
- Warm batch latency with an English hint: the 3.4 s fixture in 177 to
  252 ms median (189 to 287 ms worst over two runs of five), a 10 s clip in
  268 to 315 ms median (303 to 343 ms worst). Auto-detection costs the same.
- The aligner adds 1.28 GB (2.29 GB with both loaded), 160 ms median to
  align 10 s, and a 3.38 GB MLX peak.
- Long input in one piece fails: 300 s of speech decoded in one call looped
  until the 8192-token default (6768 words for about 920 spoken) and peaked
  at 4.26 GB. In 30 s pieces it took 10.4 s and peaked at 2.22 GB.
- It caches in the standard Hugging Face hub cache, and loads a local
  checkpoint folder without the Hub.

## Licences

| What | Licence | Source |
|---|---|---|
| `mlx-community/Qwen3-ASR-0.6B-8bit`, `-1.7B-8bit` | Apache-2.0, not gated | model cards, `license: apache-2.0` |
| `mlx-community/Qwen3-ForcedAligner-0.6B-8bit` | Apache-2.0, not gated | model card |
| `Qwen/Qwen3-ASR-0.6B`, `Qwen/Qwen3-ForcedAligner-0.6B`, the originals | Apache-2.0 | model cards |
| QwenLM/Qwen3-ASR, the reference code | Apache-2.0 | `LICENSE` |
| mlx-audio, the runtime | MIT | already a dependency |

All allow commercial use. voiced redistributes no weights.

## Decision

`qwen3-asr` is a second STT engine, `voiced.engines.qwen3_asr.Qwen3ASREngine`,
selectable and never the default. Parakeet stays the default: it streams,
with partials and a finalize within ADR 0008's targets, which is what Home
Assistant and push-to-talk need. Qwen3-ASR is the engine for transcribing
files and for the languages Parakeet lacks.

- **Dependency.** No new extra: mlx-audio is added to the `stt` extra at
  the `tts` extra's range, so the STT side declares what it imports; there
  are no new packages in the lock. Everything that imports mlx-audio runs inside `load()`.
- **Models.** `mlx-community/Qwen3-ASR-0.6B-8bit` is the default model:
  1.01 GB of weights, closest to Parakeet in accuracy and memory.
  `mlx-community/Qwen3-ASR-1.7B-8bit` works as `model` (2.47 GB of
  weights); any other Qwen3-ASR repository of mlx-community's is refused
  (`ModelRefused`), a local checkpoint folder is not. Both and
  the aligner are rows of ADR 0022's catalogue, the aligner as the engine's
  shared model, so `voiced models download` fetches it with either model
  and deletes it with the last; the engine loads the pinned revisions from
  the cache and never downloads.
- **Batch.** `transcribe` cuts the input into pieces of about 30 s
  (`CHUNK_S`) with mlx-audio's splitter, at the quietest 100 ms within 5 s
  of each limit, and decodes each piece greedily in its own call, with its
  own bound of 20 tokens per second of the piece (`TOKENS_PER_S`; English
  took 4) and, without a hint, its own detected language. Left to chunk
  the input itself, mlx-audio shares one token budget across the pieces
  and stops when it is spent, so a loop in the first piece dropped every
  later one, and it detects the language once, on the first piece, and
  forces it on the rest. Each piece is then aligned with its own text,
  and its words grouped into sentences with voiced's sentence splitter,
  counting words with the aligner's own tokenizer: one segment per
  sentence, times in input seconds. A piece in a language the aligner does
  not cover, whose aligned words do not match the text, or whose alignment
  fails for any reason (logged as a warning), is one segment: the
  transcript is never lost to the aligner. Silence (no 32 ms frame above
  `voiced.audio.SILENCE_FLOOR`, -60 dBFS, shared with Parakeet) and input
  under 100 ms never reach the model: it writes words for near-silent
  noise.
- **Segments through the aligner.** It runs in reasonable memory for
  what this engine is for, and it stays loaded. It adds 1.28 GB, and with
  it the STT service is past ADR 0008's 3 GB budget for the two services
  together (the numbers are under Measurements); without it the 0.6B would
  fit. The aligner is kept anyway: Qwen3-ASR is opt-in and never the
  default, it is chosen for transcribing files, where sentence times for
  `srt`, `vtt` and `verbose_json` are the point. The budget exists so the
  default pair leaves room beside an LLM, and the default pair still meets
  it; whoever picks this engine trades that room for subtitles, and can
  switch back to Parakeet for live use. Aligning costs about
  16 ms per second of audio, and keeping it loaded means the first `srt`
  request does not pay for a load. Japanese and Korean are aligned only
  where their word tokenizers (`nagisa`, `soynlp`) are importable; voiced
  does not install them, so those two languages get one segment per piece.
  Downloading either model fetches the aligner; if it is missing, loading
  reports the model as not downloaded, naming the aligner (ADR 0022).
- **Streaming.** A session buffers `add_audio` and transcribes the whole
  turn at `finalize`, with no aligner (one segment per 30 s piece: finalize
  is on the critical path and a stream's segments are rarely used).
  `partial()` is always `""`: there is no partial at all, rather than the
  one the issue allowed. A partial would be a full decode of everything so
  far on the one model thread, the cost of a finalize each time, and a
  finalize asked for during one waits for it. Finalize holds
  `voiced.gpu.stt_turn` around each piece's decode, since a client waits
  for it (ADR 0019), so a long turn lets the TTS in between pieces; a batch
  request does not take it, as with Parakeet. `max_streams` is 1, and a
  batch request is refused while a session is open.
- **Languages.** `languages` are the 30 primary subtags (`en`, `zh`, `yue`,
  `fil`, ...) mapped to the model's names in `LANGUAGES`; the service
  serves those in `stt.languages`, as before. A hint goes to the model by
  name and comes back as given. Without one the model names the language
  it heard, which is mapped back to its subtag (`None` for a name outside
  the table).

## Alternatives considered

- **Partials by decoding again.** Each partial a full decode of the turn
  so far: on one model thread it costs a finalize every time and delays the
  real finalize behind it. Rejected; no partials.
- **The whole input in one call.** Simplest, but 300 s looped to the token
  limit at a 4.26 GB peak. Rejected for 30 s pieces, decoded one at a
  time (60 s pieces lost a tenth of the words).
- **The 1.7B as the default model.** 1.6% against 1.8% word error rate on
  the clip set, at about twice the finalize time and 1.5 GB more. Rejected:
  it is the catalogue's second model.
- **No aligner.** One segment per 30 s piece fits the memory budget (about
  1.9 GB with Kokoro), but subtitles from a file engine would be 30 s long.
  Rejected, see *Segments through the aligner*.

## Measurements

On an Apple M1 Max with 64 GB, 2026-10-21. `scripts/bench_engine.py`, each
service alone (ADR 0024; the rows in [benchmarks.md](../benchmarks.md)):

| Model | WER | Finalize after 200 ms silence, median / worst | At the last word | Batch RTF | Footprint | MLX peak, load / after use |
|---|---|---|---|---|---|---|
| 0.6B 8-bit | 1.8% | 192 / 242 ms | 184 / 216 ms | 0.056 | 2.50 GB | 3.14 / 3.41 GB |
| 1.7B 8-bit | 1.6% | 315 / 354 ms | 307 / 341 ms | 0.071 | 3.95 GB | 4.60 / 4.86 GB |

The rows' Size (1.0 GB, 2.5 GB) is the model's own repository; the shared
aligner (1.28 GB) is not counted, so a download is 2.3 GB or 3.7 GB in
all. Counting shared models is a harness follow-up. Parakeet on the same
harness: 2.0%, finalize 6 ms worst after silence, batch RTF 0.023, 1.52 GB.

`scripts/bench.py` with the 0.6B and Kokoro up together, as they serve:

| Measure | Result | Target |
|---|---|---|
| Finalize, 64 ms chunks, 200 ms silence | 188 median / 224 ms worst | 50 / 150 ms |
| The same, Kokoro synthesizing | 178 / 277 ms | 50 / 150 ms |
| Kokoro first audio while a client streams to Qwen3-ASR | 116 median / 194 ms worst | 200 ms |
| Batch, 10.1 s clip over HTTP, with the aligner | 481 ms median | 300 ms |
| STT footprint, idle after use | 2.46 GB (MLX peak 3.37 GB) | |
| STT + Kokoro footprint, idle after use | 3.21 GB | 3 GB |

In process, warm, by turn length (finalize is a stream's decode; batch
includes the aligner and is what every batch request pays):

| Turn | 0.6B finalize | 0.6B batch + align | 1.7B finalize | 1.7B batch + align |
|---|---|---|---|---|
| 3 s | 132 ms | 222 ms | 225 ms | 314 ms |
| 10 s | 266 ms | 421 ms | 523 ms | 681 ms |
| 30 s | 611 ms | 952 ms | 1312 ms | 1701 ms |

Memory in process: 2.29 GB active with the 0.6B and the aligner, 3.75 GB
peak while serving a 30 s turn; 3.75 GB and 5.20 GB with the 1.7B. 300 s
of audio transcribes in 10.4 s in 30 s pieces with a 2.22 GB peak; in one
piece the decoder looped to the token limit (6768 words for about 920
spoken), which is why it is cut into pieces and bounded.

## Known misses

- Streaming finalize misses ADR 0008's 50 ms median and 150 ms worst, by
  design: finalize decodes the whole turn, so it grows with the turn. The
  0.6B takes about 130 ms for a 3 s turn, 270 ms for 10 s and 610 ms for
  30 s; the 1.7B about twice as long. With Kokoro synthesizing the worst
  case rose to 277 ms. A voice assistant should keep Parakeet.
- Batch misses ADR 0008's 300 ms for 10 s of audio: 421 ms in process with
  the aligner (266 ms without), 481 ms over HTTP. Every batch request pays
  the aligner, `json` and `text` included.
- Memory misses ADR 0008's 3 GB for the two services together: 3.21 GB
  with Kokoro idle after use, and the STT alone peaks at 3.4 GB (3.75 GB on
  a 30 s turn) before Kokoro's own peak. The aligner's 1.28 GB is the
  difference; *Segments through the aligner* says why it stays. The 1.7B
  needs about 4 GB at rest.
- No partial transcripts while a stream runs.
- One stream at a time.
- Sentence timestamps only in the aligner's languages that voiced can
  tokenize: Chinese, Cantonese, English, French, German, Italian,
  Portuguese, Russian, Spanish; Japanese and Korean only with `nagisa` or
  `soynlp` installed. Elsewhere one segment per 30 s piece.

## Consequences

- A second STT engine to keep working on mlx-audio's internals, which the
  pin in `pyproject.toml` names: it imports `split_audio_into_chunks` from
  `mlx_audio.stt.models.qwen3_asr.qwen3_asr`, calls
  `generate(piece, chunk_duration=inf, max_tokens=)` and reads
  `result.segments[0]["language"]`, and uses the aligner's
  `get_supported_languages()`, `items` and
  `aligner_processor.encode_timestamp`. The
  fake tier runs the engine against stand-in `mlx` and `mlx_audio` modules
  on CI; the model tier checks load, batch with segments, detection,
  silence, a stream against the batch transcript, unknown languages,
  `/v1/models` and a transcription over HTTP, and the 1.7B. The e2e suite
  subtitles Kokoro's speech through it.
- The aligner as a second model inside one engine is a pattern a later
  engine can copy through the catalogue's `shared` models.
- Every batch request aligns, `json` and `text` included, because
  `STTEngine.transcribe` cannot be told whether segments are wanted (the
  interface is frozen). A later interface change could pass that and save
  the aligner's time on those responses.
- The buffering stream session and the `LANGUAGES` table stay in this
  module until a second engine needs them (Whisper would); then they move
  to a shared `BufferedStreamSession` and a language-name table beside
  `voiced.text.language`.
