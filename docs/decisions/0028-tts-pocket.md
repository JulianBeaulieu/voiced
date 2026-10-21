# 0028: Pocket TTS ships selectable, not default, on the GPU

Date: 2026-10-21. Status: accepted.

## Context

Pocket TTS is Kyutai's small text-to-speech model, and the one Phonon
(ADR 0021) builds on: a flow-matching transformer of about 100M parameters
that predicts Mimi codec latents one 80 ms frame at a time, and Mimi's
decoder, which turns them into 24 kHz audio. No phonemizer, no espeak-ng.
mlx-audio, already in voiced's pin (0.5.8 to 0.5.9), ports it as
`pocket_tts`, and `mlx-community/pocket-tts` holds Kyutai's English
checkpoint converted for it (235.7 MB, the January 2026 English model).

Checked on an Apple M1 Max (64 GB, macOS 15.7.4) with Python 3.12 before
building anything:

- mlx-audio 0.5.9's `pocket_tts` loads the model and needs one package
  voiced does not pin: `sentencepiece` (0.2.2, a `cp312 macosx arm64`
  wheel, no dependencies) for the tokenizer. No PyTorch.
- On the GPU, `generate_audio_stream` yields one 1920-sample frame per
  step. Warm, the first frame came after 32 ms median, 36 ms worst (n=6),
  and a sentence synthesized at 6 to 10 times real time. MLX peak 0.96 GB,
  RSS 420 MB.
- MLX on the CPU (`mx.set_default_device(mx.cpu)`) was 225 ms to the
  first frame and 0.5 times real time: too slow to stream.
- A sentence's generator keeps MLX arrays (the attention cache, Mimi's
  decoder state) from one frame to the next. Advanced from a second thread,
  it fails on its first frame with "There is no Stream(gpu, 3) in current
  thread".
- The model's `config.json` names its tokenizer on the Hub
  (`hf://kyutai/pocket-tts-without-voice-cloning/tokenizer.model@d4fdd22`),
  and mlx-audio fetches its voices from the same repository by name.

## Licences

| What | Licence | Source |
|---|---|---|
| `mlx-community/pocket-tts` (weights, converted) | CC-BY-4.0, not gated | model card, `license: cc-by-4.0`, base model `kyutai/pocket-tts` |
| `kyutai/pocket-tts-without-voice-cloning` (tokenizer, voice embeddings) | CC-BY-4.0, not gated | model card |
| `alba` | CC-BY-4.0 | `kyutai/tts-voices`, alba-mackenna |
| `marius`, `javert` | CC0 | `kyutai/tts-voices`, voice-donations |
| `fantine`, `eponine`, `azelma` | CC-BY-4.0 | `kyutai/tts-voices`, VCTK |
| `cosette` (Expresso), `jean` (EARS) | CC-BY-NC-4.0 | `kyutai/tts-voices`; not offered |
| mlx-audio, the runtime | MIT | package metadata |
| `sentencepiece` | Apache-2.0 | package metadata |

Kyutai's model card adds a "Prohibited use" section: use must comply with
the law and must not involve voice impersonation or cloning without
consent, misinformation or deception (including presenting generated audio
as a real recording), or unlawful, harmful, harassing, discriminatory or
privacy-invasive content. voiced offers no voice cloning. voiced
redistributes none of the files: each Mac downloads them from Hugging Face,
and the README carries the CC-BY-4.0 attribution.

## Decision

`pocket` is a fifth TTS engine, `voiced.engines.pocket.PocketEngine`, a
`SentenceTTSEngine`, selectable and never the default.

- **Dependency.** `sentencepiece>=0.2,<0.3` joins the `tts` extra: Pocket
  TTS runs on the same mlx-audio as Kokoro and Soprano, and mlx-audio does
  not declare the tokenizer's package. The shipped lock already carries
  `tts`, so the app and every install path can switch to it. The comment on
  the mlx-audio pin in `pyproject.toml` names what Pocket relies on.
  Everything that imports MLX or mlx-audio runs inside `load()`.
- **GPU, behind the STT.** The model runs on the GPU, so it follows
  ADR 0019: before each frame it waits while an STT encoder call holds the
  lock (`voiced.gpu.Yielder`, the 300 ms per-sentence budget). A frame is
  about 10 ms of GPU work with a sync of its own (the end-of-speech check),
  so there are no large buffers to split, as Kokoro had to.
- **One model thread per sentence engine.** `SentenceTTSEngine` now runs
  the load and every step of a sentence (each `next()`, the `close()`) on
  the engine's own `ModelThread`, as Parakeet does, instead of the shared
  pool behind `run_blocking`: Pocket's sentence state cannot move between
  threads, and the next MLX engines that stream within a sentence keep
  state the same way. A TTS process serves one request at a time (the
  exclusive slot), so one thread costs Kokoro, Soprano and Phonon nothing,
  and cancellation still waits for the step in flight.
- **One sentence at a time.** Mimi's streaming decoder state lives on the
  model and is reset at the start of each sentence, so two sentences
  generated at once would garble each other. The exclusive slot already
  rules that out; `_stream` holds a lock from its first frame to its close,
  so a second overlapping sentence fails loudly instead.
- **Streaming within a sentence.** `_synth_chunks` yields every 80 ms frame
  as int16; first audio is the first frame. Closing the generator stops
  the sentence at the next frame. A sentence without a letter or a digit is
  skipped: the model reads "...", "!!!" or "?" as 0.4 to 0.8 s of sound.
- **No Hub at run time.** The engine builds mlx-audio's `Model` itself from
  the pinned snapshot's `config.json`, with the tokenizer path pointed at
  the pinned `tokenizer.model`, and loads each voice's `audio_prompt` from
  the pinned embeddings, evaluated at load so a missing or unreadable
  voice fails the load, not a request. A voice's model state is built for
  every sentence from that embedding, through two of mlx-audio's private methods
  (`_run_flow_lm_and_increment_step`, `_slice_flow_cache`), the steps of
  its own `get_state_for_audio_prompt` without its download. A state is
  used up by one sentence. The pin on mlx-audio covers them.
- **Models pinned.** The catalogue (`voiced.models`, ADR 0022) lists
  `mlx-community/pocket-tts` (`config.json`, `model.safetensors`: 235.7 MB)
  and, as a shared model like Phonon's voices, the revision of
  `kyutai/pocket-tts-without-voice-cloning` that mlx-audio 0.5.9 names
  (`tokenizer.model` and the six voices' `embeddings/*.safetensors`:
  3.4 MB); `models.lock.json` pins both. The files are identical at the
  repository's current head. The engine loads them with
  `snapshot(..., check_only=True)`, so the service never downloads:
  `voiced models download mlx-community/pocket-tts` fetches both.
- **Only catalogued models.** Another `kyutai/pocket-tts*` or
  `mlx-community/pocket-tts*` id (Kyutai's PyTorch checkpoint, another
  conversion) is refused when the engine is made (`ModelRefused`), not
  failed inside `load()`.
- **Voices.** The six whose recordings allow commercial use, by Kyutai's
  lowercase names, in a static table so the catalogue lists them without
  loading anything; `alba`, Kyutai's default, first. Language `en` for all:
  the speakers' accents vary and Kyutai does not tag them. The two CC-BY-NC
  voices are not in the table, so asking for one is `UnknownVoice` before
  the model sees it.
- **Speed.** No rate control; `speed` is checked against the shared bounds
  and ignored, as for Soprano and Phonon.
- **English only.** Kyutai publishes French, German, Portuguese, Italian,
  Spanish and Dutch checkpoints, and v4.0.0 adds 18M, 7M and 2M English
  ones, but none has an mlx-audio conversion, and mlx-audio 0.5.9 does not
  read the small models' new options. voiced serves the one English model.

### Pocket TTS or Phonon

Pick Pocket TTS for Kyutai's own 100M model, the base Phonon derives from,
with voices recorded from real speakers under CC0 and CC-BY-4.0, on the GPU
behind speech-to-text when the CPU is the busier side; in voiced it speaks
English only, since the multilingual checkpoints have no MLX conversion.
Pick Phonon 31M (Apache-2.0, no attribution, 37 English presets) for the
smallest model on the CPU, leaving the GPU to speech-to-text, or Phonon 40M
(CC-BY-NC-4.0) for non-commercial use.

## Alternatives considered

- **Phonon's runtime, `ptts`.** Not without voiced carrying a port. Checked
  by reading the sources, not by a load attempt: the `ptts` 0.4.0 wheel
  voiced uses for Phonon reads Phonon's `config.json` schema only. Kyutai's
  v4.0.0 release (2026-10-20) ships Rust binaries built on xn-ptts, but
  they translate Kyutai's YAML configs to that schema in their own code and
  build on a fork of xn-ptts (a branch adding what Pocket checkpoints need:
  `inner_dim`, `z_dim`, low-rank text tables, and more), pinned to a
  commit. Running Pocket TTS on stock `ptts` would mean voiced writing
  configs for each checkpoint and depending on that fork, and those
  binaries run on the CPU only.
- **MLX on the CPU.** 225 ms to the first frame and 0.5 times real time
  (Context): too slow to stream.

mlx-audio on the GPU already runs the model with the least code on voiced's
side, so that is the path taken.

## Measurements

Apple M1 Max, 64 GB, macOS 15.7.4, a quiet machine. `voiced serve stt`
(Parakeet), `voiced serve tts` (Kokoro) and a second `voiced serve tts`
running Pocket TTS, each in its own process from a terminal, over real
sockets, measured with `scripts/bench.py`'s own functions (`tts_first_audio_ms`,
`ws_finalize_ms`, `tts_busy`, `first_audio_while_streaming_ms`,
`batch_transcription_ms`, `memory`) with voice `alba`. These are the
contention numbers; the row in `docs/benchmarks.md` (`scripts/bench_engine.py`,
a separate run) has 45 / 47 ms first audio and a 0.67 GB footprint. First audio is the
`openai` client's streamed `pcm` request for "Turn on the kitchen lights."
to the first byte, after one warm-up.

| Metric | Measured | Target |
|---|---|---|
| Pocket TTS first audio | median 44 / p95 50 / worst 51 ms (n=10) | <= 200 ms |
| Pocket TTS first audio while another client streams to STT | median 42 / p95 61 / worst 66 ms (n=30) | <= 200 ms |
| Kokoro first audio, same run | median 95 / p95 118 / worst 133 ms (n=10) | <= 200 ms |
| Streaming finalize, 64 ms chunks, 0 ms trailing silence, alone | median 113 / p95 123 / worst 124 ms (n=32) | <= 150 ms worst |
| The same, Kokoro synthesizing | median 101 / p95 128 / worst 131 ms (n=16) | <= 150 ms worst |
| The same, Pocket TTS synthesizing | median 100 / p95 104 / worst 108 ms (n=16) | <= 150 ms worst |
| Streaming finalize, 200 ms trailing silence, Pocket TTS synthesizing | median 1 / worst 1 ms (n=16) | <= 150 ms worst |
| Batch transcription, 10.1 s clip, alone | median 241 / worst 254 ms (n=10) | <= 300 ms |
| Batch transcription, 10.1 s clip, Pocket TTS synthesizing | median 275 / worst 278 ms (n=10) | <= 300 ms |
| Pocket TTS service, idle after a long reply | footprint 0.68 GB, RSS 0.43 GB, MLX peak 968 MB | |
| STT + Pocket TTS footprint, idle after use | 2.48 GB | <= 3 GB |

Two earlier runs shared the laptop with other benchmarks and are not
reported; this one held the machine to itself. The model loads in about
3 s from a warm cache.

## Known misses and limits

- None of ADR 0008's targets was missed in this run. Batch transcription
  while Pocket TTS synthesizes is 34 ms slower in the median, within
  target; batch calls do not take the GPU lock (ADR 0019).
- The MLX peak, 968 MB, is a transient while a sentence is generated. It is
  inside the memory budget beside Parakeet but more than Phonon's whole
  footprint (0.26 GB).
- The engine relies on two private mlx-audio methods and on the converted
  config's layout (`flow_lm.lookup_table.tokenizer_path`); a bump of
  mlx-audio has to re-run the model tier.
- Speed 4 of 5 in `docs/benchmarks.md`: a real-time factor of 0.167, about
  four times Kokoro's and Phonon's, so a long reply takes longer to finish,
  though its first audio (45 ms median) is second only to Phonon's.
- Not measured on the Mac Studio.

## Consequences

- Every `SentenceTTSEngine` runs on its own model thread; an engine added
  later gets that without asking.
- `sentencepiece` is installed with the `tts` extra for everyone.
- An mlx-audio bump has to re-run Pocket's model tier (the private methods
  and the config layout above).
- The README carries Kyutai's CC-BY-4.0 attribution for the model, the
  tokenizer and the voices.
