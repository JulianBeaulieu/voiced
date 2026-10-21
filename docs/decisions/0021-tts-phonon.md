# 0021: Phonon ships selectable, not default, on the CPU

Date: 2026-10-20. Status: accepted.

## Context

Phonon is Gradium's family of small text-to-speech models: a flow-matching
transformer that predicts Mimi codec latents one 80 ms frame at a time, and
Mimi's decoder, which turns them into 24 kHz audio. The English model,
`gradium/phonon-31m-en`, has 31M parameters without the audio tokenizer and
its 8-bit weights (`model.q8.gguf`) are 64.5 MB. It runs in Gradium's Rust
runtime, `xn`, shipped in the `ptts` wheel: no PyTorch, no espeak-ng, its
own text normalization. The voices sounded good for their size, and a model
this small could leave the GPU to speech-to-text.

Checked on an Apple M1 Max (64 GB, macOS 15.7.4) with Python 3.12 before
building anything:

- `ptts` 0.4.0 has a `cp39-abi3-macosx_11_0_arm64` wheel of 6.5 MB whose only
  dependency is numpy. The quickstart synthesized.
- It downloads into the standard Hugging Face cache (it honours `HF_HOME`)
  and reads a local checkpoint folder without the Hub.
- `stream()` yields before the sentence is done: the first chunk came after
  21 to 25 ms warm (48 ms cold), and a 5.4 s sentence finished in about
  360 ms (14 to 16 times real time). Chunks are float32, 80 to 320 ms long.
- It runs on the CPU by default; the build also offers `metal`. During
  synthesis it kept about five of the ten cores busy. Peak RSS with the
  model loaded was 229 MB.
- An `AudioStream` can be advanced from any thread, and `next()` releases
  the GIL: an asyncio ticker on 5 ms sleeps saw at most 8.7 ms between
  ticks while chunks were read through `asyncio.to_thread`.

## Licences

| What | Licence | Source |
|---|---|---|
| `gradium/phonon-31m-en` (weights, tokenizer, default voice) | Apache-2.0 | model card, `license: apache-2.0` |
| `gradium/phonon-voices` (the preset voices) | Apache-2.0 | model card |
| `gradium/phonon-40m-en`, `-40m-multilingual` (the two voiced supports) | CC-BY-NC-4.0, gated | model cards, `license: cc-by-nc-4.0` |
| `ptts`, the runtime | MIT OR Apache-2.0 | wheel metadata |
| Kyutai's Pocket TTS and Mimi, which Phonon builds on | CC-BY-4.0 | `kyutai/pocket-tts`, `kyutai/mimi` model cards |

Gradium releases Phonon 31M's weights, tokenizer and voices under
Apache-2.0, which allows commercial use. voiced redistributes none of them:
each Mac downloads them from Hugging Face. The 40M models are for
non-commercial use only and need their terms accepted on the Hub.

## Decision

`phonon` is a fourth TTS engine, `voiced.engines.phonon.PhononEngine`, a
`SentenceTTSEngine`, selectable and never the default. Kokoro stays the
default: it is the engine every other measurement and the Home Assistant
path were tuned on, and Phonon is new.

- **Dependency.** `ptts>=0.4,<0.5` in a `phonon` extra of its own (it needs
  no MLX, so `pip install voiced[phonon]` works alone), and in the shipped
  lock with `stt` and `tts`, so the app, Homebrew and `voiced install` all
  carry it (6.5 MB) and the menu bar app can switch to it. Everything that
  imports `ptts` runs inside `load()`.
- **CPU, not GPU.** The model is loaded with `device="cpu"`. It never
  touches Metal, so ADR 0019's lock does not apply to it: it neither waits
  for nor holds the GPU the STT goes first on, and Kokoro's yield points
  have no counterpart here.
- **Threads.** Each blocking step (the load, every `next()` of a sentence's
  stream and its `close()`) runs in a worker thread through `run_blocking`
  (amended by ADR 0028: every `SentenceTTSEngine` step now runs on the
  engine's `ModelThread`), like the other engines' model calls, so the event
  loop never waits and a cancelled request keeps the slot until the step
  returns. No dedicated model thread: `ptts` holds no thread-bound state. `ptts`'s own pool of
  CPU threads defaults to every core, efficiency cores included, beside
  the STT's CPU work, and nothing else would bound it on a Mac with four
  cores or with two STT streams open; the engine sets it to 4
  (`ptts.set_num_threads`, process-wide, before the model is built). On
  the M1 Max, 4 threads were no slower than the default 10 (see
  Measurements).
- **Streaming within a sentence.** `SentenceTTSEngine` gains
  `_synth_chunks(sentence, voice, speed)`, a blocking generator of int16
  pieces; the loop yields each piece as it comes and closes the generator
  when the sentence ends or the client leaves. Its default yields
  `_synth_sentence` as one piece, so Kokoro and Soprano are unchanged.
  Phonon yields each decoded chunk, converted to int16, and closing it
  leaves `ptts`'s `with` block, which stops Phonon's workers. First audio
  is the first chunk, not the sentence. Phonon's `_synth_sentence` is never
  called and raises `NotImplementedError`. A sentence without a letter or
  a digit ("...", "!!!") is skipped: Phonon reads one as 1 to 1.6 s of
  sound.
- **Text.** `lang="en"`, `ptts`'s English normalization: voiced is English
  only, and the runtime requires a language. voiced has already normalized
  the text, so it mostly finds nothing to do.
- **Model and voices pinned.** `voiced.models.OPTIONAL_PINS` fixes the
  model revision and the voices repository's revision: the ones `ptts`
  0.4.0 names as its defaults (later commits on the Hub change only the
  model card). They are pinned like the defaults but left out of the app's
  manifest, which sums what a default install downloads. The engine
  declares `download_patterns()` (config, tokenizer, `model.q8.gguf`,
  default voice: 65.0 MB) and a new optional `extra_downloads()`, the voices
  repository with `voices.jsonl` and the 37 English presets (9.5 MB).
  `voiced install` fetches both with byte progress, `load()` reads them
  from the cache through `voiced.models.snapshot`, and
  `voiced uninstall --remove-models` removes their pinned revisions, and
  the 40M models', with the defaults' (every `OPTIONAL_PINS` entry is in
  `pinned_revisions()`, whichever engine the config names). `ptts` gets local
  folders only, the catalogue included, so it never reaches the Hub
  itself. Switching to Phonon from the menu bar app restarts the service,
  whose `load()` then downloads the model and voices (about 75 MB) with no
  progress shown, until ADR 0022's on-demand downloads.
- **Voices.** `default`, the checkpoint's own voice, first (an engine switch
  without a voice picks it), then the English rows of `voices.jsonl` as a
  static table in the engine: the id is the catalogue's name as it is
  (`Easton`, `The Snow Queen`), the language its locale (`en-US`, `en-GB`,
  `en` for the character voices) and its gender, so the menu bar app labels
  them as it does Kokoro's. `engines.list` carries the languages as a
  `voice_languages` map beside `voice_labels`, for any engine, so the app
  can group voices without parsing labels. A static table lets the catalogue list voices
  without the cache (ADR 0013); a model test keeps it equal to the pinned
  `voices.jsonl`. Every preset is registered with `add_voice` at load, so
  a named voice is on disk before the first request, and a name outside
  the table is `UnknownVoice` before `ptts` sees it. Voice cloning needs
  Gradium's hosted API and is out of scope.
- **Speed.** Phonon has no rate control; `speed` is checked against the
  shared bounds and ignored, as for Soprano.

### The 40M models

`tts.model = "gradium/phonon-40m-en"` or `"gradium/phonon-40m-multilingual"`
works, at pinned revisions, and is never a default. The other 40M models
(French, German, Spanish, Portuguese) are refused when the engine is
built: they do not speak English, voiced's one language, and are not
pinned. The engine sets its `license` from the model, so `/v1/models`, the
status page and Wyoming's info report `CC-BY-NC-4.0` for the 40M models and
`Apache-2.0` for 31M. Both are gated on the Hub. Without access, the
download fails at load or at `voiced install` with one plain message:
accept the licence at the model's page, then run `hf auth login` with
`HF_HOME` set to the Hugging Face home the services use, as the user they
run as. The plists pass `HF_HOME` and no `HF_TOKEN`, so the token has to be
the `token` file there. `voiced doctor` does not check for it: a token file
says nothing about access, and ADR 0022 replaces this with its
`token_missing`, `token_invalid` and `gated_terms` states.

## Measurements

Apple M1 Max, 64 GB, macOS 15.7.4. `scripts/bench.py` with the new
`--phonon-url`, against `voiced serve stt` (Parakeet), `voiced serve tts`
(Kokoro) and a third `voiced serve tts` running Phonon, each in its own
process, over real sockets. First audio is the `openai` client's streamed
`pcm` request for "Turn on the kitchen lights." to the first byte, voice
`default`, after one warm-up. This run used `ptts`'s default pool of 10
threads, before the engine set 4.

| Metric | Measured |
|---|---|
| Phonon 31M first audio | median 44 / p95 50 / worst 51 ms (n=10) |
| Phonon 31M first audio while another client streams to STT | median 25 / p95 37 / worst 38 ms (n=30) |
| Kokoro first audio, same run, for comparison | median 103 / p95 106 / worst 107 ms (n=10) |
| Kokoro first audio while another client streams to STT, same run | median 142 / p95 282 / worst 351 ms (n=30) |
| STT streaming finalize, 64 ms chunks, 0 ms trailing silence, alone | median 100 / p95 106 / worst 111 ms (n=32) |
| The same while Phonon synthesizes back to back | median 109 / p95 124 / worst 134 ms (n=16) |
| The same with 200 ms trailing silence, Phonon synthesizing | median 1 / p95 2 / worst 2 ms (n=16) |
| TTS (Phonon 31M) memory after a long reply | footprint 0.26 GB, RSS 0.53 GB, no MLX allocations |
| Phonon 40M English first audio | median 40 / p95 46 / worst 47 ms (n=10) |
| TTS (Phonon 40M English) memory after a long reply | footprint 0.18 GB, RSS 0.41 GB |

The thread pool, measured once more the same way against a fresh STT server
and Phonon server, with `ptts`'s pool at 4 and then at 10 threads:

| Metric | 4 threads | 10 threads |
|---|---|---|
| Phonon 31M first audio | median 28 / p95 33 / worst 33 ms (n=10) | median 33 / p95 36 / worst 36 ms (n=10) |
| The same while another client streams to STT | median 19 / p95 27 / worst 28 ms (n=30) | median 26 / p95 41 / worst 45 ms (n=30) |
| STT finalize, 64 ms chunks, 0 ms trailing silence, Phonon synthesizing | median 107 / p95 113 / worst 115 ms (n=16) | median 106 / p95 109 / worst 109 ms (n=16) |

Four threads cost nothing here and leave six cores free; on this ten-core
Mac the STT finalize is the same within noise either way. The benefit is
on smaller Macs, which were not measured.

Phonon does not wait for the STT's encoder calls, so its first audio does
not grow while someone speaks; it ran faster then, plausibly because the
CPU stays clocked up. Its CPU work costs the STT a little: the push-to-talk
finalize (0 ms trailing silence) rose by about 9 ms median and 23 ms worst,
inside the 150 ms target. Calling the engine in process, one warm
two-sentence request per voice right after load (n=1 each, no HTTP), first
audio was 12 ms for `default`, 37 ms for `Easton` and 36 ms for `Captain
Silas Blackwater`, in 28 to 30 pieces.

## Consequences

- A fourth engine to keep working. `ptts` is pre-1.0, so the pin is to its
  minor release; the fake tier runs the engine against a stand-in `ptts`
  on CI, and the model tier checks load, synthesis, piece order and timing,
  unknown voices, the voice table against `voices.jsonl`,
  `/v1/audio/voices` and `/v1/models`, and the 40M English model when a
  token is present. The e2e suite feeds Phonon's speech for two presets
  back through Parakeet.
- `SentenceTTSEngine._synth_chunks` is open to any engine that decodes a
  sentence in pieces.
- `extra_downloads()` joins `download_patterns()` and `catalogue_voices()`
  as an optional engine classmethod outside the frozen interface.
- Phonon's voices outside English (65 of the 102) and the other 40M
  languages wait for voiced's languages beyond English.
- `engines.list` gains `voice_languages` for every TTS engine (ADR 0013).

## Amendment (ADR 0022)

The pins moved to `voiced/models/models.lock.json`, written from the
catalogue in `voiced/models/catalogue.py`: `OPTIONAL_PINS`,
`download_patterns()` and `extra_downloads()` are gone, and the voices
repository is Phonon's `shared` model, downloaded with any of its models
and deleted with the last. `gradium/phonon-40m-multilingual` is not in the
catalogue (its gated files could not be hashed for the lock), so the engine
refuses it like the other 40M variants; `gradium/phonon-40m-en` stays. The
engine loads from the cache only (`snapshot(..., check_only=True)`): `voiced
models download gradium/phonon-31m-en` fetches the model and the voices.
